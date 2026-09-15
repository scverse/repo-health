"""Packaging & release checks.

Trusted publishing is verified from PEP 740 attestations rather than taken on trust: the
Sigstore certificate's SAN names the exact workflow and tag that published the artefact.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from scverse_repo_health.models import Tier, failed, not_applicable, passed, unknown, verdict, warned
from scverse_repo_health.registry import check

from ._util import is_python_package, iter_steps, load_yaml, pyproject, truncate

if TYPE_CHECKING:
    from typing import Any

    from scverse_repo_health.models import CheckResult, RepoData

CATEGORY = "Packaging"
PUBLISH_ACTION = "pypa/gh-action-pypi-publish"
RECENT_RELEASES = 5
SPEC0_URL = "https://scientific-python.org/specs/spec-0000/"
SPEC0_YEARS = 3


def _on_pypi(r: RepoData) -> bool:
    return is_python_package(r) and bool(r.pypi) and not r.pypi.get("missing")


def release_workflows(r: RepoData) -> dict[str, dict[str, Any]]:
    """Workflows that publish to PyPI, keyed by path."""
    out: dict[str, dict[str, Any]] = {}
    for path, text in r.workflows.items():
        if PUBLISH_ACTION in (text or ""):
            if (parsed := load_yaml(text)) is not None:
                out[path] = parsed
            else:
                out[path] = {}
    return out


@check(
    id="packaging/pypi-org",
    tier=Tier.REQUIRED,
    category=CATEGORY,
    title="PyPI project owned by scverse",
    description="`ownership.organization` on PyPI is the scverse org",
    needs=("pypi",),
    applies_to=is_python_package,
)
def pypi_org(r: RepoData) -> CheckResult:
    if (reason := r.is_unavailable("pypi")) is not None:
        return unknown(reason)
    if not r.pypi:
        return not_applicable("No distribution name found in `pyproject.toml`")
    if r.pypi.get("missing"):
        return not_applicable(f"`{r.pypi['name']}` is not published on PyPI")
    org = (r.pypi.get("ownership") or {}).get("organization")
    url = r.pypi.get("url")
    if org == r.owner:
        return passed(f"Owned by the `{org}` PyPI organization", url)
    if org:
        return failed(f"Owned by PyPI organization `{org}`, not `{r.owner}`", url)
    return failed("Not owned by a PyPI organization", url)


@check(
    id="packaging/trusted-publishing",
    tier=Tier.REQUIRED,
    category=CATEGORY,
    title="Trusted publishing",
    description="The latest release carries PEP 740 attestations naming this repo's workflow",
    needs=("pypi",),
    applies_to=_on_pypi,
)
def trusted_publishing(r: RepoData) -> CheckResult:
    if (reason := r.is_unavailable("pypi")) is not None:
        return unknown(reason)
    assert r.pypi is not None
    version = r.pypi.get("version")
    url = r.pypi.get("url")
    fix = "https://docs.pypi.org/trusted-publishers/"
    provenance = r.pypi.get("provenance")
    if not provenance:
        return failed(f"`{version}` has no attestations — published with a token, not OIDC", fix)
    publisher = provenance.get("publisher") or {}
    parts = provenance.get("san_parts") or {}
    claimed_repo = parts.get("repo") or publisher.get("repository")
    workflow = parts.get("workflow") or f".github/workflows/{publisher.get('workflow', '?')}"
    if claimed_repo and claimed_repo.lower() != r.full_name.lower():
        return failed(f"`{version}` was published by {claimed_repo}, not {r.full_name}", url)
    detail = f"`{version}` published by {claimed_repo}/{workflow}"
    if not provenance.get("san"):
        # PyPI's own publisher record agrees, but we could not read the certificate.
        return warned(f"{detail} (per PyPI; certificate not decodable)", url)
    return passed(detail, url)


@check(
    id="packaging/release-workflow",
    tier=Tier.REQUIRED,
    category=CATEGORY,
    title="Release workflow hardened",
    description="Publishes via pypa/gh-action-pypi-publish with `id-token: write`, no password, in an environment",
    needs=("cont",),
    applies_to=is_python_package,
)
def release_workflow(r: RepoData) -> CheckResult:
    workflows = release_workflows(r)
    if not workflows:
        if r.is_unavailable("tree"):
            return unknown("Could not read the repository tree")
        return failed(f"No workflow uses `{PUBLISH_ACTION}`", r.new_file_url(".github/workflows/release.yaml"))
    path, parsed = next(iter(workflows.items()))
    fix = r.blob_url(path)
    problems: list[str] = []
    jobs = {name: job for name, job in (parsed.get("jobs") or {}).items() if isinstance(job, dict)}
    publishing = {
        name: job
        for name, job in jobs.items()
        if any(PUBLISH_ACTION in str(s.get("uses", "")) for s in job.get("steps") or [] if isinstance(s, dict))
    }
    if not publishing:
        return warned(f"`{path}` mentions {PUBLISH_ACTION} but no job could be parsed", fix)
    for name, job in publishing.items():
        perms = job.get("permissions") or parsed.get("permissions") or {}
        if isinstance(perms, dict) and perms.get("id-token") != "write":
            problems.append(f"job `{name}` lacks `id-token: write`")
        if not job.get("environment"):
            problems.append(f"job `{name}` is not bound to an environment")
    for _wf, _job, step in iter_steps(r):
        if PUBLISH_ACTION in str(step.get("uses", "")) and (step.get("with") or {}).get("password"):
            problems.append("publish step still passes a `password:`")
    if problems:
        return failed(truncate(sorted(set(problems)), 4), fix)
    return passed(f"`{path}` publishes via OIDC from an environment", fix)


def _released(releases: list[dict[str, Any]]) -> list[tuple[Version, date]]:
    """Every Python version endoflife.date reports, with its release date, oldest first."""
    dated = []
    for entry in releases:
        try:
            dated.append((Version(str(entry["cycle"])), date.fromisoformat(str(entry["releaseDate"]))))
        except (KeyError, TypeError, ValueError, InvalidVersion):
            continue
    return sorted(dated)


def _drop_date(released: date) -> date:
    """SPEC 0 drops a Python version three years after its initial release."""
    try:
        return released.replace(year=released.year + SPEC0_YEARS)
    except ValueError:  # 29 February
        return released.replace(year=released.year + SPEC0_YEARS, day=released.day - 1)


def spec0_minimum_python(releases: list[dict[str, Any]], today: date | None = None) -> Version | None:
    """The oldest Python SPEC 0 still asks for, being the first release under three years old."""
    now = today or datetime.now(UTC).date()
    return next((v for v, released in _released(releases) if _drop_date(released) > now), None)


def lowest_python(requires: str, releases: list[dict[str, Any]]) -> Version | None:
    """The oldest released Python a ``requires-python`` specifier still admits."""
    try:
        spec = SpecifierSet(requires)
    except InvalidSpecifier:
        return None
    return next((v for v, _ in _released(releases) if v in spec), None)


@check(
    id="packaging/spec0-python",
    tier=Tier.REQUIRED,
    category=CATEGORY,
    title="SPEC 0 minimum Python",
    description="`requires-python` has dropped the Python versions SPEC 0 has dropped",
    needs=("cont", "py"),
    applies_to=lambda r: r.has_path("pyproject.toml"),
)
def spec0_python(r: RepoData) -> CheckResult:
    if (reason := r.is_unavailable("python_releases")) is not None:
        return unknown(reason, SPEC0_URL)
    fix = r.blob_url("pyproject.toml")
    data = pyproject(r)
    if data is None:
        return unknown("Could not read `pyproject.toml`", fix)
    requires = (data.get("project") or {}).get("requires-python")
    if not requires:
        return failed("No `requires-python` in `pyproject.toml`", fix)
    lowest = lowest_python(str(requires), r.python_releases or [])
    if lowest is None:
        return unknown(f'Could not parse `requires-python = "{requires}"`', fix)
    wanted = spec0_minimum_python(r.python_releases or [])
    if wanted is None:
        return unknown("No Python release dates to measure SPEC 0 against", SPEC0_URL)
    return verdict(
        lowest >= wanted,
        f'`requires-python = "{requires}"` is at or above SPEC 0\'s {wanted}',
        f'`requires-python = "{requires}"` still allows {lowest}; SPEC 0 is at {wanted}',
        fix_url=SPEC0_URL,
    )


@check(
    id="packaging/immutable-releases",
    tier=Tier.REQUIRED,
    category=CATEGORY,
    title="Immutable releases enabled",
    description="The repository has immutable releases turned on",
    needs=("admin",),
)
def immutable_releases(r: RepoData) -> CheckResult:
    fix = r.settings_url()
    if (reason := r.is_unavailable("immutable_releases")) is not None:
        return unknown(reason, fix)
    payload = r.immutable_releases
    if payload is None:
        return failed("Immutable releases are not enabled", fix)
    enabled = payload.get("enabled") if isinstance(payload, dict) else bool(payload)
    if enabled is None:
        enabled = True  # endpoint answered without an explicit flag
    return verdict(bool(enabled), "Immutable releases enabled", "Immutable releases disabled", fix_url=fix)


@check(
    id="packaging/releases-immutable",
    tier=Tier.RECOMMENDED,
    category=CATEGORY,
    title="Recent releases immutable",
    description="The last few published releases are actually marked immutable",
    needs=("meta",),
    applies_to=lambda r: bool(r.releases),
)
def releases_immutable(r: RepoData) -> CheckResult:
    fix = f"{r.html_url}/releases"
    recent = [rel for rel in r.releases if not rel.get("draft")][:RECENT_RELEASES]
    if not recent:
        return not_applicable("No published releases")
    flags = [rel.get("immutable") for rel in recent]
    if all(f is None for f in flags):
        return unknown("GitHub did not report immutability on these releases", fix)
    mutable = [rel["tag_name"] for rel, f in zip(recent, flags, strict=True) if not f]
    if mutable:
        return failed(f"{len(mutable)} of the last {len(recent)} are mutable: {truncate(mutable)}", fix)
    return passed(f"All of the last {len(recent)} releases are immutable", fix)


@check(
    id="packaging/pypi-environment",
    tier=Tier.RECOMMENDED,
    category=CATEGORY,
    title="`pypi` environment protected",
    description="A deployment environment named `pypi` exists and has protection rules",
    needs=("admin",),
    applies_to=is_python_package,
)
def pypi_environment(r: RepoData) -> CheckResult:
    fix = r.settings_url("environments")
    if (reason := r.is_unavailable("environments")) is not None:
        return unknown(reason, fix)
    if r.environments is None:
        return unknown("Environments could not be read", fix)
    names = {e.get("name", "") for e in r.environments}
    env = next((e for e in r.environments if e.get("name") == "pypi"), None)
    if env is None:
        detail = f"No `pypi` environment (found: {truncate(sorted(names)) or 'none'})"
        return failed(detail, fix)
    rules = env.get("protection_rules") or []
    if not rules:
        return warned("`pypi` environment exists but has no protection rules", fix)
    kinds = sorted({str(rule.get("type")) for rule in rules})
    return passed(f"`pypi` environment protected by {truncate(kinds)}", fix)


@check(
    id="packaging/version-sync",
    tier=Tier.RECOMMENDED,
    category=CATEGORY,
    title="Tag matches PyPI version",
    description="The newest GitHub release tag matches the newest PyPI version",
    needs=("meta", "pypi"),
    applies_to=_on_pypi,
)
def version_sync(r: RepoData) -> CheckResult:
    assert r.pypi is not None
    published = [rel for rel in r.releases if not rel.get("draft") and not rel.get("prerelease")]
    if not published:
        return not_applicable("No published GitHub releases")
    tag = published[0]["tag_name"]
    pypi_version = r.pypi.get("version")
    fix = f"{r.html_url}/releases/tag/{tag}"
    try:
        same = Version(tag.lstrip("vV")) == Version(str(pypi_version))
    except InvalidVersion:
        same = tag.lstrip("vV") == str(pypi_version)
    if same:
        return passed(f"GitHub `{tag}` == PyPI `{pypi_version}`", fix)
    return warned(f"GitHub release `{tag}` but PyPI has `{pypi_version}` — did a publish fail?", fix)
