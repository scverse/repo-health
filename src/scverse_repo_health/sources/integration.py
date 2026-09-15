"""Results of the daily ``scverse/integration-testing`` run."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from scverse_repo_health._log import log

if TYPE_CHECKING:
    from typing import Any

    from .github import GitHubClient

REPO = "scverse/integration-testing"
BRANCH = "main"
ACTIONS_APP = "github-actions"
ACTIONS_URL = f"https://github.com/{REPO}/actions"
#: ``test (3.14, true, scanpy, scanpy2)`` — python, prerelease flag, package, then extras.
JOB_RE = re.compile(r"^test \((?P<args>.+)\)$")
_JOB_FIELDS = 3
_OK = frozenset({"success", "neutral", "skipped"})


def parse_job(name: str) -> tuple[str, str] | None:
    match = JOB_RE.match(name.strip())
    if match is None:
        return None
    parts = [p.strip() for p in match["args"].split(",")]
    if len(parts) < _JOB_FIELDS:
        return None
    python, is_pre, package = parts[:_JOB_FIELDS]
    return package, f"{python} pre" if is_pre == "true" else python


def summarise(check_runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Group matrix jobs by package, keyed by lowercased name."""
    out: dict[str, dict[str, Any]] = {}
    for run in check_runs:
        parsed = parse_job(str(run.get("name") or ""))
        if parsed is None:
            continue
        package, leg = parsed
        entry = out.setdefault(package.lower(), {"package": package, "total": 0, "failed": [], "url": ACTIONS_URL})
        entry["total"] += 1
        if run.get("conclusion") not in _OK:
            entry["failed"].append(leg)
            entry["url"] = run.get("html_url") or entry["url"]
    for entry in out.values():
        entry["failed"].sort()
    return out


async def fetch_results(gh: GitHubClient) -> dict[str, dict[str, Any]] | None:
    """Per-package results of the newest completed run, or ``None`` if it cannot be read."""
    suites, reason = await gh.try_get(f"/repos/{REPO}/commits/{BRANCH}/check-suites", {"per_page": 100})
    if reason or not isinstance(suites, dict):
        log.warning(f"integration-testing check suites unreadable: {reason or 'unexpected payload'}")
        return None
    completed = [
        s
        for s in suites.get("check_suites") or []
        if s.get("status") == "completed"
        and (s.get("app") or {}).get("slug") == ACTIONS_APP
        and s.get("latest_check_runs_count")
    ]
    if not completed:
        log.warning("integration-testing has no completed run to read")
        return None
    suite = max(completed, key=lambda s: str(s.get("created_at") or ""))
    runs, reason = await gh.try_get(f"/repos/{REPO}/check-suites/{suite['id']}/check-runs", {"per_page": 100})
    if reason or not isinstance(runs, dict):
        log.warning(f"integration-testing check runs unreadable: {reason or 'unexpected payload'}")
        return None
    return summarise(runs.get("check_runs") or [])
