"""Branch protection & review checks.

``GET /repos/{o}/{r}/rules/branches/{branch}`` is the primary source — it works with a
plain read token and returns full rule parameters. Repos still on *classic* branch
protection return ``[]`` there, so we fall back to ``/branches/{b}/protection``, which
needs ``administration:read`` and therefore often renders as unknown rather than a
failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from scverse_repo_health.models import Tier, failed, passed, unknown, verdict, warned
from scverse_repo_health.registry import check

from ._util import truncate

if TYPE_CHECKING:
    from scverse_repo_health.models import CheckResult, RepoData

CATEGORY = "Branch protection"
MIN_REVIEWS = 1


@dataclass(slots=True)
class Protection:
    """The union of what rulesets and classic protection can tell us."""

    source: str
    deletion_blocked: bool = False
    force_push_blocked: bool = False
    requires_pr: bool = False
    review_count: int = 0
    status_checks: list[str] = field(default_factory=list)
    linear_history: bool = False


def protection_of(r: RepoData) -> Protection | None:
    """Normalise whichever protection mechanism this repo uses, or ``None`` if unreadable."""
    if r.rulesets:
        rules = {rule.get("type"): (rule.get("parameters") or {}) for rule in r.rulesets}
        pr = rules.get("pull_request")
        checks = (rules.get("required_status_checks") or {}).get("required_status_checks") or []
        return Protection(
            source="ruleset",
            deletion_blocked="deletion" in rules,
            force_push_blocked="non_fast_forward" in rules,
            requires_pr=pr is not None,
            review_count=int((pr or {}).get("required_approving_review_count") or 0),
            status_checks=[c.get("context", "") for c in checks],
            linear_history="required_linear_history" in rules,
        )
    if r.classic_protection:
        classic = r.classic_protection
        reviews = classic.get("required_pull_request_reviews")
        return Protection(
            source="classic",
            deletion_blocked=not (classic.get("allow_deletions") or {}).get("enabled", False),
            force_push_blocked=not (classic.get("allow_force_pushes") or {}).get("enabled", False),
            requires_pr=reviews is not None,
            review_count=int((reviews or {}).get("required_approving_review_count") or 0),
            status_checks=list((classic.get("required_status_checks") or {}).get("contexts") or []),
            linear_history=(classic.get("required_linear_history") or {}).get("enabled", False),
        )
    return None


def _unreadable(r: RepoData) -> str | None:
    return r.is_unavailable("rulesets", "classic_protection")


def _rules_url(r: RepoData) -> str:
    return r.settings_url("rules")


@check(
    id="branch/protected",
    tier=Tier.REQUIRED,
    category=CATEGORY,
    title="Default branch protected",
    description="Neither force-pushes nor deletion are allowed on the default branch",
    needs=("meta", "admin?"),
)
def protected(r: RepoData) -> CheckResult:
    fix = _rules_url(r)
    protection = protection_of(r)
    if protection is None:
        if (reason := _unreadable(r)) is not None:
            return unknown(
                f"No ruleset covers `{r.default_branch}`, and classic protection is unreadable: {reason}",
                fix,
            )
        return failed(f"`{r.default_branch}` is not protected at all", fix)
    gaps = [
        name
        for name, ok in (("force-push", protection.force_push_blocked), ("deletion", protection.deletion_blocked))
        if not ok
    ]
    if gaps:
        return failed(f"{truncate(gaps)} still allowed ({protection.source})", fix)
    return passed(f"Force-push and deletion blocked ({protection.source})", fix)


@check(
    id="branch/requires-pr",
    tier=Tier.REQUIRED,
    category=CATEGORY,
    title="Requires a pull request",
    description="Changes to the default branch must go through a pull request",
    needs=("meta", "admin?"),
)
def requires_pull_request(r: RepoData) -> CheckResult:
    fix = _rules_url(r)
    protection = protection_of(r)
    if protection is None:
        if (reason := _unreadable(r)) is not None:
            return unknown(reason, fix)
        return failed("Direct pushes to the default branch are allowed", fix)
    return verdict(
        protection.requires_pr,
        f"Pull request required ({protection.source})",
        "No pull-request requirement",
        fix_url=fix,
    )


@check(
    id="branch/requires-review",
    tier=Tier.REQUIRED,
    category=CATEGORY,
    title="Requires ≥1 approving review",
    description="A pull request needs at least one approving review to merge",
    needs=("meta", "admin?"),
)
def requires_review(r: RepoData) -> CheckResult:
    fix = _rules_url(r)
    protection = protection_of(r)
    if protection is None:
        if (reason := _unreadable(r)) is not None:
            return unknown(reason, fix)
        return failed("No review requirement", fix)
    if not protection.requires_pr:
        return failed("Pull requests are not required, so neither are reviews", fix)
    count = protection.review_count
    if count >= MIN_REVIEWS:
        return passed(f"{count} approving review{'s' if count > 1 else ''} required", fix)
    return failed("Pull requests are required but need 0 approving reviews", fix)


@check(
    id="branch/status-checks",
    tier=Tier.RECOMMENDED,
    category=CATEGORY,
    title="Requires status checks",
    description="At least one status check must pass before merging",
    needs=("meta", "admin?"),
)
def status_checks(r: RepoData) -> CheckResult:
    fix = _rules_url(r)
    protection = protection_of(r)
    if protection is None:
        if (reason := _unreadable(r)) is not None:
            return unknown(reason, fix)
        return failed("No required status checks", fix)
    if protection.status_checks:
        return passed(f"{len(protection.status_checks)} required: {truncate(protection.status_checks)}", fix)
    return failed("No status checks are required to merge", fix)


@check(
    id="branch/linear-history",
    tier=Tier.INFORMATIONAL,
    category=CATEGORY,
    title="Linear history required",
    description="Merge commits are not allowed on the default branch",
    needs=("meta", "admin?"),
)
def linear_history(r: RepoData) -> CheckResult:
    fix = _rules_url(r)
    protection = protection_of(r)
    if protection is None:
        if (reason := _unreadable(r)) is not None:
            return unknown(reason, fix)
        return warned("Not required", fix)
    return verdict(protection.linear_history, "Linear history required", "Merge commits allowed", fix_url=fix)
