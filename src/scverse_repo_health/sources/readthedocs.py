"""Read the Docs API v3.

The unauthenticated API throttles hard (roughly eight requests a minute), so this client
is rate-limited by default and retries with exponential backoff. Set ``$RTD_TOKEN`` — any
account's API token will do — to get a comfortable limit.

RTD *organizations* are a Business-plan feature and no scverse project has one, so
"is this project owned by scverse" is instead answered by two things we can see:
the project points at a scverse GitHub repo, and at least two of its maintainers are
scverse core devs.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import httpx

from scverse_repo_health._log import log
from scverse_repo_health.backoff import aretry_with_backoff

from ._http import DiskCache, Throttle, cache_dir

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any, Self

BASE = "https://app.readthedocs.org/api/v3"
#: Conservative default; the observed unauthenticated limit is ~8/min.
ANON_PER_MINUTE = 6.0
AUTH_PER_MINUTE = 60.0


class ThrottledError(Exception):
    """429 from RTD."""


class RTDClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        cache: bool = True,
        cache_path: Path | None = None,
    ) -> None:
        self.token = token or os.environ.get("RTD_TOKEN")
        self.cache = DiskCache(cache_path or cache_dir() / "rtd", enabled=cache)
        self.cache.directory.mkdir(parents=True, exist_ok=True)
        self.throttle = Throttle(
            concurrency=4 if self.token else 1,
            per_minute=AUTH_PER_MINUTE if self.token else ANON_PER_MINUTE,
        )
        headers = {"User-Agent": "scverse-repo-health"}
        if self.token:
            headers["Authorization"] = f"Token {self.token}"
        self._client = httpx.AsyncClient(base_url=BASE, timeout=30, headers=headers, follow_redirects=True)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._client.aclose()

    async def _get(self, path: str) -> Any:
        cached = self.cache.get(path)
        headers = {"If-None-Match": cached[0]} if cached and cached[0] else {}

        async def once() -> httpx.Response:
            async with self.throttle:
                return await self._client.get(path, headers=headers)

        response = await aretry_with_backoff(
            lambda: _raise_if_throttled(once()),
            retries=5,
            backoff_in_seconds=5,
            exc_cls=ThrottledError,
        )
        if response.status_code == 304:  # noqa: PLR2004
            assert cached is not None
            return cached[1]
        if response.status_code in (401, 403, 404):
            return None
        response.raise_for_status()
        body = response.json()
        self.cache.set(path, response.headers.get("etag"), body)
        return body

    async def project(self, slug: str) -> dict[str, Any] | None:
        """Project metadata plus the latest build, or ``None`` if there is no such slug."""
        data = await self._get(f"/projects/{slug}/")
        if not data:
            return None
        out = {
            "slug": data.get("slug"),
            "name": data.get("name"),
            "repository": data.get("repository") or {},
            "urls": data.get("urls") or {},
            "users": [u.get("username") for u in data.get("users") or []],
            "default_branch": data.get("default_branch"),
            "readthedocs_yaml_path": data.get("readthedocs_yaml_path"),
            "home": f"https://app.readthedocs.org/projects/{data.get('slug')}/",
        }
        out["stable_build"] = await self.stable_build(slug)
        return out

    async def stable_build(self, slug: str) -> dict[str, Any] | None:
        """The most recent build of the ``stable`` version, or ``None`` if there is none.

        Not the project's most recent build: on an active repo that is almost always a
        pull-request preview (scirpy's latest was version ``714``), which says nothing
        about whether the docs people actually read still build.
        """
        data = await self._get(f"/projects/{slug}/versions/stable/builds/?limit=1")
        results = (data or {}).get("results") or []
        if not results:
            return None
        build = results[0]
        return {
            "id": build.get("id"),
            "success": build.get("success"),
            "state": (build.get("state") or {}).get("code"),
            "finished": build.get("finished"),
            "version": build.get("version"),
            "commit": build.get("commit"),
            "url": f"https://app.readthedocs.org/projects/{slug}/builds/{build.get('id')}/",
        }


async def _raise_if_throttled(awaitable: Any) -> httpx.Response:
    response: httpx.Response = await awaitable
    if response.status_code == 429:  # noqa: PLR2004
        log.info("Read the Docs throttled us; backing off (set $RTD_TOKEN to avoid this)")
        msg = "429 from Read the Docs"
        raise ThrottledError(msg)
    return response
