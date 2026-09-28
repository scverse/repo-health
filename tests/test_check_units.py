"""Unit tests for the parsing that the checks lean on."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scverse_repo_health.checks._util import is_sha, iter_uses, load_yaml, precommit_repos, triggers
from scverse_repo_health.checks.branch import protection_of
from scverse_repo_health.checks.governance import days_since
from scverse_repo_health.checks.security import actions_pinned, dangerous_triggers
from scverse_repo_health.models import Category, RepoData, Status
from scverse_repo_health.sources.pypi import normalise, parse_san

SHA = "6518dfa1abde7379ea7255daf0ce09c23f2b4c94"


def repo_with_workflows(**workflows: str) -> RepoData:
    paths = {f".github/workflows/{name}.yaml": text for name, text in workflows.items()}
    return RepoData(
        name="demo",
        category=Category.OTHER,
        repo={"full_name": "scverse/demo", "default_branch": "main"},
        tree=[*paths, "pyproject.toml"],
        files=dict(paths),
        workflows=dict(paths),
    )


def test_is_sha():
    assert is_sha(SHA)
    assert not is_sha("v1.2.3")
    assert not is_sha(SHA[:39])


def test_triggers_survives_yaml_boolean_on():
    """YAML 1.1 parses a bare `on:` as True; the `on` block must still be found."""
    workflow = load_yaml("on:\n  push:\n    branches: [main]\njobs: {}\n")
    assert workflow is not None
    assert "push" in triggers(workflow)


def test_triggers_handles_a_scalar_and_a_list():
    assert "push" in triggers(load_yaml("on: push\njobs: {}\n"))
    assert set(triggers(load_yaml("on: [push, pull_request]\njobs: {}\n"))) == {"push", "pull_request"}


def test_iter_uses_finds_steps_and_reusable_workflows():
    repo = repo_with_workflows(
        ci=f"""
on: push
jobs:
  build:
    steps:
      - uses: actions/checkout@{SHA}
      - uses: astral-sh/setup-uv@v7
  call:
    uses: scverse/repo-health/.github/workflows/shared.yaml@main
""",
    )
    refs = {u.ref for u in iter_uses(repo)}
    assert f"actions/checkout@{SHA}" in refs
    assert "astral-sh/setup-uv@v7" in refs
    assert "scverse/repo-health/.github/workflows/shared.yaml@main" in refs


def test_iter_uses_falls_back_to_regex_on_broken_yaml():
    repo = repo_with_workflows(broken="jobs:\n  a:\n   steps:\n  - uses: actions/checkout@v5\n\t bad")
    assert {u.ref for u in iter_uses(repo)} == {"actions/checkout@v5"}


def test_actions_pinned_ignores_local_and_same_org_refs():
    repo = repo_with_workflows(
        ci=f"""
on: push
jobs:
  a:
    steps:
      - uses: ./.github/actions/setup
      - uses: actions/checkout@{SHA}
  b:
    uses: scverse/demo/.github/workflows/x.yaml@main
""",
    )
    repo.repo["full_name"] = "scverse/demo"
    result = actions_pinned(repo)
    assert result.status is Status.PASS
    assert "1 action reference" in result.detail


def test_actions_pinned_reports_the_offenders():
    repo = repo_with_workflows(ci="on: push\njobs:\n  a:\n    steps:\n      - uses: actions/checkout@v5\n")
    result = actions_pinned(repo)
    assert result.status is Status.FAIL
    assert "actions/checkout@v5" in result.detail


def test_dangerous_triggers_only_fails_when_pr_code_is_checked_out():
    safe = repo_with_workflows(
        label="on:\n  pull_request_target:\njobs:\n  a:\n    steps:\n      - uses: actions/labeler@v6\n"
    )
    assert dangerous_triggers(safe).status is Status.WARN

    unsafe = repo_with_workflows(
        build="""
on:
  pull_request_target:
jobs:
  a:
    steps:
      - uses: actions/checkout@v5
        with:
          ref: ${{ github.event.pull_request.head.sha }}
