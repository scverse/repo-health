"""Python release dates, which are what SPEC 0 measures its support window from."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from scverse_repo_health._log import log

from ._http import DiskCache, cache_dir

if TYPE_CHECKING:
    from typing import Any

RELEASES_URL = "https://endoflife.date/api/python.json"


async def fetch_releases(*, cache: bool = True) -> list[dict[str, Any]] | None:
    """``cycle`` and ``releaseDate`` for every Python version.

    Returns ``None`` when endoflife.date is unreachable and nothing is cached.
    """
    disk = DiskCache(cache_dir() / "python", enabled=cache)
    disk.directory.mkdir(parents=True, exist_ok=True)
    cached = disk.get(RELEASES_URL)
    headers = {"If-None-Match": cached[0]} if cached and cached[0] else {}
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(RELEASES_URL, headers=headers)
        if response.status_code == 304:  # noqa: PLR2004
            assert cached is not None
            return cached[1]
        response.raise_for_status()
    except httpx.HTTPError as exc:
        if cached:
            log.warning(f"endoflife.date unreachable ({exc!r}); using cached copy")
            return cached[1]
        log.warning(f"endoflife.date unreachable ({exc!r}); the SPEC 0 minimum is unknown")
        return None
    body = response.json()
    disk.set(RELEASES_URL, response.headers.get("etag"), body)
    return body
