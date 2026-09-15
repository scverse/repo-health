"""Integration testing against upstream core packages."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scverse_repo_health.models import Tier, failed, passed
from scverse_repo_health.registry import check
from scverse_repo_health.sources.integration import REPO

from ._util import plural, truncate

if TYPE_CHECKING:
    from scverse_repo_health.models import CheckResult, RepoData

CATEGORY = "Supply chain"


@check(
    id="integration/upstream-tests",
    tier=Tier.REQUIRED,
    category=CATEGORY,
    title="Integration tests passing",
    description=f"This package's test suite passes in {REPO} against upstream core packages",
    needs=("meta",),
    applies_to=lambda r: r.integration is not None,
)
def upstream_tests(r: RepoData) -> CheckResult:
    data = r.integration or {}
    broken = data.get("failed") or []
    total = data.get("total") or 0
    url = data.get("url")
    if broken:
        return failed(f"{len(broken)} of {plural(total, 'job')} failing: {truncate(broken)}", url)
    return passed(f"All {plural(total, 'job')} passing", url)