"""
    )
    assert dangerous_triggers(unsafe).status is Status.FAIL


def test_precommit_repos_reads_the_config():
    repo = RepoData(
        name="demo",
        files={".pre-commit-config.yaml": f"repos:\n  - repo: https://github.com/x/y\n    rev: {SHA}\n    hooks: []\n"},
        tree=[".pre-commit-config.yaml"],
    )
    entries = precommit_repos(repo)
    assert entries[0]["rev"] == SHA


# -- branch protection normalisation ------------------------------------------------------


def test_protection_of_reads_rulesets():
    repo = RepoData(
        name="demo",
        rulesets=[
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {"type": "pull_request", "parameters": {"required_approving_review_count": 2}},
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "pre-commit.ci - pr"}]},
            },
        ],
    )
    protection = protection_of(repo)
    assert protection is not None
    assert protection.source == "ruleset"
    assert protection.deletion_blocked and protection.force_push_blocked
    assert protection.requires_pr
    assert protection.review_count == 2
    assert protection.status_checks == ["pre-commit.ci - pr"]


def test_protection_of_falls_back_to_classic():
    repo = RepoData(
        name="demo",
        rulesets=[],
        classic_protection={
            "allow_force_pushes": {"enabled": False},
            "allow_deletions": {"enabled": True},
            "required_pull_request_reviews": {"required_approving_review_count": 1},
            "required_status_checks": {"contexts": ["ci"]},
        },
    )
    protection = protection_of(repo)
    assert protection is not None
    assert protection.source == "classic"
    assert protection.force_push_blocked
    assert not protection.deletion_blocked
    assert protection.review_count == 1


def test_protection_of_returns_none_when_nothing_is_known():
    assert protection_of(RepoData(name="demo")) is None


# -- Read the Docs ------------------------------------------------------------------------


def _repo_with_build(build: dict | None) -> RepoData:
    return RepoData(
        name="demo",
        tree=["pyproject.toml", ".readthedocs.yaml"],
        rtd={"slug": "demo", "stable_build": build, "home": "https://app.readthedocs.org/projects/demo/"},
    )


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        ({"state": "finished", "success": True, "version": "stable"}, Status.PASS),
        ({"state": "finished", "success": False, "version": "stable"}, Status.FAIL),
        # Caught mid-flight: RTD has not decided yet, so neither have we.
        ({"state": "building", "success": None, "version": "stable"}, Status.UNKNOWN),
        ({"state": "cloning", "success": None, "version": "stable"}, Status.UNKNOWN),
        # No `stable` version at all, or one that has never built: not a broken build.
        (None, Status.NA),
    ],
)
def test_rtd_build_does_not_call_an_unfinished_build_a_failure(build, expected):
    from scverse_repo_health.checks.docs import rtd_build

    assert rtd_build(_repo_with_build(build)).status is expected


# -- PyPI helpers ----------------------------------------------------------------------------


def test_parse_san_splits_a_sigstore_identity():
    parts = parse_san("https://github.com/scverse/scirpy/.github/workflows/release.yaml@refs/tags/v0.24.0")
    assert parts == {
        "repo": "scverse/scirpy",
        "workflow": ".github/workflows/release.yaml",
        "ref": "refs/tags/v0.24.0",
    }


def test_parse_san_rejects_anything_else():
    assert parse_san("https://example.com/whatever") is None


@pytest.mark.parametrize(("raw", "expected"), [("Scanpy", "scanpy"), ("scvi_tools", "scvi-tools"), ("a.b", "a-b")])
def test_normalise(raw, expected):
    assert normalise(raw) == expected


# -- misc --------------------------------------------------------------------------------------


def test_days_since_is_computed_against_a_given_now():
    now = datetime(2026, 7, 28, tzinfo=UTC)
    assert days_since("2026-07-18T00:00:00Z", now) == 10
    assert days_since(None, now) is None
    assert days_since("not a date", now) is None


# -- zizmor ------------------------------------------------------------------------------------


def _repo_with_audit(audit: dict | None, *, unavailable: str | None = None) -> RepoData:
    repo = RepoData(name="demo", tree=[".github/workflows/ci.yaml"], zizmor=audit)
    if unavailable:
        repo.unavailable["zizmor"] = unavailable
    return repo


def test_zizmor_clean_passes_on_an_empty_audit():
    from scverse_repo_health.checks.security import zizmor_clean

    result = zizmor_clean(_repo_with_audit({"count": 0, "findings": [], "inputs": 3, "online": True}))
    assert result.status is Status.PASS
    assert "3 files" in result.detail and "online" in result.detail


def test_zizmor_clean_reports_the_full_count_not_the_kept_findings():
    """`findings` is capped for size, so the number has to come from `count`."""
    from scverse_repo_health.checks.security import zizmor_clean

    audit = {
        "count": 42,
        "findings": [{"ident": "unpinned-uses"}, {"ident": "artipacked"}],
        "inputs": 5,
        "online": False,
    }
    result = zizmor_clean(_repo_with_audit(audit))
    assert result.status is Status.FAIL
    assert "42 findings" in result.detail
    assert "artipacked, unpinned-uses" in result.detail


@pytest.mark.parametrize(
    ("audit", "unavailable"),
    [(None, "zizmor is not installed"), (None, None)],
)
def test_zizmor_clean_is_unknown_when_the_audit_did_not_run(audit, unavailable):
    """A missing binary or a crashed audit is "we don't know", never a failing repo."""
    from scverse_repo_health.checks.security import zizmor_clean

    assert zizmor_clean(_repo_with_audit(audit, unavailable=unavailable)).status is Status.UNKNOWN


def test_zizmor_clean_does_not_apply_without_anything_to_audit():
    from scverse_repo_health.registry import REGISTRY

    check = REGISTRY.get("security/zizmor-clean")
    assert check is not None
    assert check.run(RepoData(name="demo", tree=["README.md"])).status is Status.NA


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        ([".github/workflows/ci.yml", "README.md"], [".github/workflows/ci.yml"]),
        (["nested/dir/action.yaml"], ["nested/dir/action.yaml"]),
        ([".github/dependabot.yml"], [".github/dependabot.yml"]),
        # A workflow-shaped name outside the workflows directory is not a workflow.
        (["docs/ci.yml", ".github/workflows/notes.md"], []),
    ],
)
def test_audit_inputs_collects_what_zizmor_collects(paths, expected):
    from scverse_repo_health.sources.zizmor import audit_inputs

    assert audit_inputs(paths) == expected


async def test_audit_runs_the_real_zizmor_and_parses_its_findings():
    """The one test that shells out: it pins the JSON shape we depend on."""
    from scverse_repo_health.sources.zizmor import audit

    result = await audit(
        {
            ".github/workflows/bad.yml": (
                "name: bad\non: [push]\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
                '    steps:\n      - run: echo "${{ github.event.issue.title }}"\n'
            )
        }
    )
    assert result["inputs"] == 1
    assert result["online"] is False
    assert result["count"] >= 1
    idents = {f["ident"] for f in result["findings"]}
    assert "template-injection" in idents, idents
    finding = next(f for f in result["findings"] if f["ident"] == "template-injection")
    assert finding["path"] == ".github/workflows/bad.yml"
    assert finding["severity"] and finding["url"].startswith("https://")


# -- packages.json -------------------------------------------------------------------------------


def _listed(category: str, **repo: object) -> RepoData:
    from scverse_repo_health.sources.scverse import category_of

    entry = {"name": "demo", "category": category, "project_home": "https://github.com/scverse/demo"}
    return RepoData(
        name="demo",
        category=category_of(entry),
        repo={"full_name": "scverse/demo", **repo},
        tree=["README.md"],
        package_entry=entry,
    )


def test_packages_json_applies_to_repos_that_are_not_python_packages():
    """The website, the template and the governance repo are `core-infrastructure` in the
    index and have no `pyproject.toml` — and the index is the only reason we know that."""
    from scverse_repo_health.registry import REGISTRY

    check = REGISTRY.get("template/packages-json")
    assert check is not None
    result = check.run(_listed("core-infrastructure"))
    assert result.status is Status.PASS
    assert "core-infrastructure" in result.detail


def test_packages_json_does_not_warn_about_ecosystem_packages():
    """`ecosystem` is what the index calls the repos the dashboard groups under "other";
    the two vocabularies differing is not a finding about the repo."""
    from scverse_repo_health.checks.template import listed_in_packages_json

    repo = _listed("ecosystem")
    assert repo.category is Category.OTHER
    assert listed_in_packages_json(repo).status is Status.PASS


def test_packages_json_warns_about_a_category_the_dashboard_cannot_map():
    from scverse_repo_health.checks.template import listed_in_packages_json

    result = listed_in_packages_json(_listed("core-framwork"))
    assert result.status is Status.WARN
    assert "core-framwork" in result.detail


def test_packages_json_warns_when_the_licenses_disagree():
    from scverse_repo_health.checks.template import listed_in_packages_json

    repo = _listed("ecosystem", license={"spdx_id": "MIT"})
    repo.package_entry["license"] = "BSD-3-Clause"
    result = listed_in_packages_json(repo)
    assert result.status is Status.WARN
    assert "BSD-3-Clause" in result.detail and "MIT" in result.detail


# -- SPEC 0 ------------------------------------------------------------------------------------

RELEASES = [
    {"cycle": "3.14", "releaseDate": "2025-10-07"},
    {"cycle": "3.13", "releaseDate": "2024-10-07"},
    {"cycle": "3.12", "releaseDate": "2023-10-02"},
    {"cycle": "3.11", "releaseDate": "2022-10-24"},
]


def _repo_requiring(requires: str | None) -> RepoData:
    line = f'requires-python = "{requires}"\n' if requires else ""
    text = f'[project]\nname = "demo"\n{line}'
    return RepoData(
        name="demo",
        tree=["pyproject.toml"],
        files={"pyproject.toml": text},
        python_releases=RELEASES,
    )


def test_spec0_minimum_python_moves_with_the_drop_date():
    """Python 3.12 was released on 2023-10-02, so SPEC 0 stops asking for it three years later."""
    from datetime import date

    from packaging.version import Version

    from scverse_repo_health.checks.packaging import spec0_minimum_python

    assert spec0_minimum_python(RELEASES, date(2026, 9, 30)) == Version("3.12")
    assert spec0_minimum_python(RELEASES, date(2026, 10, 3)) == Version("3.13")
    assert spec0_minimum_python([], date(2026, 9, 30)) is None


def test_spec0_python_measures_requires_python_against_that_minimum():
    from scverse_repo_health.checks.packaging import spec0_minimum_python, spec0_python

    wanted = spec0_minimum_python(RELEASES)
    assert wanted is not None

    assert spec0_python(_repo_requiring(f">={wanted}")).status is Status.PASS
    assert spec0_python(_repo_requiring(f">=3.{wanted.minor - 1}")).status is Status.FAIL
    assert spec0_python(_repo_requiring(None)).status is Status.FAIL


def test_spec0_python_is_unknown_without_release_dates():
    from scverse_repo_health.checks.packaging import spec0_python

    repo = _repo_requiring(">=3.9")
    repo.python_releases = None
    repo.unavailable["python_releases"] = "endoflife.date unreachable"

    assert spec0_python(repo).status is Status.UNKNOWN


# -- integration testing -------------------------------------------------------------------------


def test_parse_job_pulls_the_package_and_leg_out_of_a_matrix_job():
    from scverse_repo_health.sources.integration import parse_job

    assert parse_job("test (3.12, false, scanpy, scanpy2)") == ("scanpy", "3.12")
    assert parse_job("test (3.14, true, rapids-singlecell, rapids-cu13, true)") == ("rapids-singlecell", "3.14 pre")
    assert parse_job("Keepalive Workflow") is None


def test_summarise_groups_by_package_and_records_the_failing_legs():
    from scverse_repo_health.sources.integration import summarise

    runs = [
        {"name": "test (3.12, false, SnapATAC2)", "conclusion": "failure"},
        {"name": "test (3.14, true, SnapATAC2)", "conclusion": "failure"},
        {"name": "test (3.14, false, SnapATAC2)", "conclusion": "success"},
        {"name": "test (3.12, false, muon)", "conclusion": "success"},
        {"name": "Keepalive Workflow", "conclusion": "success"},
    ]
    results = summarise(runs)

    assert results["snapatac2"] == {
        "package": "SnapATAC2",
        "total": 3,
        "failed": ["3.12", "3.14 pre"],
        "url": "https://github.com/scverse/integration-testing/actions",
    }
    assert results["muon"]["failed"] == []


def test_upstream_tests_is_not_applicable_to_a_package_outside_the_matrix():
    from scverse_repo_health.checks.integration import upstream_tests
    from scverse_repo_health.registry import REGISTRY

    spec = REGISTRY.get("integration/upstream-tests")
    assert spec is not None
    assert spec.run(RepoData(name="demo")).status is Status.NA

    passing = RepoData(name="muon", integration={"package": "muon", "total": 3, "failed": []})
    assert upstream_tests(passing).status is Status.PASS

    failing = RepoData(name="snapatac2", integration={"package": "SnapATAC2", "total": 3, "failed": ["3.12"]})
    assert upstream_tests(failing).status is Status.FAIL


# -- GPL-free dependencies -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dist", "expected"),
    [
        ({"license_expression": "GPL-3.0-or-later"}, "GPL-3.0-or-later"),
        ({"license_expression": "AGPL-3.0-only"}, "AGPL-3.0-only"),
        ({"license_expression": "LGPL-2.1-or-later"}, None),
        ({"license_expression": "MIT OR GPL-2.0-only"}, None),
        (
            {"classifiers": ["License :: OSI Approved :: GNU General Public License v3 (GPLv3)"]},
            "GNU General Public License v3 (GPLv3)",
        ),
        ({"classifiers": ["License :: OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)"]}, None),
        (
            {"classifiers": ["License :: OSI Approved", "License :: OSI Approved :: BSD License"], "license": "GPL"},
            None,
        ),
        ({"classifiers": ["License :: OSI Approved"], "license": "GNU GPL v2"}, "GNU GPL v2"),
        ({"license": "BSD-3-Clause"}, None),
        ({}, None),
    ],
)
def test_gpl_license_reads_expression_then_classifiers_then_text(dist, expected):
    from scverse_repo_health.checks.governance import gpl_license

    assert gpl_license(dist) == expected


def _repo_depending_on(*dependencies: dict, spdx: str = "BSD-3-Clause", dynamic: bool = False) -> RepoData:
    toml = '[project]\nname = "demo"\ndynamic = ["dependencies"]\n' if dynamic else '[project]\nname = "demo"\n'
    return RepoData(
        name="demo",
        repo={"full_name": "scverse/demo", "license": {"spdx_id": spdx}},
        tree=["pyproject.toml"],
        files={"pyproject.toml": toml},
        dependencies=[{"via": None, **d} for d in dependencies],
    )


def test_gpl_free_names_the_gpl_dependency_and_the_direct_one_behind_it():
    from scverse_repo_health.checks.governance import gpl_free

    clean = _repo_depending_on({"name": "numpy", "license_expression": "BSD-3-Clause"})
    assert gpl_free(clean).status is Status.PASS

    tainted = _repo_depending_on(
        {"name": "helper", "license_expression": "MIT"},
        {"name": "shim", "license_expression": "MIT", "via": "helper"},
        {"name": "igraph", "license": "GNU General Public License (GPL)", "via": "shim"},
    )
    result = gpl_free(tainted)
    assert result.status is Status.FAIL
    assert result.detail == "`igraph` (GNU General Public License (GPL)) via `helper`"


def test_gpl_free_does_not_apply_to_a_gpl_repo():
    from scverse_repo_health.registry import REGISTRY

    spec = REGISTRY.get("governance/gpl-free")
    assert spec is not None
    gpl_dep = {"name": "igraph", "license_expression": "GPL-2.0-or-later"}
    assert spec.run(_repo_depending_on(gpl_dep, spdx="GPL-3.0")).status is Status.NA
    assert spec.run(_repo_depending_on(gpl_dep, spdx="LGPL-3.0")).status is Status.FAIL


def test_gpl_free_is_unknown_when_the_dependencies_are_not_known():
    from scverse_repo_health.checks.governance import gpl_free

    skipped = _repo_depending_on()
    skipped.unavailable["dependencies"] = "PyPI lookups skipped"
    assert gpl_free(skipped).status is Status.UNKNOWN
    assert gpl_free(_repo_depending_on(dynamic=True)).status is Status.UNKNOWN


async def test_dependencies_walks_transitively_and_skips_optional_extras(tmp_path):
    from scverse_repo_health.sources.pypi import PyPIClient

    index = {
        "a": ["b[fast]", "pytest; extra == 'test'"],
        "b": ["c", "d; extra == 'fast'", "e; extra == 'slow'"],
        "c": ["a"],
        "d": [],
        "e": [],
    }

    async def get(path: str, accept: str | None = None) -> dict | None:
        name = path.split("/")[2]
        return {"info": {"name": name, "license_expression": "MIT", "requires_dist": index[name]}}

    async with PyPIClient(cache=False, cache_path=tmp_path) as pypi:
        pypi._get = get
        found = await pypi.dependencies(["a", "not a requirement!"])

    assert {d["name"]: d["via"] for d in found} == {"a": None, "b": "a", "c": "b", "d": "b"}
    assert "requires_dist" not in found[0]
