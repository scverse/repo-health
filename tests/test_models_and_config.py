from __future__ import annotations

import json

import pytest
import yaml

from scverse_repo_health.collect import Results, pypi_name_for, select_repos
from scverse_repo_health.config import CoreDevs, ReposConfig, config_dir
from scverse_repo_health.models import Category, CheckResult, RepoData, RepoReport, Status, Tier
from scverse_repo_health.registry import run_all


def test_repo_data_round_trips(scirpy: RepoData):
    again = RepoData.from_dict(json.loads(json.dumps(scirpy.to_dict())))
    assert again.name == scirpy.name
    assert again.category is scirpy.category
    assert again.tree == scirpy.tree
    assert again.unavailable == scirpy.unavailable


def test_results_round_trip(tmp_path, scirpy: RepoData):
    results = Results(
        generated="2026-07-28T00:00:00+00:00",
        org="scverse",
        reports=[RepoReport(repo=scirpy, results=run_all(scirpy))],
    )
    path = tmp_path / "results.json"
    results.write(path)
    again = Results.read(path)
    assert again.generated == results.generated
    assert again.checks == results.checks
    assert again.reports[0].repo.name == "scirpy"
    assert again.reports[0].results.keys() == results.reports[0].results.keys()


def test_check_result_drops_empty_fields():
    assert CheckResult(Status.PASS).to_dict() == {"status": "pass"}
    assert CheckResult(Status.FAIL, "why", "http://x").to_dict() == {
        "status": "fail",
        "detail": "why",
        "fix_url": "http://x",
    }


def test_score_ignores_na_and_unknown():
    report = RepoReport(
        repo=RepoData(name="x"),
        results={
            "governance/license": CheckResult(Status.PASS),
            "branch/protected": CheckResult(Status.UNKNOWN),
            "branch/requires-pr": CheckResult(Status.FAIL),
            "governance/citation": CheckResult(Status.PASS),  # informational, not scored
        },
    )
    assert report.score(Tier.REQUIRED) == (1, 2)


def test_is_unavailable_returns_the_first_reason():
    repo = RepoData(name="x", unavailable={"classic_protection": "403"})
    assert repo.is_unavailable("rulesets", "classic_protection") == "403"
    assert repo.is_unavailable("rulesets") is None


# -- config ------------------------------------------------------------------------------


def test_shipped_config_parses():
    config = ReposConfig.load()
    assert config.org == "scverse"
    assert config.exclusions
    assert all(reason for reason in config.exclusions.values()), "every exclusion needs a reason"
    assert config.include


def test_shipped_core_devs_parse():
    devs = CoreDevs.load()
    assert len(devs.devs) >= 15
    assert devs.is_core_dev("flying-sheep")
    assert devs.is_core_dev("Flying-Sheep"), "logins compare case-insensitively"
    assert not devs.is_core_dev("dependabot")
    assert devs.logins == sorted(devs.devs)


def test_core_devs_file_needs_only_login_and_name():
    """The config is a flat list; anything else in it is ignored, not required."""
    raw = yaml.safe_load((config_dir() / "core_devs.yaml").read_text(encoding="utf-8"))
    assert set(raw) == {"core_devs"}
    assert all("login" in entry for entry in raw["core_devs"])


def test_waivers_are_per_repo():
    config = ReposConfig(waivers={"demo": {"security/zizmor": "handled elsewhere"}})
    assert config.waivers["demo"]["security/zizmor"]


@pytest.mark.parametrize(
    ("repo", "expected"),
    [
        ({"name": "a", "private": True}, "private"),
        ({"name": "b", "fork": True}, "fork"),
        ({"name": "event-template"}, "Template for event repositories"),
        ({"name": "c", "archived": True}, "archived"),
    ],
)
def test_select_repos_explains_every_exclusion(repo, expected):
    from scverse_repo_health.collect import CollectOptions

    selected, excluded = select_repos([repo], ReposConfig.load(), CollectOptions())
    assert selected == []
    assert excluded[0]["reason"] == expected


def test_check_can_still_inspect_an_excluded_repo():
    """`repo-health check <name>` must work on anything, dashboard or not."""
    from scverse_repo_health.collect import CollectOptions

    opts = CollectOptions(ignore_exclusions=True)
    selected, excluded = select_repos([{"name": "event-template"}], ReposConfig.load(), opts)
    assert [r["name"] for r in selected] == ["event-template"]
    assert excluded == []


def test_select_repos_keeps_a_normal_repo():
    from scverse_repo_health.collect import CollectOptions

    selected, excluded = select_repos([{"name": "scanpy"}], ReposConfig.load(), CollectOptions())
    assert [r["name"] for r in selected] == ["scanpy"]
    assert excluded == []


def test_pypi_name_prefers_the_config_override():
    repo = RepoData(name="demo", files={"pyproject.toml": '[project]\nname = "from-toml"\n'})
    assert pypi_name_for(repo, ReposConfig()) == "from-toml"
    assert pypi_name_for(repo, ReposConfig(pypi_names={"demo": "override"})) == "override"


def test_pypi_name_records_broken_toml():
    repo = RepoData(name="demo", files={"pyproject.toml": "[project\nname ="})
    assert pypi_name_for(repo, ReposConfig()) is None
    assert repo.errors


async def test_pypi_outage_reads_as_unknown_not_missing():
    """An upstream wobble must not be reported as "this package is not on PyPI"."""
    import httpx

    from scverse_repo_health.collect import CollectOptions, _enrich
    from scverse_repo_health.models import Status
    from scverse_repo_health.registry import run_all

    class ExplodingPyPI:
        async def project(self, name):  # noqa: ARG002
            msg = "pypi.org is down"
            raise httpx.ConnectError(msg)

    class SilentRTD:
        async def project(self, slug):  # noqa: ARG002
            return None

    class NoopGitHub:
        async def try_get(self, *_args, **_kwargs):
            return None, None

    repo = RepoData(
        name="demo",
        repo={"full_name": "scverse/demo"},
        tree=["pyproject.toml"],
        files={"pyproject.toml": '[project]\nname = "demo"\n'},
    )
    await _enrich(NoopGitHub(), ExplodingPyPI(), SilentRTD(), repo, ReposConfig(), {}, CollectOptions())

    assert repo.pypi is None
    assert "pypi" in repo.unavailable
    assert run_all(repo)["packaging/pypi-org"].status is Status.UNKNOWN


def test_category_from_packages_json():
    from scverse_repo_health.sources.scverse import category_of, index_by_repo

    packages = [
        {"name": "scanpy", "project_home": "https://github.com/scverse/scanpy", "category": "core-framework"},
        {"name": "elsewhere", "project_home": "https://github.com/other/thing", "category": "core-framework"},
        {"name": "eco", "project_home": "https://github.com/scverse/eco", "category": "ecosystem"},
    ]
    index = index_by_repo(packages)
    assert set(index) == {"scanpy", "eco"}
    assert category_of(index["scanpy"]) is Category.CORE_FRAMEWORK
    assert category_of(index["eco"]) is Category.OTHER, "unknown categories fall through to Other"
    assert category_of(None) is Category.OTHER
