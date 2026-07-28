"""Check outcomes against data captured from real repositories.

Two repos were picked because between them they hit every awkward branch:

* **scirpy** — classic branch protection, mutable releases, docs already on
  ``scirpy.scverse.org``, and a ``.cruft.json`` whose ``template`` is a ``/tmp`` path.
* **scanpy** — rulesets (with ``required_approving_review_count: 0``), docs still
  advertised on ``readthedocs.io``, an RTD project pointing at ``theislab/scanpy``,
  and completely unpinned actions.

The fixtures were captured without credentials, so the admin-only checks are UNKNOWN —
which is itself the behaviour we want to pin down.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from scverse_repo_health.models import Status

if TYPE_CHECKING:
    from scverse_repo_health.models import CheckResult


def status(results: dict[str, CheckResult], check_id: str) -> Status:
    return results[check_id].status


# -- scirpy -----------------------------------------------------------------------------


def test_scirpy_template_survives_a_tmp_path(scirpy_results):
    """`template: "/tmp/tmpXXXX"` must not read as "not from the template"."""
    result = scirpy_results["template/cruft"]
    assert result.status is Status.PASS
    assert status(scirpy_results, "template/up-to-date") is Status.PASS


@pytest.mark.parametrize(
    ("check_id", "expected"),
    [
        ("docs/rtd-linked", Status.PASS),
        ("docs/rtd-core-devs", Status.PASS),
        ("docs/scverse-domain", Status.PASS),
        ("packaging/pypi-org", Status.PASS),
        ("packaging/trusted-publishing", Status.PASS),
        ("packaging/release-workflow", Status.PASS),
        ("security/actions-pinned", Status.PASS),
        ("security/zizmor-clean", Status.PASS),
        ("security/persist-credentials", Status.PASS),
        ("governance/license", Status.PASS),
        # scirpy publishes mutable releases and pins pre-commit revs to tags.
        ("packaging/releases-immutable", Status.FAIL),
        ("security/precommit-pinned", Status.FAIL),
        ("security/dependabot", Status.FAIL),
    ],
)
def test_scirpy_expected_outcomes(scirpy_results, check_id, expected):
    assert status(scirpy_results, check_id) is expected


def test_scirpy_docs_domain_is_compliant(scirpy_results):
    assert "scirpy.scverse.org" in scirpy_results["docs/scverse-domain"].detail


def test_scirpy_trusted_publishing_names_the_workflow(scirpy_results):
    detail = scirpy_results["packaging/trusted-publishing"].detail
    assert "scverse/scirpy" in detail
    assert "release.yaml" in detail


def test_scirpy_classic_protection_is_unknown_not_failed(scirpy_results):
    """Without administration:read we must say "don't know", never "not protected"."""
    for check_id in ("branch/protected", "branch/requires-pr", "branch/requires-review"):
        assert status(scirpy_results, check_id) is Status.UNKNOWN


# -- scanpy -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("check_id", "expected"),
    [
        ("template/cruft", Status.PASS),
        ("template/up-to-date", Status.FAIL),
        ("template/pre-commit-ci", Status.PASS),
        ("branch/protected", Status.PASS),
        ("branch/requires-pr", Status.PASS),
        ("branch/requires-review", Status.FAIL),
        ("branch/status-checks", Status.PASS),
        ("docs/rtd-linked", Status.FAIL),
        ("docs/rtd-core-devs", Status.FAIL),
        ("docs/scverse-domain", Status.WARN),
        ("security/actions-pinned", Status.FAIL),
        ("security/zizmor-clean", Status.FAIL),
        ("security/workflow-permissions", Status.FAIL),
        ("packaging/pypi-environment", Status.PASS),
        ("packaging/version-sync", Status.PASS),
    ],
)
def test_scanpy_expected_outcomes(scanpy_results, check_id, expected):
    assert status(scanpy_results, check_id) is expected


def test_scanpy_rulesets_are_read_without_admin(scanpy, scanpy_results):
    """Rulesets come back on a plain read token, so these are real answers."""
    assert scanpy.rulesets
    assert "ruleset" in scanpy_results["branch/protected"].detail
    assert "0 approving reviews" in scanpy_results["branch/requires-review"].detail


def test_scanpy_rtd_slug_collision_is_caught(scanpy_results):
    detail = scanpy_results["docs/rtd-linked"].detail
    assert "theislab/scanpy" in detail


def test_scanpy_template_lag_is_quantified(scanpy_results):
    assert "releases behind" in scanpy_results["template/up-to-date"].detail


# -- shared invariants ---------------------------------------------------------------------


@pytest.mark.parametrize("name", ["scirpy_results", "scanpy_results"])
def test_failures_always_offer_a_fix_link(name, request):
    results = request.getfixturevalue(name)
    missing = [check_id for check_id, result in results.items() if result.status is Status.FAIL and not result.fix_url]
    assert missing == [], f"failing checks with no remediation link: {missing}"


@pytest.mark.parametrize("name", ["scirpy_results", "scanpy_results"])
def test_every_result_has_a_detail(name, request):
    results = request.getfixturevalue(name)
    assert all(r.detail for r in results.values())


@pytest.mark.parametrize("name", ["scirpy", "scanpy"])
def test_no_check_raised_on_the_fixtures(name, request):
    repo = request.getfixturevalue(name)
    assert repo.errors == []
