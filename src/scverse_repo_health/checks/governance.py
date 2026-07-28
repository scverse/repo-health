"""Governance & community checks."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from scverse_repo_health.models import Tier, failed, passed, unknown, warned
from scverse_repo_health.registry import check

from ._util import truncate

if TYPE_CHECKING:
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


@check(
    id="governance/community-files",
    tier=Tier.RECOMMENDED,
    category=CATEGORY,
    title="Community files",
    description="Code of conduct, contributing guide and issue templates are in place",
    needs=("meta",),
)
def community_files(r: RepoData) -> CheckResult:
    fix = f"{r.html_url}/community"
    if (reason := r.is_unavailable("community_profile")) is not None:
        return unknown(reason, fix)
    if r.community_profile is None:
        return unknown("Community profile could not be read", fix)
    files = r.community_profile.get("files") or {}
    wanted = {
        "code of conduct": "code_of_conduct",
        "contributing guide": "contributing",
        "issue template": "issue_template",
    }
    missing = sorted(label for label, key in wanted.items() if not files.get(key))
    percent = r.community_profile.get("health_percentage")
    if missing:
        return failed(f"Missing {truncate(missing)} (health {percent}%)", fix)
    return passed(f"All present (health {percent}%)", fix)


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
    id="governance/citation",
    tier=Tier.INFORMATIONAL,
    category=CATEGORY,
    title="CITATION.cff",
    description="A machine-readable citation file is present",
    needs=("cont",),
)
def citation(r: RepoData) -> CheckResult:
    if (reason := r.is_unavailable("tree")) is not None:
        return unknown(reason)
    if (path := r.find("CITATION.cff", "CITATION.CFF")) is not None:
        return passed(f"`{path}` present", r.blob_url(path))
    return warned("No `CITATION.cff`", r.new_file_url("CITATION.cff"))


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
