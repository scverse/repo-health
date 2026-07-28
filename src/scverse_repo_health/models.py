"""Data model shared by the collection, check and rendering layers.

Everything here is JSON round-trippable: ``collect`` writes ``RepoData``/``CheckResult``
objects to ``results.json``, ``render`` reads them back, and the tests build ``RepoData``
straight from captured fixtures without touching the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any, Self


class Status(StrEnum):
    """Outcome of a single check for a single repo."""

    PASS = "pass"  # noqa: S105 - a check outcome, not a credential
    FAIL = "fail"
    WARN = "warn"
    NA = "na"
    UNKNOWN = "unknown"

    @property
    def glyph(self) -> str:
        return _GLYPHS[self]

    @property
    def label(self) -> str:
        return _LABELS[self]


_GLYPHS = {
    Status.PASS: "✓",
    Status.FAIL: "✗",
    Status.WARN: "!",
    Status.NA: "–",
    Status.UNKNOWN: "?",
}
_LABELS = {
    Status.PASS: "pass",
    Status.FAIL: "fail",
    Status.WARN: "warning",
    Status.NA: "not applicable",
    Status.UNKNOWN: "unknown",
}


class Tier(StrEnum):
    """How much a check matters. Only ``REQUIRED`` counts towards a group's score."""

    REQUIRED = "required"
    RECOMMENDED = "recommended"
    INFORMATIONAL = "informational"

    @property
    def short(self) -> str:
        return {Tier.REQUIRED: "R", Tier.RECOMMENDED: "r", Tier.INFORMATIONAL: "i"}[self]


class Category(StrEnum):
    """Repo grouping, derived from scverse.org's ``packages.json``."""

    CORE_DATASTRUCTURE = "core-datastructure"
    CORE_FRAMEWORK = "core-framework"
    CORE_INFRASTRUCTURE = "core-infrastructure"
    OTHER = "other"

    @property
    def label(self) -> str:
        return {
            Category.CORE_DATASTRUCTURE: "Core data structures",
            Category.CORE_FRAMEWORK: "Core framework",
            Category.CORE_INFRASTRUCTURE: "Core infrastructure",
            Category.OTHER: "Other repositories",
        }[self]


#: Order the four groups appear in on the dashboard.
CATEGORY_ORDER = [
    Category.CORE_DATASTRUCTURE,
    Category.CORE_FRAMEWORK,
    Category.CORE_INFRASTRUCTURE,
    Category.OTHER,
]


@dataclass(slots=True)
class CheckResult:
    """The value of one cell in the matrix."""

    status: Status
    detail: str = ""
    fix_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"status": str(self.status)}
        if self.detail:
            d["detail"] = self.detail
        if self.fix_url:
            d["fix_url"] = self.fix_url
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        return cls(status=Status(d["status"]), detail=d.get("detail", ""), fix_url=d.get("fix_url"))


def passed(detail: str = "", fix_url: str | None = None) -> CheckResult:
    return CheckResult(Status.PASS, detail, fix_url)


def failed(detail: str = "", fix_url: str | None = None) -> CheckResult:
    return CheckResult(Status.FAIL, detail, fix_url)


def warned(detail: str = "", fix_url: str | None = None) -> CheckResult:
    return CheckResult(Status.WARN, detail, fix_url)


def not_applicable(detail: str = "", fix_url: str | None = None) -> CheckResult:
    return CheckResult(Status.NA, detail, fix_url)


def unknown(detail: str = "", fix_url: str | None = None) -> CheckResult:
    return CheckResult(Status.UNKNOWN, detail, fix_url)


def verdict(ok: bool, detail_ok: str, detail_bad: str, *, fix_url: str | None = None) -> CheckResult:  # noqa: FBT001
    """Small helper for the many boolean checks."""
    return passed(detail_ok, fix_url) if ok else failed(detail_bad, fix_url)


#: Raw API payloads the checks have already read; dropped from ``results.json``.
_BULK_FIELDS = frozenset(
    {
        "check_runs",
        "classic_protection",
        "community_profile",
        "dependabot_alerts",
        "environments",
        "files",
        "releases",
        "rulesets",
        "tree",
        "workflows",
        "core_devs",
        "actions_permissions",
        "immutable_releases",
        "private_vulnerability_reporting",
    }
)
#: The parts of the GitHub repo object the dashboard actually shows.
_REPO_KEYS = frozenset(
    {
        "archived",
        "default_branch",
        "description",
        "full_name",
        "homepage",
        "html_url",
        "license",
        "name",
        "pushed_at",
        "topics",
    }
)


