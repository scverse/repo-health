"""``repo-health`` command line interface."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Annotated

from cyclopts import App, Parameter
from rich.console import Console
from rich.table import Table

from ._log import log, setup_logging
from .collect import CollectionFailedError, CollectOptions, Results, collect, summarise
from .models import Status, Tier
from .registry import NEEDS, REGISTRY

app = App(
    name="repo-health",
    help="Health dashboard for the repositories of the scverse GitHub organisation.",
    version_flags=["--version"],
)
console = Console()

_repeatable = {"negative": ()}
RepoNames = Annotated[list[str], Parameter(name=["--repo"], help="Restrict to these repos (repeatable)", **_repeatable)]
CheckIds = Annotated[list[str], Parameter(name=["--check"], help="Restrict to these check ids", **_repeatable)]

STATUS_STYLE = {
    Status.PASS: "green",
    Status.FAIL: "red",
    Status.WARN: "yellow",
    Status.NA: "dim",
    Status.UNKNOWN: "blue",
}


def _options(
    org: str,
    repo: list[str] | None,
    cache: bool,
    include_archived: bool,
    concurrency: int,
    skip_pypi: bool = False,
    skip_rtd: bool = False,
    ignore_exclusions: bool = False,
) -> CollectOptions:
    return CollectOptions(
        org=org,
        repos=list(repo or []),
        include_archived=include_archived,
        cache=cache,
        concurrency=concurrency,
        skip_pypi=skip_pypi,
        skip_rtd=skip_rtd,
        ignore_exclusions=ignore_exclusions,
    )


@app.command(name="collect")
def collect_cmd(
    *,
    org: str = "scverse",
    repo: RepoNames | None = None,
    out: Path = Path("results.json"),
    cache: bool = True,
    include_archived: bool = False,
    concurrency: int = 8,
    skip_pypi: bool = False,
    skip_rtd: bool = False,
    slim: bool = True,
    verbose: bool = False,
) -> None:
    """Fetch every repo, run every check, and write results.json.

    Parameters
    ----------
    org
        GitHub organisation to scan.
    repo
        Restrict to these repositories (repeatable). Handy while developing.
    out
        Where to write the JSON artifact.
    cache
        Use the on-disk ETag cache (``--no-cache`` to bypass it).
    include_archived
        Score archived repositories too, instead of just listing them.
    concurrency
        Maximum concurrent GitHub requests.
    skip_pypi
        Do not talk to PyPI; the packaging checks render as unknown.
    skip_rtd
        Do not talk to Read the Docs; the docs checks render as unknown.
    slim
        Drop the raw file contents and API payloads the checks have already read.
        ``--no-slim`` keeps them, which is what you want when debugging a check.
    """
    setup_logging("DEBUG" if verbose else "INFO")
    opts = _options(org, repo, cache, include_archived, concurrency, skip_pypi, skip_rtd)
    results = asyncio.run(collect(opts))
    results.write(out, slim=slim)
    stats = summarise(results)
    console.print(
        f"Wrote [bold]{out}[/]: {stats['repos']} repos × {stats['checks']} checks, "
        f"{stats['overall']['percent']}% of scored cells passing"
    )
    _fail_on_collection_errors(results)


@app.command(name="render")
def render_cmd(
    results: Path = Path("results.json"),
    *,
    out: Path = Path("site"),
    previous: Path | None = None,
    verbose: bool = False,
) -> None:
    """Render a results.json into a static site.

    Parameters
    ----------
    results
        The JSON artifact written by ``collect``.
    out
        Output directory for the site.
    previous
        Last week's results.json. Cells that changed since then are outlined, which
        makes "what broke this week" the first thing you see.
    """
    setup_logging("DEBUG" if verbose else "INFO")
    from .render.site import render_site

    data = Results.read(results)
    render_site(data, out, _previous(previous))
    console.print(f"Wrote [bold]{out}/index.html[/] ({len(data.reports)} repos)")


def _fail_on_collection_errors(results: Results) -> None:
    """Failing *checks* never fail the command; failing to *collect* does.

    Called after the output has been written, so the artifact still exists.
    """
    if failed := results.meta.get("failed_repos"):
        for failure in failed:
            console.print(f"[red]collection failed:[/] {failure['repo']} — {failure['error']}")
        msg = f"{len(failed)} repositories could not be collected"
        raise CollectionFailedError(msg)


def _previous(path: Path | None) -> Results | None:
    """Load last week's results, tolerating a missing or unreadable file."""
    if path is None:
        return None
    if not path.is_file():
        log.warning(f"{path} not found; skipping the week-over-week comparison")
        return None
    try:
        return Results.read(path)
    except (OSError, ValueError, KeyError) as exc:
        log.warning(f"could not read {path} ({exc!r}); skipping the week-over-week comparison")
        return None


@app.command(name="run")
def run_cmd(
    *,
    org: str = "scverse",
    repo: RepoNames | None = None,
    out: Path = Path("site"),
    previous: Path | None = None,
    cache: bool = True,
    include_archived: bool = False,
    concurrency: int = 8,
    skip_pypi: bool = False,
    skip_rtd: bool = False,
    verbose: bool = False,
) -> None:
    """Collect and render in one go — what the weekly cron runs."""
    setup_logging("DEBUG" if verbose else "INFO")
    from .render.site import render_site

    opts = _options(org, repo, cache, include_archived, concurrency, skip_pypi, skip_rtd)
    results = asyncio.run(collect(opts))
    render_site(results, out, _previous(previous))
    stats = summarise(results)
    console.print(
        f"Wrote [bold]{out}/index.html[/]: {stats['repos']} repos, "
        f"{stats['overall']['percent']}% of scored cells passing"
    )
    _fail_on_collection_errors(results)


