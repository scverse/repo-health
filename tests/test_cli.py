"""Unit tests for the CLI helpers that do not do I/O."""

from __future__ import annotations

from scverse_repo_health.cli import _audit_markdown


def test_audit_markdown_omits_empty_sections_and_does_not_link_stale_repos():
    body = _audit_markdown("scverse", ["liana"], ["scanpy-tutorials"], [])

    assert "- [ ] [`liana`](https://github.com/scverse/liana)" in body
    assert "- [ ] `scanpy-tutorials`" in body
    assert "scverse/scanpy-tutorials" not in body
    assert "### Listed twice" not in body
