"""Retry helpers.

Adapted from ``cookiecutter-scverse/scripts/src/scverse_template_scripts/backoff.py``,
with an async variant because every source client here is async.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import TYPE_CHECKING

from ._log import log

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


def _sleep_for(attempt: int, backoff_in_seconds: float) -> float:
    return backoff_in_seconds * 2**attempt + random.uniform(0, 1)


def retry_with_backoff[T](
    fn: Callable[[], T],
    retries: int = 5,
    backoff_in_seconds: float = 1,
    exc_cls: type[Exception] = Exception,
) -> T:
    exc: Exception | None = None
    for attempt in range(retries):
        try:
            return fn()
        except exc_cls as _exc:
            exc = _exc
            sleep = _sleep_for(attempt, backoff_in_seconds)
            log.info(f"Action failed ({_exc!r}). Retrying in {sleep:.1f}s.")
            time.sleep(sleep)
    assert exc is not None
    raise exc


async def aretry_with_backoff[T](
    fn: Callable[[], Awaitable[T]],
    retries: int = 5,
    backoff_in_seconds: float = 1,
    exc_cls: type[Exception] = Exception,
) -> T:
    exc: Exception | None = None
    for attempt in range(retries):
        try:
            return await fn()
        except exc_cls as _exc:
            exc = _exc
            sleep = _sleep_for(attempt, backoff_in_seconds)
            log.info(f"Action failed ({_exc!r}). Retrying in {sleep:.1f}s.")
            await asyncio.sleep(sleep)
    assert exc is not None
    raise exc
