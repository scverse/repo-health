"""Run `zizmor <https://docs.zizmor.sh>`_ over a repository's CI definitions.

zizmor can audit a ``owner/repo`` slug itself, but this module writes the blobs the
collector has already fetched into a temp directory and audits that instead. The files are
in hand and ETag-cached either way, so this costs no extra GitHub requests, and the whole
audit replays from a captured fixture — a check that cannot be reproduced offline is a
check nobody can debug.

What zizmor is pointed at is deliberately *everything* it knows how to read, not just
``.github/workflows/``: a repo whose own pre-commit hook is scoped to the workflows
directory passes its own audit while `.github/dependabot.yml` goes unaudited. This one
happened here, which is why the check exists in this form.

Online audits (stale action refs, known-vulnerable actions) need a token and are enabled
when one is present. Without one, zizmor is asked for offline audits explicitly so the
result says which of the two it was rather than depending on the ambient environment.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from scverse_repo_health._log import log

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from typing import Any

#: Persona to audit with. `regular` is zizmor's own default and the one tuned for minimal
#: false positives — the right bar for a required check. `pedantic` findings are style.
PERSONA = "regular"
#: Kept per repo, for the failing cell's tooltip. A repo with more than this has plenty to
#: be getting on with.
MAX_FINDINGS = 25
#: Seconds. A dozen small YAML files; anything near this means something is wrong.
TIMEOUT = 120
#: Composite actions live anywhere in the tree; workflows and Dependabot config do not.
ACTION_NAMES = ("action.yml", "action.yaml")
CONFIG_PATHS = (".github/zizmor.yml", ".github/zizmor.yaml")
DEPENDABOT_PATHS = (".github/dependabot.yml", ".github/dependabot.yaml")


class ZizmorError(Exception):
    """zizmor could not be run, or did not return findings we can read."""


def audit_inputs(paths: Iterable[str]) -> list[str]:
    """Every path in a repo tree that zizmor would collect, in a stable order."""
    found = {
        path
        for path in paths
        if (path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")))
        or path.rsplit("/", 1)[-1] in ACTION_NAMES
        or path in DEPENDABOT_PATHS
    }
    return sorted(found)


async def audit(files: Mapping[str, str], *, token: str | None = None, config: str | None = None) -> dict[str, Any]:
    """Audit ``files`` (path -> text) and return what the check needs to judge them.

    ``config`` names the entry of ``files`` that is the repo's own ``zizmor.yml``, if it
    has one — it is written alongside the rest but is not itself audited.

    Raises :class:`ZizmorError` rather than returning a verdict of its own, so that a
    missing binary or a crashed run shows up as an unreadable cell instead of a failing
    repository.
    """
    if (binary := shutil.which("zizmor")) is None:
        msg = "zizmor is not installed"
        raise ZizmorError(msg)
    if not files:
        msg = "nothing to audit"
        raise ZizmorError(msg)

    with tempfile.TemporaryDirectory(prefix="repo-health-zizmor-") as directory:
        root = Path(directory)
        written = _materialise(root, files)
        args = [binary, "--format=json", "--no-progress", f"--persona={PERSONA}"]
        if token is None:
            args.append("--no-online-audits")
        if config is not None and (root / config).is_file():
            # `.github/zizmor.yml` discovery is relative to the working directory, which is
            # this temp dir; passing it explicitly keeps that from being a coincidence.
            args += ["--config", config]
        args.append(".")
        findings = await _run(args, cwd=root, token=token)

    return {
        "count": len(findings),
        "findings": [_finding(f) for f in findings[:MAX_FINDINGS]],
        "inputs": len(written) - (1 if config in written else 0),
        "online": token is not None,
        "persona": PERSONA,
    }


def _materialise(root: Path, files: Mapping[str, str]) -> list[str]:
    """Write ``files`` under ``root``, skipping anything that would escape it."""
    written = []
    for path, text in files.items():
        target = (root / path).resolve()
        if not target.is_relative_to(root.resolve()):
            log.warning(f"refusing to write {path!r} outside the audit directory")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(path)
    return written


async def _run(args: list[str], *, cwd: Path, token: str | None) -> list[dict[str, Any]]:
    """Run zizmor and return its findings list.

    The exit code is not the verdict: zizmor exits non-zero *because* it found something,
    which is the normal case here. Only unparseable output is a failure.
    """
    env = {"GH_TOKEN": token} if token else None
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=TIMEOUT)
    except TimeoutError as exc:
        process.kill()
        msg = f"zizmor timed out after {TIMEOUT}s"
        raise ZizmorError(msg) from exc

    try:
        findings = json.loads(out)
    except json.JSONDecodeError as exc:
        tail = (err or b"").decode("utf-8", "replace").strip().splitlines()[-1:]
        msg = f"zizmor exited {process.returncode} without JSON output: {tail[0] if tail else 'no output'}"
        raise ZizmorError(msg) from exc
    if not isinstance(findings, list):
        msg = f"zizmor returned {type(findings).__name__}, not a list of findings"
        raise ZizmorError(msg)
    # Findings silenced by a `# zizmor: ignore[...]` comment or the repo's own config are a
    # deliberate decision by that repo; they are not this dashboard's business to reopen.
    return [f for f in findings if isinstance(f, dict) and not f.get("ignored")]


def _finding(raw: dict[str, Any]) -> dict[str, Any]:
    """The parts of a zizmor finding worth carrying into ``results.json``."""
    determinations = raw.get("determinations") or {}
    return {
        "ident": raw.get("ident") or "?",
        "desc": raw.get("desc") or "",
        "url": raw.get("url") or "",
        "severity": determinations.get("severity"),
        "confidence": determinations.get("confidence"),
        "path": _path_of(raw),
    }


def _path_of(raw: dict[str, Any]) -> str:
    """The file a finding points at, dug out of zizmor's nested location record."""
    for location in raw.get("locations") or []:
        key = ((location.get("symbolic") or {}).get("key") or {}).get("Local") or {}
        if path := key.get("verbatim_path"):
            return str(path).removeprefix("./")
    return "?"
