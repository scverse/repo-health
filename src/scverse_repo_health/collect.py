"""Orchestration: fetch everything, run every check, produce ``results.json``.

This is the only module allowed to do I/O on behalf of checks. The JSON artifact it
writes is the contract with the renderer.
"""

from __future__ import annotations

import asyncio
import json
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ._log import log
from .config import CoreDevs, ReposConfig
from .models import CATEGORY_ORDER, RepoData, RepoReport, Status
from .registry import REGISTRY, run_all
from .sources import scverse
from .sources.github import GitHubClient, resolve_token
from .sources.pypi import PyPIClient
from .sources.readthedocs import RTDClient

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any, Self

    from .models import Category

SCHEMA_VERSION = 1
TEMPLATE_REPO = "scverse/cookiecutter-scverse"


class CollectionFailedError(Exception):
    """At least one repository could not be collected.

    Raised by the CLI *after* the artifact has been written, so a transient failure on
    one repo still produces a dashboard while CI goes red and someone looks at it.
    """


#: Blobs worth fetching, beyond the workflows. Only paths present in the tree are read.
INTERESTING_FILES = [
    ".cruft.json",
    "pyproject.toml",
    ".pre-commit-config.yaml",
    ".pre-commit-config.yml",
    ".readthedocs.yaml",
    ".readthedocs.yml",
    ".github/dependabot.yml",
    ".github/dependabot.yaml",
    ".codecov.yaml",
    ".codecov.yml",
    "codecov.yml",
]
WORKFLOW_LIMIT = 25


@dataclass(slots=True)
class CollectOptions:
    org: str = "scverse"
    repos: list[str] = field(default_factory=list)
    include_archived: bool = False
    cache: bool = True
    concurrency: int = 8
    skip_pypi: bool = False
    skip_rtd: bool = False
    #: Collect even repos listed under `exclusions:` — for `repo-health check <name>`.
    ignore_exclusions: bool = False


def catalogue() -> list[dict[str, Any]]:
    """The registry, flattened into plain dicts for ``results.json``."""
    return [
        {
            "id": c.id,
            "tier": str(c.tier),
            "category": c.category,
            "title": c.title,
            "description": c.description,
            "needs": list(c.needs),
        }
        for c in REGISTRY.all()
    ]


