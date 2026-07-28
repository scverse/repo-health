"""Documentation checks.

Read the Docs *organizations* are a Business-plan feature and no scverse project has one,
so "does scverse control these docs" is answered indirectly: the RTD project must point at
this GitHub repo, and at least two of its maintainers must be scverse core devs (per
`config/core_devs.yaml`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scverse_repo_health.models import Tier, failed, not_applicable, passed, unknown, verdict, warned
from scverse_repo_health.registry import check

from ._util import host_of, is_python_package, truncate

if TYPE_CHECKING:
    from scverse_repo_health.models import CheckResult, RepoData

CATEGORY = "Documentation"
MIN_CORE_DEV_MAINTAINERS = 2
RTD_SETTINGS = "https://app.readthedocs.org/dashboard/{slug}/edit/"


def _has_rtd(r: RepoData) -> bool:
    """Only ask about the RTD project of repos that claim to build docs there.

    A repo with no `.readthedocs.yaml` already fails :func:`readthedocs_yaml`; failing the
    four downstream RTD checks as well would count one gap five times.
    """
    return is_python_package(r) and r.find(".readthedocs.yaml", ".readthedocs.yml") is not None


@check(
    id="docs/readthedocs-yaml",
    tier=Tier.REQUIRED,
    category=CATEGORY,
    title=".readthedocs.yaml present",
    description="A `.readthedocs.yaml` build config is committed",
    needs=("cont",),
    applies_to=is_python_package,
)
def readthedocs_yaml(r: RepoData) -> CheckResult:
    if (reason := r.is_unavailable("tree")) is not None:
        return unknown(reason)
    if (path := r.find(".readthedocs.yaml", ".readthedocs.yml")) is not None:
        return passed(f"`{path}` present", r.blob_url(path))
    return failed("No `.readthedocs.yaml`", r.new_file_url(".readthedocs.yaml"))


@check(
    id="docs/rtd-linked",
    tier=Tier.REQUIRED,
    category=CATEGORY,
    title="RTD project points here",
    description="The Read the Docs project of the same slug builds *this* repository",
    needs=("rtd",),
    applies_to=_has_rtd,
)
def rtd_linked(r: RepoData) -> CheckResult:
    if (reason := r.is_unavailable("rtd")) is not None:
        return unknown(reason)
    if not r.rtd:
        return failed(
            f"No Read the Docs project with slug `{r.name.lower()}` "
            "(set `rtd_slugs:` in config/repos.yaml if it differs)",
            "https://app.readthedocs.org/dashboard/import/",
        )
    url = (r.rtd.get("repository") or {}).get("url") or ""
    expected = f"{r.html_url}.git"
    fix = RTD_SETTINGS.format(slug=r.rtd.get("slug"))
    # Guards against slug collisions: RTD `spatialdata` has at times pointed elsewhere.
    if url.rstrip("/").removesuffix(".git").lower() == r.html_url.lower():
        return passed(f"RTD `{r.rtd.get('slug')}` builds {r.full_name}", fix)
    return failed(f"RTD `{r.rtd.get('slug')}` builds {url or 'nothing'}, expected {expected}", fix)


@check(
    id="docs/rtd-core-devs",
    tier=Tier.REQUIRED,
    category=CATEGORY,
    title="≥2 core devs on RTD",
    description="At least two scverse core devs can administer the RTD project",
    needs=("rtd",),
    applies_to=_has_rtd,
)
def rtd_core_devs(r: RepoData) -> CheckResult:
    if (reason := r.is_unavailable("rtd")) is not None:
        return unknown(reason)
    if not r.rtd:
        return failed("No Read the Docs project to inspect")
    known = {login.lower() for login in r.core_devs}
    if not known:
        return unknown("`config/core_devs.yaml` is empty")
    users = r.rtd.get("users") or []
    core = [u for u in users if u.lower() in known]
    fix = RTD_SETTINGS.format(slug=r.rtd.get("slug"))
    detail = f"{len(core)} of {len(users)} maintainers are core devs"
    if core:
        detail += f" ({truncate(core)})"
    if len(core) >= MIN_CORE_DEV_MAINTAINERS:
        return passed(detail, fix)
    return failed(f"{detail}; at least {MIN_CORE_DEV_MAINTAINERS} required", fix)


@check(
    id="docs/scverse-domain",
    tier=Tier.REQUIRED,
    category=CATEGORY,
    title="Docs on *.scverse.org",
    description="Documentation is served from `<package>.scverse.org`, not readthedocs.io",
    needs=("rtd", "meta"),
    applies_to=_has_rtd,
)
def scverse_domain(r: RepoData) -> CheckResult:
    if (reason := r.is_unavailable("rtd")) is not None:
        return unknown(reason)
    fix = f"https://app.readthedocs.org/dashboard/{(r.rtd or {}).get('slug', r.name.lower())}/domains/"
    documented = (r.rtd or {}).get("urls", {}).get("documentation")
    candidates = {
        host_of(documented),
        host_of(r.repo.get("homepage")),
        host_of((r.package_entry or {}).get("documentation_home")),
    } - {""}
    if not candidates:
        return unknown("No documentation URL recorded anywhere", fix)
    on_domain = sorted(h for h in candidates if h.endswith(".scverse.org"))
    if on_domain:
        stale = sorted(h for h in candidates if h.endswith("readthedocs.io"))
        if stale:
            return warned(f"{on_domain[0]}, but {truncate(stale)} is still advertised", fix)
        return passed(on_domain[0], fix)
    return failed(f"Docs served from {truncate(sorted(candidates))}", fix)


@check(
    id="docs/rtd-build",
    tier=Tier.RECOMMENDED,
    category=CATEGORY,
    title="Stable RTD build passed",
    description="The most recent build of the `stable` version succeeded",
    needs=("rtd",),
    applies_to=_has_rtd,
)
def rtd_build(r: RepoData) -> CheckResult:
    if (reason := r.is_unavailable("rtd")) is not None:
        return unknown(reason)
    if not r.rtd:
        return failed("No Read the Docs project to inspect")
    build = r.rtd.get("stable_build")
    if not build:
        # Either the project has no `stable` version — nothing tagged yet — or it has one
        # that has never been built. Neither is a broken build.
        return not_applicable("No build of a `stable` version", r.rtd.get("home"))
    url = build.get("url")
    # RTD only fills in `success` once the build reaches `finished`; a build that is
    # still cloning or installing is not a failure, we just caught it mid-flight.
    if (state := build.get("state")) != "finished":
        return unknown(f"The `stable` build is still {state or 'running'}", url)
    commit = str(build.get("commit") or "?")[:7]
    when = str(build.get("finished") or "?")[:10]
    return verdict(
        bool(build.get("success")),
        f"`stable` built from `{commit}` on {when}",
        f"The `stable` build of `{commit}` failed on {when}",
        fix_url=url,
    )