@app.command(name="check")
def check_cmd(
    repo: str,
    *,
    org: str = "scverse",
    check: CheckIds | None = None,
    cache: bool = True,
    skip_pypi: bool = False,
    skip_rtd: bool = False,
    verbose: bool = False,
) -> None:
    """Show one repository's checks as a table. The development loop.

    Parameters
    ----------
    repo
        Repository name, without the organisation.
    check
        Only run these check ids (repeatable).
    """
    setup_logging("DEBUG" if verbose else "INFO")
    # `check` is the development loop: it must be able to look at any repository,
    # including the ones the dashboard deliberately leaves out.
    opts = _options(
        org,
        [repo],
        cache,
        include_archived=True,
        concurrency=8,
        skip_pypi=skip_pypi,
        skip_rtd=skip_rtd,
        ignore_exclusions=True,
    )
    results = asyncio.run(collect(opts))
    if not results.reports:
        console.print(f"[red]No such repository:[/] {org}/{repo}")
        raise SystemExit(1)
    report = results.reports[0]
    wanted = set(check or [])

    table = Table(title=f"{report.repo.full_name} — {report.repo.category.label}", expand=True)
    table.add_column("", width=1)
    table.add_column("check", style="bold", no_wrap=True)
    table.add_column("T", width=1)
    table.add_column("detail", overflow="fold")
    last_category = None
    for spec in REGISTRY.all():
        if wanted and spec.id not in wanted:
            continue
        result = report.results.get(spec.id)
        if result is None:
            continue
        if spec.category != last_category:
            table.add_section()
            last_category = spec.category
        style = STATUS_STYLE[result.status]
        table.add_row(
            f"[{style}]{result.status.glyph}[/]",
            spec.id,
            spec.tier.short,
            f"[{style}]{result.detail}[/]" if result.status is Status.FAIL else result.detail,
        )
    console.print(table)
    passing, applicable = report.score()
    console.print(f"Required checks: [bold]{passing}/{applicable}[/] passing")
    for error in report.repo.errors:
        console.print(f"[yellow]collection note:[/] {error}")


@app.command(name="list-checks")
def list_checks_cmd(*, tier: str | None = None) -> None:
    """List every check with its tier, category and data requirements.

    Parameters
    ----------
    tier
        Only show this tier: required, recommended or informational.
    """
    wanted = Tier(tier) if tier else None
    table = Table(title=f"{len(REGISTRY)} checks", expand=True, pad_edge=False)
    table.add_column("id", style="bold", no_wrap=True)
    table.add_column("T", width=1, justify="center")
    table.add_column("needs", width=11, overflow="fold")
    table.add_column("description", ratio=1, overflow="fold")
    last_category = None
    for spec in REGISTRY.all():
        if wanted and spec.tier is not wanted:
            continue
        if spec.category != last_category:
            table.add_section()
            last_category = spec.category
        table.add_row(
            spec.id,
            spec.tier.short,
            " ".join(spec.needs) or "—",
            spec.description or spec.title,
        )
    console.print(table)
    console.print(
        "\n[bold]Tier[/]  R required (scored)  r recommended  i informational\n"
        "[bold]Needs[/] " + "  ".join(f"{k}={v}" for k, v in NEEDS.items())
    )


@app.command(name="audit-exclusions")
def audit_exclusions_cmd(*, org: str = "scverse", cache: bool = True) -> None:
    """Fail if an org repo is neither in packages.json nor explicitly excluded.

    This is what stops a new repository from silently vanishing from the dashboard.
    """
    setup_logging()

    async def go() -> tuple[list[str], list[str], list[str]]:
        from .config import ReposConfig
        from .sources import scverse
        from .sources.github import GitHubClient, resolve_token

        config = ReposConfig.load()
        token, auth_kind = resolve_token(org)
        async with GitHubClient(token, auth_kind=auth_kind, cache=cache) as gh:
            packages, repos = await asyncio.gather(scverse.fetch_packages(cache=cache), gh.org_repos(org))
        indexed = scverse.index_by_repo(packages, org)
        # Archived repos are shown in the footer regardless, so they need no decision.
        live = {r["name"] for r in repos if not r.get("private") and not r.get("fork") and not r.get("archived")}
        accounted = set(indexed) | config.include | set(config.exclusions)
        unaccounted = sorted(live - accounted)
        stale = sorted((config.include | set(config.exclusions)) - live)
        both = sorted(name for name in indexed if config.is_excluded(name) or name in config.include)
        return unaccounted, stale, both

    unaccounted, stale, both = asyncio.run(go())
    for name in unaccounted:
        console.print(
            f"[red]unaccounted for[/] {name} — add it to packages.json, "
            "or to `include:` / `exclusions:` in config/repos.yaml"
        )
    for name in stale:
        console.print(f"[yellow]stale entry[/] {name} — listed in config/repos.yaml but not an active org repo")
    for name in both:
        console.print(f"[yellow]listed twice[/] {name} — it is in packages.json and in config/repos.yaml")
    if not unaccounted:
        console.print("[green]Every active org repository is classified, included or excluded.[/]")
    raise SystemExit(1 if unaccounted else 0)


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover
        log.warning("interrupted")
        sys.exit(130)
    except CollectionFailedError as exc:
        console.print(f"[red]{exc}[/]")
        sys.exit(2)


if __name__ == "__main__":  # pragma: no cover
    main()
