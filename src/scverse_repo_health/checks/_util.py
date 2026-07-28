"""Parsing helpers shared by the checks. Pure functions, no I/O."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import yaml

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any

    from scverse_repo_health.models import RepoData

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_USES_RE = re.compile(r"^\s*-?\s*uses:\s*['\"]?([^'\"\s#]+)", re.MULTILINE)


def is_sha(ref: str) -> bool:
    return bool(SHA_RE.match(ref.strip()))


def load_yaml(text: str | None) -> dict[str, Any] | None:
    """``yaml.safe_load`` that returns ``None`` instead of raising or yielding a scalar."""
    if not text:
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def load_toml(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    """A workflow's ``on:`` block.

    YAML 1.1 parses the bare word ``on`` as the boolean ``True``, which is why this looks
    the way it does.
    """
    raw = workflow.get("on", workflow.get(True))
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        return {raw: None}
    if isinstance(raw, list):
        return dict.fromkeys(raw)
    return {}


@dataclass(frozen=True, slots=True)
class Use:
    """One ``uses:`` reference in a workflow."""

    path: str
    job: str
    ref: str

    @property
    def action(self) -> str:
        return self.ref.split("@")[0]

    @property
    def version(self) -> str:
        _, _, version = self.ref.partition("@")
        return version

    @property
    def is_local(self) -> bool:
        """``./.github/workflows/x.yml`` and docker refs carry no version to pin."""
        return self.ref.startswith(("./", "docker://"))

    @property
    def pinned(self) -> bool:
        return self.is_local or is_sha(self.version)


def iter_uses(repo: RepoData) -> Iterator[Use]:
    """Every ``uses:`` across every workflow, including reusable-workflow job refs."""
    for path, text in repo.workflows.items():
        workflow = load_yaml(text)
        if workflow is None:
            # Unparseable YAML still tells us about pinning; fall back to the raw text.
            for ref in _USES_RE.findall(text or ""):
                yield Use(path=path, job="?", ref=ref)
            continue
        for job_name, job in (workflow.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            if isinstance(job.get("uses"), str):
                yield Use(path=path, job=job_name, ref=job["uses"])
            for step in job.get("steps") or []:
                if isinstance(step, dict) and isinstance(step.get("uses"), str):
                    yield Use(path=path, job=job_name, ref=step["uses"])


def iter_steps(repo: RepoData) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """``(workflow path, job name, step)`` for every step we can parse."""
    for path, text in repo.workflows.items():
        workflow = load_yaml(text)
        if workflow is None:
            continue
        for job_name, job in (workflow.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                if isinstance(step, dict):
                    yield path, job_name, step


def precommit_repos(repo: RepoData) -> list[dict[str, Any]]:
    """Entries of ``.pre-commit-config.yaml``'s ``repos:`` list."""
    config = load_yaml(precommit_text(repo))
    if not config:
        return []
    return [r for r in config.get("repos") or [] if isinstance(r, dict)]


def precommit_text(repo: RepoData) -> str | None:
    for path in (".pre-commit-config.yaml", ".pre-commit-config.yml"):
        if (text := repo.file(path)) is not None:
            return text
    return None


def precommit_path(repo: RepoData) -> str:
    return repo.find(".pre-commit-config.yaml", ".pre-commit-config.yml") or ".pre-commit-config.yaml"


def dependabot_config(repo: RepoData) -> dict[str, Any] | None:
    return load_yaml(repo.file(".github/dependabot.yml") or repo.file(".github/dependabot.yaml"))


def pyproject(repo: RepoData) -> dict[str, Any] | None:
    return load_toml(repo.file("pyproject.toml"))


def is_python_package(repo: RepoData) -> bool:
    """Repos that ship a distribution — the only ones packaging checks apply to."""
    return repo.has_path("pyproject.toml") or repo.has_path("setup.py")


def has_workflows(repo: RepoData) -> bool:
    return bool(repo.workflows)


def host_of(url: str | None) -> str:
    if not url:
        return ""
    return (urlparse(url).hostname or "").lower()


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    word = plural_form or f"{singular}s"
    return f"{count} {singular if count == 1 else word}"


def truncate(items: list[str], limit: int = 3) -> str:
    """``a, b, c and 4 more`` — keeps tooltips readable."""
    shown = items[:limit]
    rest = len(items) - len(shown)
    text = ", ".join(shown)
    return f"{text} and {rest} more" if rest > 0 else text
