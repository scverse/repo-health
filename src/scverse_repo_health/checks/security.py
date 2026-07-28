"""Supply chain & CI security checks.

Several of these mirror `zizmor <https://docs.zizmor.sh>`_ audits (``artipacked``,
``dangerous-triggers``, ``unpinned-uses``) so that a repo which has not adopted zizmor
still gets told about the same problems.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scverse_repo_health.models import Tier, failed, not_applicable, passed, unknown, verdict, warned
from scverse_repo_health.registry import check

from ._util import (
    dependabot_config,
    has_workflows,
    is_sha,
    iter_steps,
    iter_uses,
    load_yaml,
    plural,
    precommit_hook_ids,
    precommit_path,
    precommit_repos,
    precommit_text,
    triggers,
    truncate,
)

if TYPE_CHECKING:
    from scverse_repo_health.models import CheckResult, RepoData

CATEGORY = "Supply chain"
ZIZMOR_HOOK_REPO = "zizmorcore/zizmor-pre-commit"


def _has_precommit(r: RepoData) -> bool:
    return precommit_text(r) is not None


@check(
    id="security/actions-pinned",
    tier=Tier.REQUIRED,
    category=CATEGORY,
    title="Actions pinned to SHA",
    description="Every `uses:` names a full 40-character commit SHA",
    needs=("cont",),
    applies_to=has_workflows,
)
def actions_pinned(r: RepoData) -> CheckResult:
    unpinned = []
    total = 0
    for use in iter_uses(r):
        # Local (`./…`) refs and this org's own reusable workflows are pinned by the
        # commit that runs them, so there is nothing to pin.
        if use.is_local or use.action.startswith(f"{r.owner}/"):
            continue
        total += 1
        if not use.pinned:
            unpinned.append(use.ref)
    if total == 0:
        return not_applicable("No third-party actions used")
    fix = f"{r.html_url}/tree/{r.default_branch}/.github/workflows"
    if unpinned:
        return failed(f"{len(unpinned)} of {total} unpinned: {truncate(sorted(set(unpinned)))}", fix)
    return passed(f"All {plural(total, 'action reference')} pinned to a SHA", fix)


@check(
    id="security/precommit-pinned",
    tier=Tier.RECOMMENDED,
    category=CATEGORY,
    title="pre-commit revs pinned",
    description="Every `rev:` in `.pre-commit-config.yaml` is a commit SHA",
    needs=("cont",),
    applies_to=_has_precommit,
)
def precommit_pinned(r: RepoData) -> CheckResult:
    entries = [e for e in precommit_repos(r) if e.get("repo") not in (None, "local", "meta")]
    if not entries:
        return not_applicable("No remote pre-commit hooks")
    unpinned = [f"{e['repo'].rsplit('/', 1)[-1]}@{e.get('rev')}" for e in entries if not is_sha(str(e.get("rev", "")))]
    fix = r.blob_url(precommit_path(r))
    if unpinned:
        # The template itself still uses tags here, so this is red nearly everywhere —
        # which is the point: it shows the template is what needs changing.
        return failed(f"{len(unpinned)} of {len(entries)} on tags: {truncate(sorted(unpinned))}", fix)
    return passed(f"All {plural(len(entries), 'hook repo')} pinned to a SHA", fix)


@check(
    id="security/zizmor",
    tier=Tier.REQUIRED,
    category=CATEGORY,
    title="zizmor enabled",
    description="zizmor audits the workflows, via pre-commit or a workflow of its own",
    needs=("cont",),
    applies_to=has_workflows,
)
def zizmor_enabled(r: RepoData) -> CheckResult:
    fix = "https://docs.zizmor.sh/quickstart/"
    if any(ZIZMOR_HOOK_REPO in str(e.get("repo", "")) for e in precommit_repos(r)):
        return passed("zizmor pre-commit hook configured", r.blob_url(precommit_path(r)))
    if "zizmor" in precommit_hook_ids(r):
        return passed("zizmor pre-commit hook configured", r.blob_url(precommit_path(r)))
    hits = [p for p, text in r.workflows.items() if "zizmor" in (text or "")]
    if hits:
        return passed(f"zizmor runs in `{hits[0]}`", r.blob_url(hits[0]))
    return failed("zizmor is not configured", fix)


@check(
    id="security/dependabot",
    tier=Tier.REQUIRED,
    category=CATEGORY,
    title="Dependabot configured",
    description="`.github/dependabot.yml` covers both the github-actions and pre-commit ecosystems",
    needs=("cont",),
    applies_to=lambda r: has_workflows(r) or _has_precommit(r),
)
def dependabot(r: RepoData) -> CheckResult:
    # Only ask for the ecosystems this repo actually has: a repo with workflows but no
    # pre-commit config has nothing for the pre-commit updater to do.
    wanted = set()
    if has_workflows(r):
        wanted.add("github-actions")
    if _has_precommit(r):
        wanted.add("pre-commit")
    path = r.find(".github/dependabot.yml", ".github/dependabot.yaml")
    if path is None:
        return failed(
            f"No `.github/dependabot.yml` (needs {truncate(sorted(wanted))})",
            r.new_file_url(".github/dependabot.yml"),
        )
    config = dependabot_config(r)
    fix = r.blob_url(path)
    if config is None:
        return failed(f"`{path}` is not valid YAML", fix)
    ecosystems = {str(u.get("package-ecosystem")) for u in config.get("updates") or []}
    if missing := sorted(wanted - ecosystems):
        return failed(f"Missing ecosystem: {truncate(missing)}", fix)
    return passed(f"Covers {truncate(sorted(ecosystems), 4)}", fix)


@check(
    id="security/dependabot-alerts",
    tier=Tier.RECOMMENDED,
    category=CATEGORY,
    title="No open Dependabot alerts",
    description="No unresolved Dependabot security alerts",
    needs=("sec",),
)
def dependabot_alerts(r: RepoData) -> CheckResult:
    fix = f"{r.html_url}/security/dependabot"
    if (reason := r.is_unavailable("dependabot_alerts")) is not None:
        return unknown(reason, fix)
    if r.dependabot_alerts is None:
        return unknown("Dependabot alerts could not be read", fix)
    severities = [str((a.get("security_advisory") or {}).get("severity", "?")) for a in r.dependabot_alerts]
    high = [s for s in severities if s in ("high", "critical")]
    if not severities:
        return passed("No open Dependabot alerts", fix)
    if high:
        return failed(f"{plural(len(severities), 'open alert')}, {len(high)} high or critical", fix)
    return warned(f"{plural(len(severities), 'open alert')} ({truncate(sorted(set(severities)))})", fix)


@check(
    id="security/secret-scanning",
    tier=Tier.RECOMMENDED,
    category=CATEGORY,
    title="Secret scanning + push protection",
    description="Secret scanning and push protection are both enabled",
    needs=("admin",),
)
def secret_scanning(r: RepoData) -> CheckResult:
    fix = r.settings_url("security_analysis")
    analysis = r.repo.get("security_and_analysis")
    if not analysis:
        reason = r.is_unavailable("full_repo") or "`security_and_analysis` is only visible with admin access"
        return unknown(reason, fix)
    scanning = (analysis.get("secret_scanning") or {}).get("status")
    push = (analysis.get("secret_scanning_push_protection") or {}).get("status")
    off = [name for name, status in (("scanning", scanning), ("push protection", push)) if status != "enabled"]
    if off:
        return failed(f"Secret {truncate(off)} disabled", fix)
    return passed("Secret scanning and push protection enabled", fix)


@check(
    id="security/private-vuln-reporting",
    tier=Tier.RECOMMENDED,
    category=CATEGORY,
    title="Private vulnerability reporting",
    description="Researchers can report vulnerabilities privately",
    needs=("admin",),
)
def private_vulnerability_reporting(r: RepoData) -> CheckResult:
    fix = r.settings_url("security_analysis")
    if (reason := r.is_unavailable("private_vulnerability_reporting")) is not None:
        return unknown(reason, fix)
    payload = r.private_vulnerability_reporting
    if payload is None:
        return unknown("Endpoint returned nothing", fix)
    return verdict(
        bool(payload.get("enabled")),
        "Private vulnerability reporting enabled",
        "Private vulnerability reporting disabled",
        fix_url=fix,
    )


@check(
    id="security/workflow-permissions",
    tier=Tier.REQUIRED,
    category=CATEGORY,
    title="Minimal workflow permissions",
    description="Every workflow declares a top-level `permissions:`, and the repo default is read-only",
    needs=("cont", "admin"),
    applies_to=has_workflows,
)
def workflow_permissions(r: RepoData) -> CheckResult:
    fix = r.settings_url("actions")
    missing = []
    for path, text in r.workflows.items():
        parsed = load_yaml(text)
        if parsed is None:
            continue
        if "permissions" not in parsed:
            missing.append(path.removeprefix(".github/workflows/"))
    default_ok: bool | None = None
    if r.actions_permissions is not None:
        default_ok = r.actions_permissions.get("default_workflow_permissions") == "read"
    if missing:
        return failed(f"{len(missing)} of {len(r.workflows)} lack `permissions:`: {truncate(sorted(missing))}", fix)
    if default_ok is False:
        return failed("Workflows are fine, but the repo's default GITHUB_TOKEN is read-write", fix)
    if default_ok is None:
        reason = r.is_unavailable("actions_permissions") or "default token permissions not readable"
        return warned(f"All workflows declare `permissions:`; repo default unknown ({reason})", fix)
    return passed("All workflows declare `permissions:`; repo default is read-only", fix)


@check(
    id="security/persist-credentials",
    tier=Tier.RECOMMENDED,
    category=CATEGORY,
    title="checkout without credentials",
    description="`actions/checkout` steps set `persist-credentials: false` (zizmor `artipacked`)",
    needs=("cont",),
    applies_to=has_workflows,
)
def persist_credentials(r: RepoData) -> CheckResult:
    fix = "https://docs.zizmor.sh/audits/#artipacked"
    offenders = []
    total = 0
    for path, job, step in iter_steps(r):
        if not str(step.get("uses", "")).startswith("actions/checkout@"):
            continue
        total += 1
        if (step.get("with") or {}).get("persist-credentials") not in (False, "false"):
            offenders.append(f"{path.removeprefix('.github/workflows/')}:{job}")
    if total == 0:
        return not_applicable("No `actions/checkout` steps")
    if offenders:
        return failed(f"{len(offenders)} of {total} keep credentials: {truncate(sorted(offenders))}", fix)
    return passed(f"All {plural(total, 'checkout')} set `persist-credentials: false`", fix)


@check(
    id="security/dangerous-triggers",
    tier=Tier.RECOMMENDED,
    category=CATEGORY,
    title="No unsafe pull_request_target",
    description="`pull_request_target` does not check out untrusted PR code (zizmor `dangerous-triggers`)",
    needs=("cont",),
    applies_to=has_workflows,
)
def dangerous_triggers(r: RepoData) -> CheckResult:
    fix = "https://docs.zizmor.sh/audits/#dangerous-triggers"
    using: list[str] = []
    unsafe: list[str] = []
    for path, text in r.workflows.items():
        parsed = load_yaml(text)
        if parsed is None or "pull_request_target" not in triggers(parsed):
            continue
        short = path.removeprefix(".github/workflows/")
        using.append(short)
        for job in (parsed.get("jobs") or {}).values():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps") or []:
                if not isinstance(step, dict) or not str(step.get("uses", "")).startswith("actions/checkout@"):
                    continue
                ref = str((step.get("with") or {}).get("ref", ""))
                if "pull_request" in ref or "head" in ref.lower():
                    unsafe.append(short)
    if not using:
        return passed("No workflow uses `pull_request_target`", fix)
    if unsafe:
        return failed(f"{truncate(sorted(set(unsafe)))} check out PR code under `pull_request_target`", fix)
    return warned(f"{truncate(sorted(set(using)))} use `pull_request_target` (no PR checkout seen)", fix)


@check(
    id="security/security-md",
    tier=Tier.RECOMMENDED,
    category=CATEGORY,
    title="SECURITY.md",
    description="A security policy exists, in this repo or via the org's `.github` repo",
    needs=("meta",),
)
def security_md(r: RepoData) -> CheckResult:
    fix = f"{r.html_url}/security/policy"
    if (reason := r.is_unavailable("community_profile")) is not None:
        return unknown(reason, fix)
    if r.community_profile is None:
        return unknown("Community profile could not be read", fix)
    files = r.community_profile.get("files") or {}
    policy = files.get("security") or files.get("security_policy")
    return verdict(
        bool(policy),
        "Security policy present",
        "No security policy",
        fix_url=(policy or {}).get("html_url") if policy else fix,
    )
