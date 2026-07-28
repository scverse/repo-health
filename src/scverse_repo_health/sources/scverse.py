"""scverse.org's ecosystem package index, which is what defines a repo's category."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from scverse_repo_health._log import log
from scverse_repo_health.models import Category

from ._http import DiskCache, cache_dir

if TYPE_CHECKING:
    from typing import Any

PACKAGES_URL = "https://scverse.org/ecosystem-packages/packages.json"

_CATEGORIES = {c.value: c for c in Category}


async def fetch_packages(*, cache: bool = True) -> list[dict[str, Any]]:
    """The raw ``packages.json`` list.

    Falls back to a stale cached copy if scverse.org is unreachable — a website outage
    should degrade the category column, not fail the whole run.
    """
    disk = DiskCache(cache_dir() / "scverse", enabled=cache)
    disk.directory.mkdir(parents=True, exist_ok=True)
    cached = disk.get(PACKAGES_URL)
    headers = {"If-None-Match": cached[0]} if cached and cached[0] else {}
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(PACKAGES_URL, headers=headers)
        if response.status_code == 304:  # noqa: PLR2004
            assert cached is not None
            return cached[1]
        response.raise_for_status()
    except httpx.HTTPError as exc:
        if cached:
            log.warning(f"packages.json unreachable ({exc!r}); using cached copy")
            return cached[1]
        raise
    body = response.json()
    disk.set(PACKAGES_URL, response.headers.get("etag"), body)
    return body


def index_by_repo(packages: list[dict[str, Any]], org: str = "scverse") -> dict[str, dict[str, Any]]:
    """Map repo name -> package entry, for packages whose ``project_home`` is in ``org``."""
    prefix = f"github.com/{org}/".lower()
    index: dict[str, dict[str, Any]] = {}
    for entry in packages:
        home = (entry.get("project_home") or "").rstrip("/")
        low = home.lower()
        if prefix not in low:
            continue
        repo = home[low.index(prefix) + len(prefix) :].removesuffix(".git")
        if repo and "/" not in repo:
            index[repo] = entry
    return index


def category_of(entry: dict[str, Any] | None) -> Category:
    """``packages.json`` category string -> :class:`~..models.Category`."""
    if not entry:
        return Category.OTHER
    return _CATEGORIES.get((entry.get("category") or "").strip().lower(), Category.OTHER)