@dataclass
class Results:
    """Everything ``render`` needs. Serialised as ``results.json``."""

    generated: str
    org: str
    reports: list[RepoReport] = field(default_factory=list)
    excluded: list[dict[str, str]] = field(default_factory=list)
    template: dict[str, Any] | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    #: The check catalogue as it was when this file was written. Rendering uses this
    #: rather than the live registry, so an older results.json still renders correctly.
    checks: list[dict[str, Any]] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.checks:
            self.checks = catalogue()

    def to_dict(self, *, slim: bool = True) -> dict[str, Any]:
        """The published artifact. ``slim=False`` keeps the raw payloads, for debugging."""
        template = self.template
        if slim and template:
            # The full release list is only needed while collecting.
            template = {k: v for k, v in template.items() if k != "releases"}
        return {
            "schema_version": self.schema_version,
            "generated": self.generated,
            "org": self.org,
            "template": template,
            "meta": self.meta,
            "checks": self.checks,
            "repos": [r.to_dict(slim=slim) for r in self.reports],
            "excluded": self.excluded,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        return cls(
            generated=d["generated"],
            org=d.get("org", "scverse"),
            reports=[RepoReport.from_dict(r) for r in d.get("repos", [])],
            excluded=d.get("excluded", []),
            template=d.get("template"),
            meta=d.get("meta", {}),
            checks=d.get("checks") or [],
            schema_version=d.get("schema_version", SCHEMA_VERSION),
        )

    def write(self, path: Path, *, slim: bool = True) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(self.to_dict(slim=slim), indent=1, sort_keys=False)
        path.write_text(body + "\n", encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> Self:
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def sorted_reports(self) -> list[RepoReport]:
        return sorted(
            self.reports,
            key=lambda r: (CATEGORY_ORDER.index(r.repo.category), r.repo.archived, r.repo.name.lower()),
        )


# -- template ---------------------------------------------------------------------------


async def fetch_template(gh: GitHubClient) -> dict[str, Any]:
    """Latest ``cookiecutter-scverse`` release and its commit, shared across all repos."""
    releases, tags = await asyncio.gather(
        gh.paginate(f"/repos/{TEMPLATE_REPO}/releases", limit=100),
        gh.paginate(f"/repos/{TEMPLATE_REPO}/tags", limit=100),
    )
    sha_of = {t["name"]: t["commit"]["sha"] for t in tags}
    published = [
        {
            "tag": r["tag_name"],
            "commit": sha_of.get(r["tag_name"]),
            "published_at": r.get("published_at"),
            "url": r.get("html_url"),
        }
        for r in releases
        if not r.get("draft")
    ]
    latest = published[0] if published else None
    return {"repo": TEMPLATE_REPO, "latest": latest, "releases": published}


async def template_position(gh: GitHubClient, template: dict[str, Any], commit: str | None) -> dict[str, Any] | None:
    """Where ``commit`` sits relative to the latest template release."""
    latest = template.get("latest")
    if not commit or not latest or not latest.get("commit"):
        return None
    releases: list[dict[str, Any]] = template.get("releases") or []
    if (index := next((i for i, r in enumerate(releases) if r.get("commit") == commit), None)) is not None:
        return {"releases_behind": index, "release": releases[index]["tag"], "latest": latest["tag"]}
    # Not a release commit — ask GitHub how far apart the two commits are.
    data, _reason = await gh.try_get(f"/repos/{TEMPLATE_REPO}/compare/{commit}...{latest['commit']}")
    if not data:
        return {"releases_behind": None, "latest": latest["tag"], "unknown": True}
    behind = data.get("ahead_by", 0)  # commits the template has that this repo does not
    newer = sum(1 for r in releases if r.get("published_at") and _is_after(r, data))
    return {
        "releases_behind": newer or None,
        "commits_behind": behind,
        "latest": latest["tag"],
        "status": data.get("status"),
    }


def _is_after(release: dict[str, Any], comparison: dict[str, Any]) -> bool:
    """True if ``release`` is among the commits the template is ahead by."""
    shas = {c["sha"] for c in comparison.get("commits") or []}
    return release.get("commit") in shas


# -- per-repo fetching --------------------------------------------------------------------


async def fetch_repo(  # noqa: C901 - one flat list of endpoint calls
    gh: GitHubClient,
    repo_obj: dict[str, Any],
    *,
    category: Category,
    package_entry: dict[str, Any] | None,
) -> RepoData:
    name = repo_obj["name"]
    full = repo_obj["full_name"]
    branch = repo_obj.get("default_branch") or "main"
    data = RepoData(name=name, category=category, repo=repo_obj, package_entry=package_entry)

    tree, tree_reason = await gh.try_get(f"/repos/{full}/git/trees/{branch}", {"recursive": "1"})
    if tree_reason:
        data.unavailable["tree"] = tree_reason
    elif tree:
        data.tree = sorted(e["path"] for e in tree.get("tree", []) if e.get("type") == "blob")
        if tree.get("truncated"):
            data.errors.append("git tree truncated; file-based checks may be incomplete")

    wanted = [p for p in INTERESTING_FILES if p in data.tree]
    workflow_paths = [p for p in data.tree if p.startswith(".github/workflows/") and p.endswith((".yml", ".yaml"))][
        :WORKFLOW_LIMIT
    ]
    blobs = await asyncio.gather(*(gh.file(full, p, branch) for p in [*wanted, *workflow_paths]))
    for path, text in zip([*wanted, *workflow_paths], blobs, strict=True):
        if text is not None:
            data.files[path] = text
    data.workflows = {p: data.files[p] for p in workflow_paths if p in data.files}

    endpoints: dict[str, tuple[str, dict[str, Any] | None, bool]] = {
        "rulesets": (f"/repos/{full}/rules/branches/{branch}", None, False),
        "releases": (f"/repos/{full}/releases", {"per_page": 10}, False),
        "community_profile": (f"/repos/{full}/community/profile", None, False),
        "immutable_releases": (f"/repos/{full}/immutable-releases", None, True),
        "environments": (f"/repos/{full}/environments", None, True),
        "actions_permissions": (f"/repos/{full}/actions/permissions/workflow", None, True),
        "private_vulnerability_reporting": (f"/repos/{full}/private-vulnerability-reporting", None, True),
        "dependabot_alerts": (f"/repos/{full}/dependabot/alerts", {"state": "open", "per_page": 100}, True),
        "check_runs": (f"/repos/{full}/commits/{branch}/check-runs", {"per_page": 100}, False),
        "full_repo": (f"/repos/{full}", None, False),
    }
    fetched = await asyncio.gather(
        *(gh.try_get(path, params, privileged=priv) for path, params, priv in endpoints.values())
    )
    results = dict(zip(endpoints, fetched, strict=True))

    for key, (_value, reason) in results.items():
        if reason:
            data.unavailable[key] = reason

    data.rulesets = results["rulesets"][0] if isinstance(results["rulesets"][0], list) else None
    data.releases = results["releases"][0] or []
    data.community_profile = results["community_profile"][0]
    data.immutable_releases = results["immutable_releases"][0]
    data.environments = _environments(results["environments"][0])
    data.actions_permissions = results["actions_permissions"][0]
    data.private_vulnerability_reporting = results["private_vulnerability_reporting"][0]
    data.dependabot_alerts = results["dependabot_alerts"][0]
    if (runs := results["check_runs"][0]) and isinstance(runs, dict):
        data.check_runs = sorted({r.get("name", "") for r in runs.get("check_runs") or []})
    if (full_obj := results["full_repo"][0]) and isinstance(full_obj, dict):
        # The org listing omits `security_and_analysis`; the full object carries it for
        # callers with administration:read.
        data.repo = {**repo_obj, **full_obj}

    # Classic branch protection is only interesting when there is no ruleset covering the
    # default branch — and it needs administration:read, so we do not ask unless we must.
    if not data.rulesets and "rulesets" not in data.unavailable:
        protection, reason = await gh.try_get(f"/repos/{full}/branches/{branch}/protection", privileged=True)
        data.classic_protection = protection
        if reason:
            data.unavailable["classic_protection"] = reason

    return data


def _environments(payload: Any) -> list[dict[str, Any]] | None:
    if isinstance(payload, dict):
        return payload.get("environments") or []
    return payload if isinstance(payload, list) else None


def pypi_name_for(data: RepoData, config: ReposConfig) -> str | None:
    """Distribution name: explicit override, else ``[project] name``, else no PyPI check."""
    if override := config.pypi_names.get(data.name):
        return override
    if (text := data.file("pyproject.toml")) is not None:
        try:
            parsed = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            data.errors.append(f"pyproject.toml is not valid TOML: {exc}")
            return None
        if name := (parsed.get("project") or {}).get("name"):
            return str(name)
    return None


# -- top level ------------------------------------------------------------------------------


async def collect(opts: CollectOptions | None = None) -> Results:
    opts = opts or CollectOptions()
    config, core_devs = ReposConfig.load(), CoreDevs.load()
    token, auth_kind = resolve_token(opts.org)
    log.info(f"authenticating as: {auth_kind}")

    async with (
        GitHubClient(token, auth_kind=auth_kind, concurrency=opts.concurrency, cache=opts.cache) as gh,
        PyPIClient(cache=opts.cache) as pypi,
        RTDClient(cache=opts.cache) as rtd,
    ):
        packages, template, repo_objs = await asyncio.gather(
            scverse.fetch_packages(cache=opts.cache),
            fetch_template(gh),
            gh.org_repos(opts.org),
        )
        by_repo = scverse.index_by_repo(packages, opts.org)

        selected, excluded = select_repos(repo_objs, config, opts)
        log.info(f"collecting {len(selected)} repos ({len(excluded)} excluded)")

        async def one(repo_obj: dict[str, Any]) -> RepoReport:
            entry = by_repo.get(repo_obj["name"])
            data = await fetch_repo(gh, repo_obj, category=scverse.category_of(entry), package_entry=entry)
            data.template = template
            data.core_devs = core_devs.logins
            data.waivers = config.waivers.get(data.name, {})
            await _enrich(gh, pypi, rtd, data, config, template, opts)
            return RepoReport(repo=data, results=run_all(data))

        # One repo blowing up must not cost us the other eighty. Failures are recorded,
        # surfaced on the dashboard and re-raised as a non-zero exit *after* the artifact
        # has been written — see `CollectionFailedError` in cli.py.
        outcomes = await asyncio.gather(*(one(r) for r in selected), return_exceptions=True)
        reports = [o for o in outcomes if isinstance(o, RepoReport)]
        failed = [
            {"repo": repo["name"], "error": repr(outcome)}
            for repo, outcome in zip(selected, outcomes, strict=True)
            if isinstance(outcome, BaseException)
        ]
        for failure in failed:
            log.error(f"collecting {failure['repo']} failed: {failure['error']}")

        meta = {
            "auth": auth_kind,
            "failed_repos": failed,
            "github_requests": gh.requests,
            "github_cache_hits": gh.cache_hits,
            "rate_limit_remaining": gh.rate.remaining,
            "rate_limit": gh.rate.limit,
            "core_devs": len(core_devs.devs),
        }
        log.info(
            f"{gh.requests} GitHub requests ({gh.cache_hits} served from cache), "
            f"{gh.rate.remaining}/{gh.rate.limit} rate limit remaining"
        )

    return Results(
        generated=datetime.now(UTC).isoformat(timespec="seconds"),
        org=opts.org,
        reports=reports,
        excluded=excluded,
        template=template,
        meta=meta,
    )


async def _enrich(  # noqa: PLR0917 - a private helper threading the whole collection context
    gh: GitHubClient,
    pypi: PyPIClient,
    rtd: RTDClient,
    data: RepoData,
    config: ReposConfig,
    template: dict[str, Any],
    opts: CollectOptions,
) -> None:
    """Second wave: things that depend on what the first wave found."""
    cruft_commit = None
    if (cruft := data.file(".cruft.json")) is not None:
        try:
            cruft_commit = json.loads(cruft).get("commit")
        except json.JSONDecodeError as exc:
            data.errors.append(f".cruft.json is not valid JSON: {exc}")
    data.template_status = await template_position(gh, template, cruft_commit)

    name = pypi_name_for(data, config)
    slug = config.rtd_slug(data.name)
    pypi_task = pypi.project(name) if name and not opts.skip_pypi else _none()
    rtd_task = rtd.project(slug) if not opts.skip_rtd else _none()
    pypi_result, rtd_result = await asyncio.gather(pypi_task, rtd_task, return_exceptions=True)
    for label, result in (("pypi", pypi_result), ("rtd", rtd_result)):
        if isinstance(result, BaseException):
            # An outage upstream is "we don't know", never "this package is missing".
            data.unavailable[label] = f"{label} lookup failed: {result!r}"
            data.errors.append(f"{label} lookup failed: {result!r}")
        else:
            setattr(data, label, result)
    if name and data.pypi is None and not opts.skip_pypi and "pypi" not in data.unavailable:
        data.pypi = {"name": name, "missing": True}
    if opts.skip_pypi:
        data.unavailable["pypi"] = "PyPI lookups skipped"
    if opts.skip_rtd:
        data.unavailable["rtd"] = "Read the Docs lookups skipped"


async def _none() -> None:
    return None


def select_repos(
    repo_objs: list[dict[str, Any]],
    config: ReposConfig,
    opts: CollectOptions,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Split the org listing into repos we score and repos we list with a reason."""
    selected: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    wanted = {r.lower() for r in opts.repos}
    for repo in sorted(repo_objs, key=lambda r: r["name"].lower()):
        name = repo["name"]
        if wanted and name.lower() not in wanted:
            continue
        if repo.get("private"):
            excluded.append({"repo": name, "reason": "private"})
        elif repo.get("fork"):
            excluded.append({"repo": name, "reason": "fork"})
        elif config.is_excluded(name) and not opts.ignore_exclusions:
            excluded.append({"repo": name, "reason": config.exclusions[name]})
        elif repo.get("archived") and not opts.include_archived:
            excluded.append({"repo": name, "reason": "archived"})
        else:
            selected.append(repo)
    if wanted:
        found = {r["name"].lower() for r in selected} | {e["repo"].lower() for e in excluded}
        for missing in sorted(wanted - found):
            log.warning(f"--repo {missing}: no such repo in the org listing")
    return selected, excluded


def summarise(results: Results) -> dict[str, Any]:
    """Per-category and overall counts, for the dashboard header."""
    from .registry import Summary  # noqa: PLC0415 - keeps module import order simple

    overall = Summary()
    per_category: dict[str, Summary] = {}
    for report in results.reports:
        bucket = per_category.setdefault(str(report.repo.category), Summary())
        for result in report.results.values():
            overall.add(result)
            bucket.add(result)
    return {
        "overall": overall.to_dict(),
        "by_category": {k: v.to_dict() for k, v in per_category.items()},
        "repos": len(results.reports),
        "checks": len(REGISTRY),
        "failing_repos": sum(1 for r in results.reports if any(v.status is Status.FAIL for v in r.results.values())),
    }
