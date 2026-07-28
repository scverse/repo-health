#!/usr/bin/env python
"""Capture one repository's collected data as a test fixture.

    uv run python scripts/capture_fixture.py scirpy

Writes ``tests/fixtures/<repo>.json`` — the serialised :class:`RepoData`, i.e. exactly
what the checks see. Tests then exercise the whole catalogue offline.

Re-run it when an upstream API changes shape; commit the diff so the change is reviewable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from scverse_repo_health._log import setup_logging
from scverse_repo_health.collect import CollectOptions, collect

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repos", nargs="+")
    parser.add_argument("--org", default="scverse")
    parser.add_argument("--out", type=Path, default=FIXTURES)
    parser.add_argument("--skip-pypi", action="store_true")
    parser.add_argument("--skip-rtd", action="store_true")
    args = parser.parse_args()

    setup_logging()
    results = asyncio.run(
        collect(
            CollectOptions(
                org=args.org,
                repos=args.repos,
                include_archived=True,
                skip_pypi=args.skip_pypi,
                skip_rtd=args.skip_rtd,
            )
        )
    )
    args.out.mkdir(parents=True, exist_ok=True)
    for report in results.reports:
        path = args.out / f"{report.repo.name}.json"
        path.write_text(json.dumps(report.repo.to_dict(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {path} ({path.stat().st_size // 1024} KiB)")
    if not results.reports:
        print("no repositories collected")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
