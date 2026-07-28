from __future__ import annotations

import json
import re

import pytest

from scverse_repo_health.collect import Results
from scverse_repo_health.models import Status
from scverse_repo_health.render.site import build_context, render_site, worst

from .conftest import results_bundle


@pytest.fixture(scope="module")
def site(tmp_path_factory, request):
    from .conftest import load_repo

    out = tmp_path_factory.mktemp("site")
    results = results_bundle(load_repo("scirpy"), load_repo("scanpy"))
    render_site(results, out)
    return out


def test_site_has_the_expected_files(site):
    assert (site / "index.html").is_file()
    assert (site / "repo" / "scirpy.html").is_file()
    assert (site / "repo" / "scanpy.html").is_file()
    assert (site / "static" / "style.css").is_file()
    assert (site / "static" / "dashboard.js").is_file()
    assert (site / "results.json").is_file()
    assert (site / ".nojekyll").is_file(), "GitHub Pages must not run Jekyll over this"


def test_published_results_json_round_trips(site):
    again = Results.from_dict(json.loads((site / "results.json").read_text()))
    assert {r.repo.name for r in again.reports} == {"scirpy", "scanpy"}


def test_published_results_json_is_slim_but_still_renders(tmp_path):
    """The artifact drops raw payloads, and must still produce the identical site."""
    from .conftest import load_repo

    results = results_bundle(load_repo("scirpy"), load_repo("scanpy"))
    full = json.dumps(results.to_dict(slim=False))
    slim = json.dumps(results.to_dict())
    assert len(slim) < len(full) / 4, "slimming should be worth doing"
    assert "cookiecutter" in full and '"files"' in full
    assert '"files"' not in slim, "file contents are consumed at collect time"

    render_site(results, tmp_path / "a")
    render_site(Results.from_dict(json.loads(slim)), tmp_path / "b")
    assert (tmp_path / "a" / "index.html").read_text() == (tmp_path / "b" / "index.html").read_text()
    assert (tmp_path / "a" / "repo" / "scirpy.html").read_text() == (
        tmp_path / "b" / "repo" / "scirpy.html"
    ).read_text()


#: One matrix cell and the glyph it opens with. Group 1 is the rest of the class list, which
#: the tests filter on: the first column of a check group also carries `group-start`, and the
#: collapsed summaries `collapsed-only`.
CELL = re.compile(r'<td class="cell([^"]*)"[^>]*>\s*<(?:a|span) class="glyph[^>]*>')


def data_cells(html: str) -> list[str]:
    """The check cells, without the one-per-group collapsed summaries."""
    return [m.group(0) for m in CELL.finditer(html) if "collapsed-only" not in m.group(1)]


def test_index_renders_every_cell(site):
    html = (site / "index.html").read_text()
    # 2 repos × 40 checks, plus one collapsed-summary cell per column group per repo.
    assert len(data_cells(html)) == 2 * 40
    assert 'class="cell collapsed-only group-start"' in html
    for name in ("scirpy", "scanpy"):
        assert f'data-repo="{name}"' in html


def test_cells_carry_a_glyph_and_a_tooltip_not_just_colour(site):
    html = (site / "index.html").read_text()
    for glyph in ("✓", "✗", "!", "–", "?"):
        assert glyph in html
    # Every glyph inside the matrix has a title and an aria-label, so a cell's meaning
    # never rests on colour alone. (Legend glyphs are exempt: they sit beside their text.)
    cells = data_cells(html)
    assert len(cells) == 2 * 40
    assert all("title=" in tag and "aria-label=" in tag for tag in cells)


def test_failing_cells_deep_link_to_the_fix(site):
    html = (site / "index.html").read_text()
    assert re.search(r'<a class="glyph s-fail[^"]*" href="https://[^"]+"', html)


