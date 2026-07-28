"""GitHub REST client.

Deliberately not PyGithub: several endpoints we need (``/rules/branches/``,
``/immutable-releases``, ``/private-vulnerability-reporting``) are not wrapped, and we
want async fan-out plus ETag caching.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from scverse_repo_health._log import log
from scverse_repo_health.backoff import aretry_with_backoff

from ._http import DiskCache, ForbiddenError, NotFoundError, Throttle, cache_dir

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any, Self

API = "https://api.github.com"
ACCEPT = "application/vnd.github+json"
API_VERSION = "2022-11-28"


class RetryableStatusError(Exception):
    """5xx or a secondary rate limit — worth another go."""


def token_from_env() -> str | None:
    """``$GITHUB_TOKEN`` / ``$GH_TOKEN``, else whatever ``gh`` is logged in with."""
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if tok := os.environ.get(var):
            return tok
    try:
        out = subprocess.run(
            ["gh", "auth", "token"],  # noqa: S607 - `gh` on PATH is exactly what we want
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _app_jwt(app_id: str, private_key: str) -> str:
    """Sign a short-lived RS256 JWT for a GitHub App (no PyJWT dependency)."""
    # Imported here: only the GitHub App path needs them.
    from cryptography.hazmat.primitives import hashes, serialization  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric import padding  # noqa: PLC0415

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    now = int(time.time())
    header = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}).encode())
    signing_input = f"{header}.{payload}".encode()
    key = serialization.load_pem_private_key(private_key.encode(), password=None)
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr]
    return f"{header}.{payload}.{b64(signature)}"


def installation_token(app_id: str, private_key: str, org: str) -> str:
    """Exchange App credentials for an installation token scoped to ``org``.

    CI normally uses ``actions/create-github-app-token`` and passes the result in as
    ``$GITHUB_TOKEN``; this is the local-development path.
    """
    headers = {"Authorization": f"Bearer {_app_jwt(app_id, private_key)}", "Accept": ACCEPT}
    with httpx.Client(base_url=API, headers=headers, timeout=30) as client:
        installations = client.get("/app/installations").raise_for_status().json()
        for inst in installations:
            if (inst.get("account") or {}).get("login", "").lower() == org.lower():
                url = f"/app/installations/{inst['id']}/access_tokens"
                return client.post(url).raise_for_status().json()["token"]
    msg = f"GitHub App {app_id} is not installed on {org!r}"
    raise RuntimeError(msg)


def resolve_token(org: str = "scverse") -> tuple[str | None, str]:
    """Return ``(token, how_we_got_it)``, preferring App credentials."""
    app_id = os.environ.get("APP_ID")
    private_key = os.environ.get("APP_PRIVATE_KEY")
    if app_id and private_key:
        try:
            return installation_token(app_id, private_key, org), "github-app"
        except (httpx.HTTPError, RuntimeError) as exc:
            log.warning(f"could not mint an installation token ({exc!r}); falling back")
    if tok := token_from_env():
        return tok, "token"
    return None, "anonymous"


@dataclass(slots=True)
class RateLimit:
    limit: int = 0
    remaining: int = 0
    reset: int = 0

    @property
    def used(self) -> int:
        return self.limit - self.remaining


class GitHubClient:
    """Async GitHub client with an ETag cache and 403-aware error types."""

    def __init__(
        self,
        token: str | None = None,
        *,
        auth_kind: str = "anonymous",
        concurrency: int = 8,
        cache: bool = True,
        cache_path: Path | None = None,
    ) -> None:
        self.token = token
        self.auth_kind = auth_kind
        self.cache = DiskCache(cache_path or cache_dir() / "github", enabled=cache)
        self.cache.directory.mkdir(parents=True, exist_ok=True)
        self.throttle = Throttle(concurrency)
        self.rate = RateLimit()
        self.requests = 0
        self.cache_hits = 0
        headers = {"Accept": ACCEPT, "X-GitHub-Api-Version": API_VERSION, "User-Agent": "scverse-repo-health"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(base_url=API, headers=headers, timeout=30, follow_redirects=True)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._client.aclose()

    def _note_rate(self, response: httpx.Response) -> None:
        h = response.headers
        if "x-ratelimit-limit" in h:
            self.rate = RateLimit(
                limit=int(h["x-ratelimit-limit"]),
                remaining=int(h.get("x-ratelimit-remaining", 0)),
                reset=int(h.get("x-ratelimit-reset", 0)),
            )

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        accept: str | None = None,
    ) -> Any:
        """GET ``path`` and return decoded JSON. Raises :class:`ForbiddenError`/:class:`NotFoundError`."""
        key = f"{path}?{sorted((params or {}).items())}&{accept or ''}"
        cached = self.cache.get(key)
        headers = {"Accept": accept} if accept else {}
        if cached and cached[0]:
            headers["If-None-Match"] = cached[0]

        async def once() -> httpx.Response:
            async with self.throttle:
                self.requests += 1
                response = await self._client.get(path, params=params, headers=headers)
            self._note_rate(response)
            retryable = response.status_code >= 500 or (  # noqa: PLR2004
                response.status_code == 403 and "secondary rate limit" in response.text.lower()  # noqa: PLR2004
            )
            if retryable:
                msg = f"{response.status_code} on {path}"
                raise RetryableStatusError(msg)
            return response

        response = await aretry_with_backoff(once, retries=4, exc_cls=RetryableStatusError)

        if response.status_code == 304:  # noqa: PLR2004
            self.cache_hits += 1
            assert cached is not None
            return cached[1]
        if response.status_code in (401, 403):
            msg = f"{response.status_code} on {path}: {_message(response)}"
            raise ForbiddenError(msg)
        if response.status_code == 404:  # noqa: PLR2004
            msg = f"404 on {path}"
            raise NotFoundError(msg)
        response.raise_for_status()
        body = response.json() if response.content else None
        self.cache.set(key, response.headers.get("etag"), body)
        return body

    async def try_get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        privileged: bool = False,
    ) -> tuple[Any, str | None]:
        """GET, mapping failures onto ``(value, reason_unavailable)``.

        A 404 normally yields ``(None, None)`` — genuinely absent, which most checks read
        as a clean FAIL. Anything else yields a reason, which renders as UNKNOWN.

        ``privileged=True`` marks an endpoint that needs ``administration:read``. GitHub
        hides those behind a 404 rather than a 403 for callers that may not even know the
        resource exists, so without a token we must report UNKNOWN instead of inventing a
        failure.
        """
        try:
            return await self.get(path, params), None
        except NotFoundError:
            if privileged and self.auth_kind == "anonymous":
                return None, f"404 on {path} without credentials (needs administration:read)"
            return None, None
        except ForbiddenError as exc:
            return None, str(exc)
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            return None, f"{type(exc).__name__}: {exc}"

    async def paginate(self, path: str, params: dict[str, Any] | None = None, *, limit: int = 1000) -> list[Any]:
        """Follow ``page``/``per_page`` until exhausted or ``limit`` items collected."""
        out: list[Any] = []
        page = 1
        while len(out) < limit:
            batch = await self.get(path, {**(params or {}), "per_page": 100, "page": page})
            if not isinstance(batch, list) or not batch:
                break
            out.extend(batch)
            if len(batch) < 100:  # noqa: PLR2004
                break
            page += 1
        return out[:limit]

    # -- endpoint helpers ---------------------------------------------------------------

    async def org_repos(self, org: str) -> list[dict[str, Any]]:
        return await self.paginate(f"/orgs/{org}/repos", {"type": "all", "sort": "full_name"})

    async def tree(self, full_name: str, branch: str) -> list[str]:
        """Every path in ``branch``, from a single recursive call."""
        data = await self.get(f"/repos/{full_name}/git/trees/{branch}", {"recursive": "1"})
        paths = [e["path"] for e in data.get("tree", []) if e.get("type") == "blob"]
        if data.get("truncated"):
            log.warning(f"{full_name}: git tree truncated, some file checks may be wrong")
        return paths

    async def file(self, full_name: str, path: str, branch: str) -> str | None:
        """Raw text of a file, or ``None`` if it is missing, unreadable or not decodable."""
        try:
            data = await self.get(f"/repos/{full_name}/contents/{path}", {"ref": branch})
        except (NotFoundError, ForbiddenError):
            return None
        except httpx.HTTPError as exc:
            log.warning(f"{full_name}: could not read {path} ({exc!r})")
            return None
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            return None
        try:
            return base64.b64decode(data["content"]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None

    async def rate_limit(self) -> dict[str, Any]:
        return await self.get("/rate_limit")


def _message(response: httpx.Response) -> str:
    try:
        return str(response.json().get("message", ""))[:200]
    except (json.JSONDecodeError, ValueError):
        return response.text[:200]


async def gather_dict(**coros: Any) -> dict[str, Any]:
    """``asyncio.gather`` that keeps the names, returning exceptions rather than raising."""
    keys = list(coros)
    values = await asyncio.gather(*(coros[k] for k in keys), return_exceptions=True)
    return dict(zip(keys, values, strict=True))
