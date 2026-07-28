"""Turn a :class:`~..collect.Results` into ``site/``.

Reads only the JSON artifact, never the network, so the whole visual layer can be
iterated on offline against a captured file.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import ChainableUndefined, Environment, PackageLoader, select_autoescape
from markupsafe import Markup, escape

from scverse_repo_health._log import log
from scverse_repo_health.models import CATEGORY_ORDER, Category, Status, Tier

if TYPE_CHECKING:
    from typing import Any

    from scverse_repo_health.collect import Results
    from scverse_repo_health.models import CheckResult, RepoReport

HERE = Path(__file__).parent
#: Only these statuses count towards a score; NA and UNKNOWN are excluded.
SCORED = (Status.PASS, Status.FAIL, Status.WARN)


@dataclass(frozen=True, slots=True)
class CheckSpec:
    """A column, as recorded in ``results.json``."""

    id: str
    tier: Tier
    category: str
    title: str
    description: str
    needs: tuple[str, ...]

    @property
    def anchor(self) -> str:
        return self.id.replace("/", "-")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CheckSpec:
        return cls(
            id=d["id"],
            tier=Tier(d.get("tier", "recommended")),
            category=d.get("category", "Other"),
            title=d.get("title", d["id"]),
            description=d.get("description", ""),
            needs=tuple(d.get("needs") or []),
        )


@dataclass(slots=True)
class Tally:
    """Counts behind a stat tile."""

    label: str
    counts: dict[Status, int]

    @property
    def scored(self) -> int:
        return sum(self.counts[s] for s in SCORED)

    @property
    def percent(self) -> int:
        return round(100 * self.counts[Status.PASS] / self.scored) if self.scored else 0

    @property
    def failing(self) -> int:
        return self.counts[Status.FAIL]


def tally(label: str, results: list[CheckResult]) -> Tally:
    counts = dict.fromkeys(Status, 0)
    for result in results:
        counts[result.status] += 1
    return Tally(label, counts)


#: Worst-first, for collapsing a column group down to one cell.
SEVERITY = [Status.FAIL, Status.WARN, Status.UNKNOWN, Status.PASS, Status.NA]


def worst(results: list[CheckResult]) -> dict[str, Any]:
    """The most severe status in a column group, plus how many cells share it."""
    if not results:
        return {"status": Status.NA, "detail": "Nothing in this group applies"}
    status = next(s for s in SEVERITY if any(r.status is s for r in results))
    count = sum(1 for r in results if r.status is status)
    return {
        "status": status,
        "detail": f"{count} of {len(results)} {status.label} — expand the group for detail",
    }


def _score(report: RepoReport, specs: dict[str, CheckSpec], tier: Tier | None = None) -> Tally:
    chosen = [
        result
        for check_id, result in report.results.items()
        if tier is None or (check_id in specs and specs[check_id].tier is tier)
    ]
    return tally(report.repo.name, chosen)


def _changes(results: Results, previous: Results | None) -> dict[tuple[str, str], str]:
    """``(repo, check) -> 'regressed' | 'fixed'`` against last week's run."""
    if previous is None:
        return {}
    before = {
        (report.repo.name, check_id): result.status
        for report in previous.reports
        for check_id, result in report.results.items()
    }
    changes: dict[tuple[str, str], str] = {}
    for report in results.reports:
        for check_id, result in report.results.items():
            key = (report.repo.name, check_id)
            old = before.get(key)
            if old is None or old == result.status:
                continue
            if result.status is Status.FAIL:
                changes[key] = "regressed"
            elif old is Status.FAIL:
                changes[key] = "fixed"
    return changes


def build_context(results: Results, previous: Results | None = None) -> dict[str, Any]:
    """Everything the templates need, computed once."""
    specs = [CheckSpec.from_dict(c) for c in results.checks]
    by_id = {s.id: s for s in specs}
    columns: dict[str, list[CheckSpec]] = {}
    for spec in specs:
        columns.setdefault(spec.category, []).append(spec)

    reports = results.sorted_reports()
    groups = []
    for category in CATEGORY_ORDER:
        members = [r for r in reports if r.repo.category is category and not r.repo.archived]
        if members:
            groups.append((category, members))
    archived = [r for r in reports if r.repo.archived]

    everything = [result for r in reports for result in r.results.values()]
    return {
        "base": ".",
        "group_status": {
            r.repo.name: {
                category: worst([r.results[s.id] for s in specs_in if s.id in r.results])
                for category, specs_in in columns.items()
            }
            for r in reports
        },
        "results": results,
        "generated": results.generated,
        "org": results.org,
        "meta": results.meta,
        "template": results.template,
        "specs": specs,
        "by_id": by_id,
        "columns": columns,
        "groups": groups,
        "archived": archived,
        "excluded": results.excluded,
        "overall": tally("Overall", everything),
        "required": tally(
            "Required checks",
            [
                res
                for r in reports
                for cid, res in r.results.items()
                if by_id.get(cid) and by_id[cid].tier is Tier.REQUIRED
            ],
        ),
        # Scored the same way as the hero tile — required checks only — so the numbers
        # on the page are all answering the same question.
        "group_tallies": {
            str(category): tally(
                category.label,
                [
                    res
                    for r in members
                    for cid, res in r.results.items()
                    if by_id.get(cid) and by_id[cid].tier is Tier.REQUIRED
                ],
            )
            for category, members in groups
        },
        "repo_scores": {r.repo.name: _score(r, by_id, Tier.REQUIRED) for r in reports},
        "changes": _changes(results, previous),
        "has_previous": previous is not None,
        "Status": Status,
        "Tier": Tier,
        "Category": Category,
    }


def _environment() -> Environment:
    env = Environment(
        loader=PackageLoader("scverse_repo_health.render", "templates"),
        autoescape=select_autoescape(["html", "xml", "html.j2"]),
        # The templates walk JSON dicts whose optional keys are genuinely absent
        # (`pypi.version` on an unpublished package, say), so a missing key renders as
        # nothing rather than raising.
        undefined=ChainableUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["status_class"] = lambda status: f"s-{status}"
    env.filters["code"] = inline_code
    return env


_BACKTICKS = re.compile(r"`([^`]+)`")


def inline_code(text: object) -> Markup:
    """Render the backticks that check details are written with as ``<code>``.

    Escapes first and substitutes second, so the only markup that survives is ours.
    """
    return Markup(_BACKTICKS.sub(r"<code>\1</code>", str(escape(text))))  # noqa: S704 - escaped above


def render_site(results: Results, out: Path, previous: Results | None = None) -> None:
    """Write ``index.html``, one page per repo, the static assets and ``results.json``."""
    out = Path(out)
    (out / "repo").mkdir(parents=True, exist_ok=True)
    env = _environment()
    context = build_context(results, previous)

    (out / "index.html").write_text(env.get_template("dashboard.html.j2").render(**context), encoding="utf-8")

    repo_template = env.get_template("repo.html.j2")
    for report in results.reports:
        page = repo_template.render({**context, "base": "..", "report": report})
        (out / "repo" / f"{report.repo.name}.html").write_text(page, encoding="utf-8")

    static_out = out / "static"
    if static_out.exists():
        shutil.rmtree(static_out)
    shutil.copytree(HERE / "static", static_out)

    (out / "results.json").write_text(json.dumps(results.to_dict(), indent=1) + "\n", encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    log.info(f"rendered {len(results.reports)} repos into {out}")