@dataclass(slots=True)
class RepoData:
    """Everything fetched for one repository.

    Checks are pure functions of this object — they must never perform I/O. Any endpoint
    that could not be read (typically a 403 because the GitHub App is not installed, or
    the local token lacks ``administration:read``) is recorded in :attr:`unavailable`
    keyed by attribute name, and the corresponding checks report ``UNKNOWN`` rather than
    pretending the feature is off.
    """

    name: str
    category: Category = Category.OTHER
    #: Raw repo object from ``GET /orgs/{org}/repos``.
    repo: dict[str, Any] = field(default_factory=dict)
    #: All paths in the default branch, from one recursive tree call.
    tree: list[str] = field(default_factory=list)
    #: Decoded text of the handful of blobs we actually care about, keyed by path.
    files: dict[str, str] = field(default_factory=dict)
    #: Subset of :attr:`files` under ``.github/workflows/``.
    workflows: dict[str, str] = field(default_factory=dict)
    rulesets: list[dict[str, Any]] | None = None
    classic_protection: dict[str, Any] | None = None
    releases: list[dict[str, Any]] = field(default_factory=list)
    community_profile: dict[str, Any] | None = None
    environments: list[dict[str, Any]] | None = None
    actions_permissions: dict[str, Any] | None = None
    immutable_releases: dict[str, Any] | None = None
    private_vulnerability_reporting: dict[str, Any] | None = None
    dependabot_alerts: list[dict[str, Any]] | None = None
    #: Names of check runs seen on the tip of the default branch.
    check_runs: list[str] = field(default_factory=list)
    #: PyPI data, see :mod:`scverse_repo_health.sources.pypi`.
    pypi: dict[str, Any] | None = None
    #: Read the Docs data, see :mod:`scverse_repo_health.sources.readthedocs`.
    rtd: dict[str, Any] | None = None
    #: This repo's entry in scverse.org's ``packages.json``, if listed.
    package_entry: dict[str, Any] | None = None
    #: Latest ``cookiecutter-scverse`` release, shared across repos.
    template: dict[str, Any] | None = None
    #: How far this repo's ``.cruft.json`` commit is behind the template.
    template_status: dict[str, Any] | None = None
    #: GitHub logins of the scverse core team, from ``config/core_devs.yaml``.
    core_devs: list[str] = field(default_factory=list)
    #: Check ids waived for this repo, id -> reason.
    waivers: dict[str, str] = field(default_factory=dict)
    #: Attribute name -> why it could not be fetched.
    unavailable: dict[str, str] = field(default_factory=dict)
    #: Non-fatal problems hit while collecting.
    errors: list[str] = field(default_factory=list)

    # -- convenience accessors over the raw repo object -------------------------------

    @property
    def full_name(self) -> str:
        return self.repo.get("full_name") or f"scverse/{self.name}"

    @property
    def owner(self) -> str:
        return self.full_name.split("/")[0]

    @property
    def html_url(self) -> str:
        return self.repo.get("html_url") or f"https://github.com/{self.full_name}"

    @property
    def default_branch(self) -> str:
        return self.repo.get("default_branch") or "main"

    @property
    def archived(self) -> bool:
        return bool(self.repo.get("archived"))

    @property
    def topics(self) -> list[str]:
        return list(self.repo.get("topics") or [])

    @property
    def description(self) -> str:
        return self.repo.get("description") or ""

    @property
    def homepage(self) -> str:
        return self.repo.get("homepage") or ""

    def is_unavailable(self, *attrs: str) -> str | None:
        """Return the reason the first of ``attrs`` is unreadable, or ``None``."""
        for attr in attrs:
            if (reason := self.unavailable.get(attr)) is not None:
                return reason
        return None

    def file(self, path: str) -> str | None:
        """Text of a fetched blob, or ``None`` if absent or not fetched."""
        return self.files.get(path)

    def has_path(self, path: str) -> bool:
        return path in self.tree

    def find(self, *candidates: str) -> str | None:
        """First of ``candidates`` that exists in the tree."""
        return next((c for c in candidates if c in self.tree), None)

    def blob_url(self, path: str) -> str:
        return f"{self.html_url}/blob/{self.default_branch}/{path}"

    def new_file_url(self, path: str) -> str:
        return f"{self.html_url}/new/{self.default_branch}?filename={path}"

    def settings_url(self, page: str = "") -> str:
        return f"{self.html_url}/settings/{page}".rstrip("/")

    # -- (de)serialisation ------------------------------------------------------------

    def to_dict(self, *, slim: bool = False) -> dict[str, Any]:
        """Serialise. ``slim`` drops the raw payloads the checks have already consumed.

        A full dump is ~90 KiB per repo, most of it file contents and untouched GitHub
        objects — about 5 MB across the org, downloaded by everyone who opens the
        dashboard. ``slim=True`` keeps only what the renderer and a downstream consumer
        need. Test fixtures use the full form; ``results.json`` uses the slim one.
        """
        from dataclasses import fields  # noqa: PLC0415 - only needed for serialisation

        out: dict[str, Any] = {}
        for f in fields(self):
            if slim and f.name in _BULK_FIELDS:
                continue
            value = getattr(self, f.name)
            out[f.name] = str(value) if isinstance(value, StrEnum) else value
        if slim:
            out["repo"] = {k: v for k, v in self.repo.items() if k in _REPO_KEYS}
            out["template"] = None  # identical for every repo; kept once in Results
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        from dataclasses import fields  # noqa: PLC0415 - only needed for serialisation

        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in d.items() if k in known}
        if "category" in kwargs:
            kwargs["category"] = Category(kwargs["category"])
        return cls(**kwargs)


@dataclass(slots=True)
class RepoReport:
    """One repo's row in the matrix: its data plus every check's result."""

    repo: RepoData
    results: dict[str, CheckResult]

    def score(self, tier: Tier = Tier.REQUIRED) -> tuple[int, int]:
        """``(passing, applicable)`` over checks of ``tier``, ignoring NA/UNKNOWN."""
        from .registry import REGISTRY  # noqa: PLC0415 - registry imports this module

        passing = applicable = 0
        for check_id, result in self.results.items():
            check = REGISTRY.get(check_id)
            if check is None or check.tier is not tier:
                continue
            if result.status in (Status.NA, Status.UNKNOWN):
                continue
            applicable += 1
            if result.status is Status.PASS:
                passing += 1
        return passing, applicable

    def to_dict(self, *, slim: bool = False) -> dict[str, Any]:
        return {
            "repo": self.repo.to_dict(slim=slim),
            "results": {k: v.to_dict() for k, v in self.results.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        return cls(
            repo=RepoData.from_dict(d["repo"]),
            results={k: CheckResult.from_dict(v) for k, v in d["results"].items()},
        )
