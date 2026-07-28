"""Loading of the two hand-maintained YAML files under ``config/``."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from ._log import log

if TYPE_CHECKING:
    from typing import Any

_PKG_DIR = Path(__file__).parent


def config_dir() -> Path:
    """Locate ``config/``.

    Checked in order: ``$REPO_HEALTH_CONFIG``, ``./config`` (the normal case — the tool
    runs from its own checkout), the source tree two levels up, and finally the copy
    force-included in the wheel.
    """
    candidates = [
        Path(env) if (env := os.environ.get("REPO_HEALTH_CONFIG")) else None,
        Path.cwd() / "config",
        _PKG_DIR.parent.parent / "config",
        _PKG_DIR / "_config_data",
    ]
    for path in candidates:
        if path is not None and (path / "repos.yaml").is_file():
            return path
    msg = "could not locate config/repos.yaml; set $REPO_HEALTH_CONFIG"
    raise FileNotFoundError(msg)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        log.warning(f"{path} not found, using defaults")
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@dataclass(slots=True)
class Exclusion:
    repo: str
    reason: str


@dataclass(slots=True)
class ReposConfig:
    """``config/repos.yaml``: which repos to skip, and per-repo waived checks."""

    org: str = "scverse"
    exclusions: dict[str, str] = field(default_factory=dict)
    #: Repos not in packages.json that are nonetheless scored, under "Other".
    include: set[str] = field(default_factory=set)
    #: repo -> {check id -> reason}
    waivers: dict[str, dict[str, str]] = field(default_factory=dict)
    #: repo -> PyPI distribution name, when it differs from the repo name.
    pypi_names: dict[str, str] = field(default_factory=dict)
    #: repo -> Read the Docs slug, when it differs from the repo name.
    rtd_slugs: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> ReposConfig:
        raw = _load_yaml(path or (config_dir() / "repos.yaml"))
        exclusions = {e["repo"]: e.get("reason", "") for e in raw.get("exclusions") or []}
        return cls(
            org=raw.get("org", "scverse"),
            exclusions=exclusions,
            include=set(raw.get("include") or []),
            waivers={k: dict(v or {}) for k, v in (raw.get("waivers") or {}).items()},
            pypi_names=dict(raw.get("pypi_names") or {}),
            rtd_slugs=dict(raw.get("rtd_slugs") or {}),
        )

    def is_excluded(self, repo: str) -> bool:
        return repo in self.exclusions

    def pypi_name(self, repo: str) -> str:
        return self.pypi_names.get(repo, repo)

    def rtd_slug(self, repo: str) -> str:
        return self.rtd_slugs.get(repo, repo.lower())


@dataclass(slots=True)
class CoreDevs:
    """``config/core_devs.yaml``: hand-maintained list of the scverse core team.

    Deliberately a committed file rather than something read from scverse.org at run
    time: the dashboard must not depend on the website's page structure.
    """

    #: GitHub login -> display name. The name is for humans reading the file; only the
    #: logins are used, and they are compared case-insensitively.
    devs: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None = None) -> CoreDevs:
        raw = _load_yaml(path or (config_dir() / "core_devs.yaml"))
        entries = raw.get("core_devs") or []
        return cls(devs={d["login"]: d.get("name", d["login"]) for d in entries if d.get("login")})

    @property
    def logins(self) -> list[str]:
        return sorted(self.devs)

    def is_core_dev(self, username: str) -> bool:
        return username.lower() in {d.lower() for d in self.devs}


@cache
def load_config() -> tuple[ReposConfig, CoreDevs]:
    return ReposConfig.load(), CoreDevs.load()
