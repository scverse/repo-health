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
            {"type": "required_linear_history"},
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
    assert protection.linear_history


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
        rtd={"slug": "demo", "latest_build": build, "home": "https://app.readthedocs.org/projects/demo/"},
    )


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        ({"state": "finished", "success": True, "version": "latest"}, Status.PASS),
        ({"state": "finished", "success": False, "version": "latest"}, Status.FAIL),
        # Caught mid-flight: RTD has not decided yet, so neither have we.
        ({"state": "building", "success": None, "version": "724"}, Status.UNKNOWN),
        ({"state": "cloning", "success": None, "version": "724"}, Status.UNKNOWN),
        (None, Status.UNKNOWN),
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