def test_matrix_rows_all_have_the_same_width(site):
    """A misaligned row would silently shift every cell under the wrong column."""
    from html.parser import HTMLParser

    class Widths(HTMLParser):
        def __init__(self):
            super().__init__()
            self.rows: list[int] = []
            self.width = 0
            self.in_row = False

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if tag == "tr":
                self.in_row, self.width = True, 0
            elif tag in ("td", "th") and self.in_row:
                self.width += int(attributes.get("colspan", 1))

        def handle_endtag(self, tag):
            if tag == "tr" and self.in_row:
                self.rows.append(self.width)
                self.in_row = False

    html = (site / "index.html").read_text()
    matrix = html.split('<table class="matrix">', 1)[1].split("</table>", 1)[0]
    parser = Widths()
    parser.feed(matrix)
    # 1 row header + 40 checks + 1 collapsed summary per column group.
    expected = 1 + 40 + 6
    # The second header row is one short: the corner cell spans into it via rowspan.
    assert parser.rows[0] == expected
    assert parser.rows[1] == expected - 1
    assert set(parser.rows[2:]) == {expected}


def test_index_lists_excluded_repos_with_their_reason(site):
    html = (site / "index.html").read_text()
    assert "event-template" in html
    assert "Template for event repositories" in html


def test_backticks_in_details_become_code_not_literal_backticks(site):
    from scverse_repo_health.render.site import inline_code

    assert str(inline_code("`.cruft.json` is missing")) == "<code>.cruft.json</code> is missing"
    assert str(inline_code("<script>")) == "&lt;script&gt;", "escaping happens before markup"

    html = (site / "repo" / "scanpy.html").read_text()
    assert "<code>.cruft.json</code>" in html
    assert "`.cruft.json`" not in html


def test_repo_page_shows_details_and_unknown_endpoints(site):
    html = (site / "repo" / "scirpy.html").read_text()
    assert "scverse/scirpy" in html
    assert "security/actions-pinned" in html
    assert "could not be read" in html, "the anonymous capture has unreadable endpoints"


def test_changes_are_marked_against_a_previous_run(tmp_path):
    from .conftest import load_repo

    current = results_bundle(load_repo("scirpy"))
    previous = results_bundle(load_repo("scirpy"))
    # Pretend zizmor was failing last week and dependabot was passing.
    previous.reports[0].results["security/zizmor"].status = Status.FAIL
    previous.reports[0].results["security/dependabot"].status = Status.PASS

    context = build_context(current, previous)
    assert context["changes"][("scirpy", "security/zizmor")] == "fixed"
    assert context["changes"][("scirpy", "security/dependabot")] == "regressed"

    render_site(current, tmp_path, previous)
    html = (tmp_path / "index.html").read_text()
    assert "changed-regressed" in html
    assert "changed-fixed" in html


def test_failed_repos_are_shown_not_swallowed(tmp_path):
    """A repo that blew up during collection must be visible, not silently missing."""
    from .conftest import load_repo

    results = results_bundle(load_repo("scirpy"))
    results.meta["failed_repos"] = [{"repo": "squidpy", "error": "ReadTimeout()"}]
    render_site(results, tmp_path)
    html = (tmp_path / "index.html").read_text()
    assert "could not be collected" in html
    assert "squidpy" in html
    assert "ReadTimeout()" in html


def test_no_previous_run_means_no_change_markers(site):
    assert "changed-regressed" not in (site / "index.html").read_text()


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([Status.PASS, Status.FAIL, Status.NA], Status.FAIL),
        ([Status.PASS, Status.WARN], Status.WARN),
        ([Status.PASS, Status.UNKNOWN], Status.UNKNOWN),
        ([Status.PASS, Status.PASS], Status.PASS),
        ([Status.NA], Status.NA),
        ([], Status.NA),
    ],
)
def test_group_summary_takes_the_worst_status(statuses, expected):
    from scverse_repo_health.models import CheckResult

    assert worst([CheckResult(s) for s in statuses])["status"] is expected


def test_tallies_ignore_na_and_unknown(site):
    from .conftest import load_repo

    context = build_context(results_bundle(load_repo("scirpy")))
    required = context["required"]
    assert required.scored == required.counts[Status.PASS] + required.counts[Status.FAIL] + required.counts[Status.WARN]
    assert 0 <= required.percent <= 100
