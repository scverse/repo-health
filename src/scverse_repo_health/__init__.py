"""Health dashboard for the repositories of the scverse GitHub organisation."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("scverse-repo-health")
except PackageNotFoundError:  # pragma: no cover - not installed
    __version__ = "0.0.dev0"

__all__ = ["__version__"]
