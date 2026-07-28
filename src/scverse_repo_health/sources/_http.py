"""Shared plumbing for the HTTP sources: an on-disk ETag cache and a throttled client."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from scverse_repo_health._log import log

if TYPE_CHECKING:
    from typing import Any


class ForbiddenError(Exception):
    """403 — we are not allowed to read this. Renders as ``UNKNOWN``, never ``FAIL``."""


class NotFoundError(Exception):
    """404 — the resource genuinely is not there."""


def cache_dir() -> Path:
    root = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    path = Path(root) / "scverse-repo-health"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(slots=True)
class DiskCache:
    """Conditional-request cache.

    GitHub does not count a ``304 Not Modified`` against the rate limit, so keeping
    ETags around makes repeated local runs nearly free.
    """

    directory: Path
    enabled: bool = True

    def _path(self, key: str) -> Path:
        return self.directory / f"{hashlib.sha256(key.encode()).hexdigest()}.json"

    def get(self, key: str) -> tuple[str, Any] | None:
        if not self.enabled:
            return None
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return entry.get("etag"), entry.get("body")

    def set(self, key: str, etag: str | None, body: Any) -> None:
        if not self.enabled or not etag:
            return
        try:
            self._path(key).write_text(json.dumps({"etag": etag, "body": body}), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - cache is best-effort
            log.debug(f"could not write cache entry: {exc!r}")

    def clear(self) -> int:
        count = 0
        for path in self.directory.glob("*.json"):
            path.unlink(missing_ok=True)
            count += 1
        return count


class Throttle:
    """Bound concurrency and, optionally, requests per minute."""

    def __init__(self, concurrency: int = 8, per_minute: float | None = None) -> None:
        self._sem = asyncio.Semaphore(concurrency)
        self._interval = 60 / per_minute if per_minute else 0.0
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def __aenter__(self) -> None:
        await self._sem.acquire()
        if self._interval:
            async with self._lock:
                loop = asyncio.get_running_loop()
                wait = self._next_at - loop.time()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._next_at = max(loop.time(), self._next_at) + self._interval

    async def __aexit__(self, *_exc: object) -> None:
        self._sem.release()
