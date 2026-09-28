"""Governance & community checks."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from scverse_repo_health.models import Tier, failed, passed, unknown, warned
from scverse_repo_health.registry import check

from ._util import plural, pyproject, truncate

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

    from scverse_repo_health.models import CheckResult, RepoData

CATEGORY = "Governance"
STALE_DAYS = 365
REQUIRED_TOPIC = "scverse"
#: SPDX ids of the OSI-approved licenses that actually show up across the org.
OSI_LICENSES = {
    "AGPL-3.0",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "GPL-2.0",
    "GPL-3.0",
    "ISC",
    "LGPL-3.0",
    "MIT",
    "MPL-2.0",
}
#: GPL and AGPL, in SPDX ids, classifiers and free text alike; LGPL is deliberately not matched.
GPL_RE = re.compile(r"\bA?GPL|(?<!Lesser )General Public License")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def days_since(value: str | None, now: datetime | None = None) -> int | None:
    parsed = parse_ts(value)
    if parsed is None:
        return None
    return ((now or datetime.now(UTC)) - parsed).days


@check(
    id="governance/license",
    tier=Tier.REQUIRED,
    category=CATEGORY,
    title="OSI license",
    description="The repo carries a recognised OSI-approved license",
    needs=("meta",),
)
def osi_license(r: RepoData) -> CheckResult:
    license_obj = r.repo.get("license") or {}
    spdx = license_obj.get("spdx_id")
    fix = f"{r.html_url}/community/license/new?branch={r.default_branch}"
    if not spdx or spdx == "NOASSERTION":
        blob = r.find("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING")
        if blob:
            return warned(f"`{blob}` exists but GitHub could not identify the license", r.blob_url(blob))
        return failed("No license", fix)
    url = r.blob_url(r.find("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING") or "LICENSE")
    if spdx not in OSI_LICENSES:
        return warned(f"`{spdx}` is not in the known-OSI list", url)
    return passed(spdx, url)


def gpl_license(dist: Mapping[str, Any]) -> str | None:
    """The license a distribution declares if every option it offers is GPL or AGPL, else ``None``."""
    if expression := dist.get("license_expression"):
        options = re.split(r"\s+OR\s+", expression)
    else:
        classifiers = [c.rsplit(" :: ", 1)[-1] for c in dist.get("classifiers") or [] if c != "License :: OSI Approved"]
        options = classifiers or [text for text in [dist.get("license")] if text]
    return " OR ".join(options) if options and all(GPL_RE.search(o) for o in options) else None


def _is_gpl_repo(r: RepoData) -> bool:
    return bool(GPL_RE.search((r.repo.get("license") or {}).get("spdx_id") or ""))


@check(
    id="governance/gpl-free",
    tier=Tier.REQUIRED,
    category=CATEGORY,
    title="GPL-free dependencies",
    description="No runtime dependency, direct or transitive, is GPL-licensed, unless the repo is GPL itself",
    needs=("cont", "pypi"),
    applies_to=lambda r: r.has_path("pyproject.toml") and not _is_gpl_repo(r),
)
def gpl_free(r: RepoData) -> CheckResult:
    fix = r.blob_url("pyproject.toml")
    if (reason := r.is_unavailable("dependencies")) is not None:
        return unknown(reason, fix)
    if "dependencies" in ((pyproject(r) or {}).get("project", {}).get("dynamic") or []):
        return unknown("`dependencies` is dynamic in `pyproject.toml`", fix)
    if r.dependencies is None:
        return unknown("Dependencies were not collected", fix)
    by_name = {d["name"]: d for d in r.dependencies}

    def direct(dist: Mapping[str, Any]) -> str:
        while dist.get("via") in by_name:
            dist = by_name[dist["via"]]
        return dist["name"]

    gpl = [
        f"`{d['name']}` ({terms})" + (f" via `{root}`" if (root := direct(d)) != d["name"] else "")
        for d in r.dependencies
        if (terms := gpl_license(d))
    ]
    if gpl:
        return failed(truncate(gpl), fix)
    return passed(f"None of {plural(len(r.dependencies), 'runtime dependency', 'runtime dependencies')} is GPL", fix)


@check(
    id="governance/description-topics",
    tier=Tier.RECOMMENDED,
    category=CATEGORY,
    title="Description, homepage, topic",
    description="The repo has a description, a homepage and the `scverse` topic",
    needs=("meta",),
)
def description_and_topics(r: RepoData) -> CheckResult:
    fix = r.html_url
    missing = []
    if not (r.repo.get("description") or "").strip():
        missing.append("description")
    if not (r.repo.get("homepage") or "").strip():
        missing.append("homepage")
    if REQUIRED_TOPIC not in r.topics:
        missing.append(f"`{REQUIRED_TOPIC}` topic")
    if missing:
        return failed(f"Missing {truncate(missing)}", fix)
    return passed(f"Described, linked and tagged ({len(r.topics)} topics)", fix)


@check(
    id="governance/maintained",
    tier=Tier.INFORMATIONAL,
    category=CATEGORY,
    title="Actively maintained",
    description="Pushed to within the last year",
    needs=("meta",),
)
def maintained(r: RepoData) -> CheckResult:
    fix = f"{r.html_url}/commits/{r.default_branch}"
    days = days_since(r.repo.get("pushed_at"))
    if days is None:
        return unknown("No push timestamp", fix)
    if days > STALE_DAYS:
        return warned(f"Last push {days // 30} months ago", fix)
    return passed(f"Last push {days} days ago", fix)
