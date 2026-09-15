# scverse repo-health

A weekly-refreshed dashboard showing, at a glance, which repositories in the
[scverse][] GitHub organisation meet which of the org's standards — cookiecutter template
adoption, trusted publishing, branch protection, PyPI org ownership, docs domain, supply
chain hardening — and, for every gap, a direct link to the page where it gets fixed.

In the spirit of [nf-co.re/pipeline_health][nfcore].

**Dashboard: <https://scverse.github.io/repo-health/>**

[scverse]: https://scverse.org
[nfcore]: https://nf-co.re/pipeline_health

## How it works

```
repo-health collect  →  results.json  →  repo-health render  →  site/
```

Two phases, with `results.json` as the contract between them. That makes rendering
testable offline, the data diffable week over week, and the whole thing machine-readable
by anyone who wants to consume it (it is published alongside the site at
[`results.json`](https://scverse.github.io/repo-health/results.json)).

The published artifact is slimmed: the raw file contents and API payloads the checks have
already consumed are dropped, which takes it from roughly 90 KiB per repo to 13 KiB
without changing a pixel of the rendered site. `collect --no-slim` keeps everything, for
debugging a check against real data.

Every check is a **pure function** of an already-fetched `RepoData` — no I/O inside
checks — so each one is unit-tested against a JSON fixture captured from a real repo.

A check that cannot be evaluated because an endpoint returned 403 renders as `?`
(unknown), never as a failure. The tool therefore still produces something useful when
run without the GitHub App installed; it just knows less.

## Usage

```console
$ uv run repo-health list-checks              # every check: id, tier, category, needs
$ uv run repo-health check scirpy             # one repo, rich table — the dev loop
$ uv run repo-health collect --out results.json
$ uv run repo-health render results.json --out site/
$ uv run repo-health run --out site/          # collect + render
$ uv run repo-health audit-exclusions         # non-zero if an org repo is unclassified
```

Useful flags: `--repo NAME` (repeatable) restricts to a few repos, `--no-cache` bypasses
the ETag cache, `--include-archived` adds archived repos, `--previous last-week.json`
outlines the cells that changed, `--skip-pypi` / `--skip-rtd` keep the run entirely
inside GitHub, and `--skip-zizmor` skips the one check that shells out.
`audit-exclusions` takes `--markdown FILE` and `--exit-zero`, which is how the monthly workflow turns it into an issue.

Failing *checks* never set a non-zero exit code — only collection errors do. The
dashboard is informational and a red cell must not break the weekly cron. When a single
repository does fail to collect, the run still writes the site (with that repo listed in
the footer) and *then* exits 2, so CI goes red without costing you the other eighty.

## The checks

38 checks in six groups, which are the six column groups on the dashboard. Each declares
a **tier** and what data it **needs**:

| Tier | Meaning |
|---|---|
| **R** required | Expected of every scverse package. Counts towards the compliance score. |
| **r** recommended | Strongly encouraged, not scored. |
| **i** informational | Context, not a judgement. |

| Needs | Where the answer comes from |
|---|---|
| `meta` | GitHub `metadata:read` — public, so these always have an answer |
| `cont` | GitHub `contents:read` — the git tree and a handful of blobs |
| `admin` | GitHub `administration:read` — renders `?` without the App installed |
| `admin?` | Rulesets need only `meta`; `admin` is the fallback for *classic* branch protection |
| `sec` | GitHub `dependabot_alerts:read` |
| `actions` | GitHub `actions:read` |
| `pypi` | pypi.org |
| `rtd` | readthedocs.org |
| `web` | scverse.org's `packages.json` |

A check that applies to a repository but cannot be evaluated renders `?`, never a
failure. Checks that do not apply — packaging checks on a repo with no `pyproject.toml`,
say — render `–`. `repo-health list-checks` prints this same catalogue in the terminal.

### Template & consistency

| | Check | What it means | Needs |
|---|---|---|---|
| **R** | `template/cruft` | `.cruft.json` exists and names the scverse template | cont |
| **R** | `template/up-to-date` | `.cruft.json` commit matches the latest cookiecutter-scverse release | cont meta |
| **r** | `template/src-layout` | Builds with hatchling from a `src/` layout | cont |
| **r** | `template/pre-commit-ci` | pre-commit.ci runs on pull requests | meta |
| **r** | `template/codecov` | A codecov config or a codecov check run exists | cont meta |
| **r** | `template/default-branch` | The default branch is called `main` | meta |
| **i** | `template/packages-json` | Listed in scverse.org's ecosystem index, under a category the dashboard knows | web |

### Documentation

| | Check | What it means | Needs |
|---|---|---|---|
| **R** | `docs/readthedocs-yaml` | A `.readthedocs.yaml` build config is committed | cont |
| **R** | `docs/rtd-linked` | The Read the Docs project of the same slug builds *this* repository | rtd |
| **R** | `docs/rtd-core-devs` | At least two scverse core devs can administer the RTD project | rtd |
| **R** | `docs/scverse-domain` | Documentation is served from `<package>.scverse.org`, not readthedocs.io | rtd meta |
| **r** | `docs/rtd-build` | The most recent build of the `stable` version succeeded | rtd |

The last four are `–` for a repo with no `.readthedocs.yaml` — that one missing file is
already the finding, and would otherwise be counted five times over.

### Packaging & release

| | Check | What it means | Needs |
|---|---|---|---|
| **R** | `packaging/pypi-org` | `ownership.organization` on PyPI is the scverse org | pypi |
| **R** | `packaging/trusted-publishing` | The latest release carries PEP 740 attestations naming this repo's workflow | pypi |
| **R** | `packaging/release-workflow` | Publishes via pypa/gh-action-pypi-publish with `id-token: write`, no password, in an environment | cont |
| **R** | `packaging/spec0-python` | `requires-python` has dropped the Python versions SPEC 0 has dropped | cont |
| **R** | `packaging/immutable-releases` | The repository has immutable releases turned on | admin |
| **r** | `packaging/releases-immutable` | The last few published releases are actually marked immutable | meta |
| **r** | `packaging/pypi-environment` | A deployment environment named `pypi` exists and has protection rules | admin |
| **r** | `packaging/version-sync` | The newest GitHub release tag matches the newest PyPI version | meta pypi |

`trusted-publishing` is not taken on trust: the Sigstore certificate inside the
attestation names the exact workflow and tag that published the artefact, e.g.
`github.com/scverse/scirpy/.github/workflows/release.yaml@refs/tags/v0.24.0`.

### Supply chain & CI security

| | Check | What it means | Needs |
|---|---|---|---|
| **R** | `security/actions-pinned` | Every `uses:` names a full 40-character commit SHA | cont |
| **r** | `security/precommit-pinned` | Every `rev:` in `.pre-commit-config.yaml` is a commit SHA | cont |
| **R** | `security/zizmor-clean` | A zizmor audit of every workflow, action and Dependabot config reports no findings | cont |
| **R** | `security/dependabot` | `.github/dependabot.yml` covers both the github-actions and pre-commit ecosystems | cont |
| **r** | `security/dependabot-alerts` | No unresolved Dependabot security alerts | sec |
| **r** | `security/secret-scanning` | Secret scanning and push protection are both enabled | admin |
| **r** | `security/private-vuln-reporting` | Researchers can report vulnerabilities privately | admin |
| **R** | `security/workflow-permissions` | Every workflow declares a top-level `permissions:`, and the repo default is read-only | cont admin |
| **r** | `security/persist-credentials` | `actions/checkout` steps set `persist-credentials: false` (zizmor `artipacked`) | cont |
| **r** | `security/dangerous-triggers` | `pull_request_target` does not check out untrusted PR code (zizmor `dangerous-triggers`) | cont |
| **r** | `security/security-md` | A security policy exists, in this repo or via the org's `.github` repo | meta |

`precommit-pinned` is red almost everywhere, because the template itself still pins
pre-commit hooks to tags. That is informative: the template is what needs changing.

`zizmor-clean` runs the real [zizmor](https://docs.zizmor.sh) — a project dependency, so
it is simply installed — over the workflows, composite actions and Dependabot config the
collector already fetched, written to a temp directory. It is deliberately not "is zizmor
configured": the template already brings the hook, so that only confirmed the template.
The audit covers everything zizmor can read rather than the paths a repo's own hook is
scoped to, which is how this repo once shipped an unaudited `dependabot.yml`. A repo's own
`# zizmor: ignore[…]` comments and `.github/zizmor.yml` are honoured — a triaged finding
is a decision, not a defect — and the `regular` persona keeps false positives out of a
required check. Findings need only `contents:read`; with a token the online audits (stale
and known-vulnerable action refs) run too, and the cell says which of the two it was.

### Branch protection & review

Rulesets (`GET /repos/{o}/{r}/rules/branches/{branch}`) are the primary source and need
only a read token. Repos still on *classic* branch protection return `[]` there, so these
fall back to `/branches/{b}/protection`, which needs `administration:read` — hence
`admin?`, and hence `?` rather than a failure for those repos when the App is absent.

| | Check | What it means | Needs |
|---|---|---|---|
| **R** | `branch/protected` | Neither force-pushes nor deletion are allowed on the default branch | meta admin? |
| **R** | `branch/requires-pr` | Changes to the default branch must go through a pull request | meta admin? |
| **R** | `branch/requires-review` | A pull request needs at least one approving review to merge | meta admin? |
| **r** | `branch/status-checks` | At least one status check must pass before merging | meta admin? |

### Governance & community

| | Check | What it means | Needs |
|---|---|---|---|
| **R** | `governance/license` | The repo carries a recognised OSI-approved license | meta |
| **r** | `governance/description-topics` | The repo has a description, a homepage and the `scverse` topic | meta |
| **i** | `governance/maintained` | Pushed to within the last year | meta |

## Repository selection

1. `GET /orgs/scverse/repos`, dropping private repos and forks. Archived repos are
   collected but listed separately in a collapsed footer.
2. Category comes from matching the repo URL against `project_home` in scverse.org's
   [`packages.json`](https://scverse.org/ecosystem-packages/packages.json):
   `core-datastructure`, `core-framework`, `core-infrastructure`, else `other`.
3. Everything else in the org is listed in [`config/repos.yaml`](config/repos.yaml),
   under either `include:` (scored, shown under "Other repositories") or `exclusions:`
   (listed in the footer with a reason). Heuristics get this wrong, which is why it is a
   hand-maintained list: `scverse.github.io` has plenty of code but is a website,
   `anndata-tutorials` has none but matters.
4. `repo-health audit-exclusions` reports an active, public, non-fork org repo that appears in none of the three, so new repos cannot silently vanish from the dashboard.
   Archived repos and forks need no entry.
   It runs monthly and on demand, into one issue labelled `repo-audit`.

### Waivers

An intentional deviation can be waived in `config/repos.yaml`; the cell renders grey with
the reason on hover rather than red.

```yaml
waivers:
  some-repo:
    packaging/pypi-org: "Published under a partner organisation by agreement"
```

## Authentication

The cron authenticates as the **`scverse-repo-health` GitHub App**, installed on the
scverse org with **read-only** permissions:

Set **exactly these five** under *Permissions & events → Repository permissions*, each to
**Read-only**, and nothing else. No organization or account permissions are needed.

| App settings UI | Workflow input | Endpoints it unlocks |
|---|---|---|
| **Metadata** | `permission-metadata` | `/orgs/{org}/repos`, `/repos/{o}/{r}`, `/rules/branches/{branch}`, `/community/profile` *(mandatory; GitHub grants it automatically)* |
| **Contents** | `permission-contents` | `/git/trees`, `/contents/{path}`, `/releases`, `/tags`, `/compare` |
| **Administration** | `permission-administration` | `/branches/{b}/protection`, `/immutable-releases`, `/environments`, `/actions/permissions/workflow`, `/private-vulnerability-reporting`, and `security_and_analysis` on the repo object |
| **Dependabot alerts** | `permission-vulnerability-alerts` | `/dependabot/alerts` |
| **Checks** | `permission-checks` | `/commits/{ref}/check-runs` |

The workflow inputs are not named after the UI labels — they follow the REST API. The two
that catch people out: "Dependabot alerts" is `vulnerability-alerts`, and check runs come
under `checks`, **not** `actions`.

The two lists must agree. `permission-*:` can only *narrow* what the installation holds,
so asking for anything it lacks fails the run with a 422 that does not name the culprit.
If you drop a permission from the App, drop the matching input too.

> **Adding a permission to an App does not reach existing installations.** GitHub queues
> it for review and the installation keeps its old set until an org owner accepts. If you
> granted Checks after installing and still get a 422, that acceptance is what is missing:
> **Organization settings → GitHub Apps → `scverse-repo-health` → Review request**.

Two setup mistakes worth recognising by their error:

| Symptom | Cause |
|---|---|
| `404` on `/users/scverse/installation` | the App exists but is not installed on the org |
| `422 The permissions requested are not granted to this installation` | a `permission-*:` input asks for something the installation lacks — or was granted on the App but never accepted on the installation |

Repository secrets on `scverse/repo-health`: `APP_ID`, `APP_PRIVATE_KEY`, `RTD_TOKEN`.
`APP_ID` is passed as `client-id`, which expects the App's **Client ID** (`Iv23…`); if the
secret holds the numeric **App ID** instead, switch the input to `app-id`.

Locally the tool falls back to `$GITHUB_TOKEN`, `$GH_TOKEN`, or `gh auth token`, and the
admin-only checks simply render as `?`. It also works with no credentials at all, subject
to the 60 requests/hour anonymous limit.

Read the Docs throttles unauthenticated API access after roughly eight requests a minute.
Set `$RTD_TOKEN` (any account's API token) to avoid a slow, backoff-heavy run.

## Development

```console
$ uv sync --all-groups
$ uv run pytest
$ uv run ruff check . && uv run ruff format --check .
$ uv run repo-health run --out site/ --repo scirpy --repo scanpy
$ python -m http.server -d site
```

The check catalogue lives in [`src/scverse_repo_health/checks/`](src/scverse_repo_health/checks/).
To add one, write a pure function and decorate it:

```python
@check(
    id="security/actions-pinned",
    tier=Tier.REQUIRED,
    category="Supply chain",
    title="Actions pinned to SHA",
    needs=("cont",),
    applies_to=lambda r: bool(r.workflows),
)
def actions_pinned(r: RepoData) -> CheckResult: ...
```

Then add a fixture-backed test in [`tests/`](tests/). Fixtures are snapshots of real
repositories, captured with:

```console
$ uv run python scripts/capture_fixture.py scirpy scanpy
```

[`config/core_devs.yaml`](config/core_devs.yaml) is a hand-maintained list of the core
team — a GitHub `login` and a `name` per person. It is deliberately committed rather than
read from [scverse.org/people](https://scverse.org/people) at run time, so the dashboard
does not depend on that page's structure. Keep it in step with the website by hand.

This repo is subject to its own dashboard, and is expected to be all green.

## License

MPL-2.0
