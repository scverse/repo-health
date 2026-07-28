"""Template & consistency checks."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from scverse_repo_health.models import Tier, failed, not_applicable, passed, unknown, verdict, warned
from scverse_repo_health.registry import check

from ._util import is_python_package, pyproject, truncate

if TYPE_CHECKING:
    from scverse_repo_health.models import CheckResult, RepoData

CATEGORY = "Template"
TEMPLATE_NAMES = ("cookiecutter-scverse", "scverse/cookiecutter-scverse")


def _cruft(repo: RepoData) -> dict | None:
    if (text := repo.file(".cruft.json")) is None:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


@check(
    id="template/cruft",
    tier=Tier.REQUIRED,
    category=CATEGORY,
    title="Uses cookiecutter-scverse",
    description="`.cruft.json` exists and names the scverse template",
    needs=("cont",),
    applies_to=is_python_package,
)
def uses_template(r: RepoData) -> CheckResult:
    fix = "https://github.com/scverse/cookiecutter-scverse#usage"
    if (cruft := _cruft(r)) is None:
        if r.has_path(".cruft.json"):
            return failed("`.cruft.json` is present but not valid JSON", r.blob_url(".cruft.json"))
        return failed("No `.cruft.json` — repo was not generated from the template", fix)
    url = r.blob_url(".cruft.json")
    if any(name in c for c in _template_refs(cruft) for name in TEMPLATE_NAMES):
        return passed("Generated from cookiecutter-scverse", url)
    # Falling back on the commit: cruft run against a local checkout records
    # `template: "/tmp/tmpXXXX"` everywhere (scirpy does), but the commit it pins is
    # still a cookiecutter-scverse commit, and `template_status` resolved it upstream.
    if r.template_status and not r.template_status.get("unknown"):
        return passed(
            f"Generated from cookiecutter-scverse (template path is a local checkout: "
            f"{str(cruft.get('template') or '?')[:40]})",
            url,
        )
    return failed(
        f"`.cruft.json` does not name the scverse template (template={cruft.get('template') or '?'!s})",
        url,
    )


def _template_refs(cruft: dict) -> list[str]:
    """Everywhere cruft might record where the template came from."""
    context = cruft.get("context") or {}
    inner = context.get("cookiecutter") or {}
    return [
        str(source.get(key) or "")
        for source in (cruft, context, inner)
        if isinstance(source, dict)
        for key in ("template", "_template", "_commit")
    ]


@check(
    id="template/up-to-date",
    tier=Tier.REQUIRED,
    category=CATEGORY,
    title="Template up to date",
    description="`.cruft.json` commit matches the latest cookiecutter-scverse release",
    needs=("cont", "meta"),
    applies_to=lambda r: is_python_package(r) and r.has_path(".cruft.json"),
)
def template_up_to_date(r: RepoData) -> CheckResult:  # noqa: PLR0911 - one return per distinguishable state
    status = r.template_status
    latest = ((r.template or {}).get("latest") or {}).get("tag", "?")
    fix = f"{r.html_url}/pulls?q=is%3Apr+cruft"
    if not status:
        return unknown("Could not determine the template commit")
    if status.get("unknown"):
        return unknown(f"Template commit not found in {(r.template or {}).get('repo')}", fix)
    behind = status.get("releases_behind")
    if behind == 0:
        return passed(f"On the latest template release ({latest})", fix)
    if behind:
        return failed(f"{behind} release{'s' if behind > 1 else ''} behind ({latest} is current)", fix)
    if (commits := status.get("commits_behind")) is not None:
        if commits == 0:
            return passed(f"Up to date with the template ({latest})", fix)
        return warned(f"{commits} template commits behind, but no release since ({latest} is current)", fix)
    return unknown("Could not compare against the template", fix)


@check(
    id="template/src-layout",
    tier=Tier.RECOMMENDED,
    category=CATEGORY,
    title="src layout + hatchling",
    description="Builds with hatchling/hatch-vcs from a `src/` layout",
    needs=("cont",),
    applies_to=is_python_package,
)
def src_layout(r: RepoData) -> CheckResult:
    parsed = pyproject(r)
    fix = r.blob_url("pyproject.toml")
    if parsed is None:
        return unknown("Could not parse `pyproject.toml`", fix)
    backend = ((parsed.get("build-system") or {}).get("build-backend") or "").lower()
    requires = " ".join((parsed.get("build-system") or {}).get("requires") or []).lower()
    has_src = any(p.startswith("src/") for p in r.tree)
    problems = []
    if "hatchling" not in backend:
        problems.append(f"build backend is {backend or 'unset'}")
    if "hatch-vcs" not in requires:
        problems.append("no hatch-vcs")
    if not has_src:
        problems.append("no `src/` directory")
    if problems:
        return failed("; ".join(problems), fix)
    return passed("hatchling + hatch-vcs, src layout", fix)


@check(
    id="template/pre-commit-ci",
    tier=Tier.RECOMMENDED,
    category=CATEGORY,
    title="pre-commit.ci enabled",
    description="pre-commit.ci runs on pull requests",
    needs=("meta",),
    applies_to=lambda r: r.has_path(".pre-commit-config.yaml") or r.has_path(".pre-commit-config.yml"),
)
def precommit_ci(r: RepoData) -> CheckResult:
    fix = "https://pre-commit.ci/"
    if (reason := r.is_unavailable("check_runs")) is not None:
        return unknown(reason, fix)
    names = [n for n in r.check_runs if "pre-commit.ci" in n.lower()]
    required = _required_status_checks(r)
    if names:
        return passed(f"Saw check run {names[0]!r}", fix)
    if any("pre-commit" in c.lower() for c in required):
        return passed("Listed among required status checks", fix)
    if not r.check_runs:
        return unknown("No check runs on the tip of the default branch to look at", fix)
    return failed("No pre-commit.ci check run on the default branch", fix)


@check(
    id="template/codecov",
    tier=Tier.RECOMMENDED,
    category=CATEGORY,
    title="codecov configured",
    description="A codecov config or a codecov check run exists",
    needs=("cont", "meta"),
    applies_to=is_python_package,
)
def codecov(r: RepoData) -> CheckResult:
    fix = f"https://app.codecov.io/gh/{r.full_name}"
    if (path := r.find(".codecov.yaml", ".codecov.yml", "codecov.yml", "codecov.yaml")) is not None:
        return passed(f"`{path}` present", r.blob_url(path))
    if any("codecov" in n.lower() for n in r.check_runs):
        return passed("codecov check run seen on the default branch", fix)
    if (reason := r.is_unavailable("check_runs")) is not None:
        return unknown(f"No codecov config, and check runs unreadable: {reason}", fix)
    return failed("No codecov config file and no codecov check run", fix)


@check(
    id="template/default-branch",
    tier=Tier.RECOMMENDED,
    category=CATEGORY,
    title="Default branch is main",
    description="The default branch is called `main`",
    needs=("meta",),
)
def default_branch_is_main(r: RepoData) -> CheckResult:
    return verdict(
        r.default_branch == "main",
        "Default branch is `main`",
        f"Default branch is `{r.default_branch}`",
        fix_url=r.settings_url("branches"),
    )


@check(
    id="template/packages-json",
    tier=Tier.INFORMATIONAL,
    category=CATEGORY,
    title="Listed in packages.json",
    description="Listed in scverse.org's ecosystem index, with a matching license and category",
    needs=("web",),
    applies_to=is_python_package,
)
def listed_in_packages_json(r: RepoData) -> CheckResult:
    fix = "https://github.com/scverse/scverse.github.io/blob/main/data/ecosystem-packages.yaml"
    if not r.package_entry:
        return not_applicable("Not listed in packages.json")
    entry = r.package_entry
    mismatches = []
    repo_license = ((r.repo.get("license") or {}) or {}).get("spdx_id")
    if entry.get("license") and repo_license and entry["license"] != repo_license:
        mismatches.append(f"license {entry['license']} vs GitHub's {repo_license}")
    if entry.get("category") and entry["category"] != str(r.category):
        mismatches.append(f"category {entry['category']}")
    if mismatches:
        return warned(f"Listed as {truncate(mismatches)}", fix)
    return passed(f"Listed under {entry.get('category', 'no category')}", fix)


def _required_status_checks(r: RepoData) -> list[str]:
    """Names of required status checks, from rulesets or classic protection."""
    names: list[str] = []
    for rule in r.rulesets or []:
        if rule.get("type") == "required_status_checks":
            params = rule.get("parameters") or {}
            names += [c.get("context", "") for c in params.get("required_status_checks") or []]
    classic = (r.classic_protection or {}).get("required_status_checks") or {}
    names += list(classic.get("contexts") or [])
    return [n for n in names if n]
