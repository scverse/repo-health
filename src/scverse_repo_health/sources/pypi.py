"""PyPI: project ownership and PEP 740 publish provenance.

Two facts we want are not in the ordinary JSON API:

* **Org ownership** *is* — ``ownership.organization`` (verified present for scirpy,
  scanpy, anndata, pertpy, squidpy).
* **Trusted publishing** is provable through the attestations: ``/simple/<pkg>/`` gives a
  per-file ``provenance`` URL, and the bundle behind it names the publishing repo and
  workflow. We read PyPI's structured ``publisher`` block *and* independently decode the
  Sigstore certificate's SAN, which is the cryptographic ground truth:
  ``https://github.com/scverse/scirpy/.github/workflows/release.yaml@refs/tags/v0.24.0``.
"""

from __future__ import annotations

import base64
import re
from typing import TYPE_CHECKING

import httpx

from scverse_repo_health._log import log

from ._http import DiskCache, Throttle, cache_dir

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any, Self

BASE = "https://pypi.org"
SIMPLE_ACCEPT = "application/vnd.pypi.simple.v1+json"

_SAN_RE = re.compile(r"^https://github\.com/(?P<repo>[^/]+/[^/]+)/(?P<workflow>\.github/workflows/[^@]+)@(?P<ref>.+)$")


def normalise(name: str) -> str:
    """PEP 503 normalisation."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_san(uri: str) -> dict[str, str] | None:
    """Split a Sigstore SAN URI into repo / workflow / ref."""
    m = _SAN_RE.match(uri)
    return m.groupdict() if m else None


def certificate_san(certificate_b64: str) -> str | None:
    """Pull the single SAN URI out of a base64 DER Sigstore certificate."""
    from cryptography import x509  # noqa: PLC0415 - only needed when provenance exists

    try:
        cert = x509.load_der_x509_certificate(base64.b64decode(certificate_b64))
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        uris = san.get_values_for_type(x509.UniformResourceIdentifier)
    except Exception as exc:
        log.debug(f"could not decode Sigstore certificate: {exc!r}")
        return None
    return str(uris[0]) if uris else None


class PyPIClient:
    def __init__(self, *, concurrency: int = 8, cache: bool = True, cache_path: Path | None = None) -> None:
        self.cache = DiskCache(cache_path or cache_dir() / "pypi", enabled=cache)
        self.cache.directory.mkdir(parents=True, exist_ok=True)
        self.throttle = Throttle(concurrency)
        self._client = httpx.AsyncClient(
            base_url=BASE,
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": "scverse-repo-health"},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._client.aclose()

    async def _get(self, path: str, accept: str | None = None) -> Any:
        cached = self.cache.get(path)
        headers = {"Accept": accept} if accept else {}
        if cached and cached[0]:
            headers["If-None-Match"] = cached[0]
        async with self.throttle:
            response = await self._client.get(path, headers=headers)
        if response.status_code == 304:  # noqa: PLR2004
            assert cached is not None
            return cached[1]
        if response.status_code == 404:  # noqa: PLR2004
            return None
        response.raise_for_status()
        body = response.json()
        self.cache.set(path, response.headers.get("etag"), body)
        return body

    async def project(self, name: str) -> dict[str, Any] | None:
        """The whole picture for one distribution, or ``None`` if it is not on PyPI."""
        meta = await self._get(f"/pypi/{name}/json")
        if meta is None:
            return None
        info = meta.get("info") or {}
        version = info.get("version")
        out: dict[str, Any] = {
            "name": info.get("name") or name,
            "version": version,
            "summary": info.get("summary"),
            "home_page": info.get("home_page"),
            "project_urls": info.get("project_urls") or {},
            "license": info.get("license_expression") or _first_line(info.get("license")),
            "classifiers": info.get("classifiers") or [],
            "ownership": meta.get("ownership") or {},
            "url": f"{BASE}/project/{normalise(name)}/",
        }
        out["provenance"] = await self.provenance(name, version) if version else None
        return out

    async def provenance(self, name: str, version: str) -> dict[str, Any] | None:
        """Attestation summary for the files of ``version``.

        ``None`` means the release has no attestations at all — i.e. it was not published
        through trusted publishing (or predates it).
        """
        simple = await self._get(f"/simple/{normalise(name)}/", accept=SIMPLE_ACCEPT)
        if not simple:
            return None
        prefix = f"{normalise(name).replace('-', '_')}-{version}"
        files = [
            f
            for f in simple.get("files", [])
            if f.get("provenance") and normalise(f.get("filename", "")).startswith(normalise(prefix))
        ]
        if not files:
            return None
        # One file is enough: every artefact of a release is published by the same job.
        payload = await self._get(f"/integrity/{normalise(name)}/{version}/{files[0]['filename']}/provenance")
        if not payload:
            return None
        bundles = payload.get("attestation_bundles") or []
        if not bundles:
            return None
        publisher = bundles[0].get("publisher") or {}
        attestations = bundles[0].get("attestations") or []
        san = None
        if attestations:
            certificate = (attestations[0].get("verification_material") or {}).get("certificate")
            san = certificate_san(certificate) if certificate else None
        return {
            "filename": files[0]["filename"],
            "publisher": publisher,
            "san": san,
            "san_parts": parse_san(san) if san else None,
            "files_with_provenance": len(files),
        }


def _first_line(text: str | None) -> str | None:
    """PyPI still carries whole license texts in ``info.license`` for older metadata."""
    if not text:
        return None
    line = text.strip().splitlines()[0].strip()
    return line[:80] or None
