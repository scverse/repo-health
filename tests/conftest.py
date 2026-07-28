from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scverse_repo_health.collect import Results
from scverse_repo_health.models import Category, RepoData, RepoReport
from scverse_repo_health.registry import run_all

if TYPE_CHECKING:
    from scverse_repo_health.models import CheckResult

FIXTURES = Path(__file__).parent / "fixtures"


def load_repo(name: str) -> RepoData:
    """A ``RepoData`` captured from the real repo by ``scripts/capture_fixture.py``."""
    return RepoData.from_dict(json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8")))


def results_for(name: str) -> dict[str, CheckResult]:
    return run_all(load_repo(name))


@pytest.fixture(scope="session")
def scirpy() -> RepoData:
    """Classic branch protection, mutable releases, compliant docs domain."""
    return load_repo("scirpy")


@pytest.fixture(scope="session")
def scanpy() -> RepoData:
    """Rulesets requiring 0 approvals, docs still on readthedocs.io, unpinned actions."""
    return load_repo("scanpy")


@pytest.fixture(scope="session")
def scirpy_results(scirpy: RepoData) -> dict[str, CheckResult]:
    return run_all(scirpy)


@pytest.fixture(scope="session")
def scanpy_results(scanpy: RepoData) -> dict[str, CheckResult]:
    return run_all(scanpy)


@pytest.fixture
def bare() -> RepoData:
    """A repo with nothing in it — the "mostly N/A" case, e.g. a hackathon repo."""
    return RepoData(
        name="2026_04_hackathon_padua",
        category=Category.OTHER,
        repo={
            "name": "2026_04_hackathon_padua",
            "full_name": "scverse/2026_04_hackathon_padua",
            "html_url": "https://github.com/scverse/2026_04_hackathon_padua",
            "default_branch": "main",
            "description": "",
            "homepage": None,
            "topics": [],
            "license": None,
            "pushed_at": "2026-05-01T00:00:00Z",
        },
        tree=["README.md", "notebooks/day1.ipynb"],
    )


def report_for(repo: RepoData) -> RepoReport:
    return RepoReport(repo=repo, results=run_all(repo))


def results_bundle(*repos: RepoData) -> Results:
    return Results(
        generated="2026-07-28T00:00:00+00:00",
        org="scverse",
        reports=[report_for(r) for r in repos],
        excluded=[{"repo": "event-template", "reason": "Template for event repositories"}],
        template={"repo": "scverse/cookiecutter-scverse", "latest": {"tag": "v0.8.0", "url": "https://x"}},
        meta={"auth": "anonymous"},
    )
