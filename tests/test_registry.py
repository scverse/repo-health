from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from scverse_repo_health.models import CheckResult, Status, Tier
from scverse_repo_health.registry import CHECK_CATEGORIES, NEEDS, REGISTRY, Check, run_all

if TYPE_CHECKING:
    from scverse_repo_health.models import RepoData


def test_catalogue_is_populated():
    assert len(REGISTRY) > 30


@pytest.mark.parametrize("spec", REGISTRY.all(), ids=lambda s: s.id)
def test_every_check_is_fully_declared(spec: Check):
    assert "/" in spec.id, "ids are namespaced, e.g. security/actions-pinned"
    assert isinstance(spec.tier, Tier)
    assert spec.category in CHECK_CATEGORIES
    assert spec.title
    assert spec.description
    assert set(spec.needs) <= set(NEEDS)


#: What a 13rem header clears for a 45° label once the tier badge is subtracted: 13rem × √2
#: ≈ 260pt of label, ~26 of them the badge, the rest at the ~6.3pt an average character of
#: mixed-case prose takes at 0.85rem. Past this, `--head-height` in `style.css` has to grow
#: or the dashboard's column labels start being cut off by the group-title band above them.
#: Being a character count this is only a proxy — a title in caps would still overrun it.
MAX_TITLE = 36


@pytest.mark.parametrize("spec", REGISTRY.all(), ids=lambda s: s.id)
def test_check_titles_fit_the_rotated_header(spec: Check):
    assert len(spec.title) <= MAX_TITLE, f"{spec.title!r} is {len(spec.title)} characters"


def test_each_group_starts_with_a_required_check():
    """The dashboard draws each group's divider on the group's first column, so that column
    has to be one that survives the "required checks only" filter."""
    for category, checks in REGISTRY.by_category().items():
        assert checks[0].tier is Tier.REQUIRED, f"{category} starts with {checks[0].id}"


def test_ids_are_unique_and_ordered_by_category():
    ids = [c.id for c in REGISTRY.all()]
    assert len(ids) == len(set(ids))
    seen = [c.category for c in REGISTRY.all()]
    positions = [CHECK_CATEGORIES.index(c) for c in seen]
    assert positions == sorted(positions)


def test_readme_catalogue_matches_the_registry():
    """The README lists every check; keep the two from drifting apart."""
    import re
    from pathlib import Path

    readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| \*\*([Rri])\*\* \| `([a-z0-9-]+/[a-z0-9-]+)` \|", readme, re.MULTILINE)
    documented = {check_id: tier for tier, check_id in rows}
    assert len(documented) == len(rows), "a check is listed twice in the README"

    registered = {c.id: c.tier.short for c in REGISTRY.all()}
    assert set(documented) == set(registered), "README and registry disagree on which checks exist"
    assert documented == registered, "README and registry disagree on a tier"
    assert f"{len(registered)} checks in six groups" in readme, "the count in the README is stale"


def test_run_all_returns_one_result_per_check(bare: RepoData):
    results = run_all(bare)
    assert set(results) == {c.id for c in REGISTRY.all()}
    assert all(isinstance(v, CheckResult) for v in results.values())


def test_bare_repo_is_mostly_not_applicable(bare: RepoData):
    """A hackathon repo should be quietly N/A, not a wall of red."""
    results = run_all(bare)
    na = sum(1 for r in results.values() if r.status is Status.NA)
    assert na > len(results) / 3
    # Nothing raised: an exception would surface as UNKNOWN with a traceback in `errors`.
    assert bare.errors == []


def test_a_crashing_check_becomes_unknown(bare: RepoData):
    def boom(_repo: RepoData) -> CheckResult:
        msg = "nope"
        raise RuntimeError(msg)

    spec = Check(id="x/boom", tier=Tier.REQUIRED, category="Template", title="Boom", fn=boom)
    result = spec.run(bare)
    assert result.status is Status.UNKNOWN
    assert "RuntimeError" in result.detail
    assert bare.errors


def test_waivers_render_as_not_applicable(bare: RepoData):
    bare.waivers = {"governance/license": "Not a software project"}
    result = run_all(bare)["governance/license"]
    assert result.status is Status.NA
    assert "Not a software project" in result.detail


def test_unknown_needs_token_is_rejected():
    from scverse_repo_health.registry import Registry

    registry = Registry()
    with pytest.raises(ValueError, match="unknown needs"):
        registry.add(
            Check(
                id="x/y",
                tier=Tier.REQUIRED,
                category="Template",
                title="t",
                fn=lambda _r: CheckResult(Status.PASS),
                needs=("telepathy",),
            )
        )
