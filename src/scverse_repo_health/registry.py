"""The check registry.

A check is a *pure function* of an already-fetched :class:`~.models.RepoData`. It never
does I/O, which is what makes every one of them unit-testable from a JSON fixture.

    @check(id="security/actions-pinned", tier=Tier.REQUIRED, category="Supply chain",
           title="Actions pinned to SHA", applies_to=lambda r: bool(r.workflows))
    def actions_pinned(r: RepoData) -> CheckResult: ...
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ._log import log
from .models import CheckResult, Status, not_applicable, unknown

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from typing import Any

    from .models import RepoData, Tier

#: Short tokens used in ``Check.needs``, and what they mean.
NEEDS = {
    "meta": "GitHub metadata:read (or public)",
    "cont": "GitHub contents:read",
    "admin": "GitHub administration:read",
    "admin?": "GitHub administration:read, only as a fallback",
    "sec": "GitHub Dependabot alerts:read",
    "actions": "GitHub actions:read",
    "pypi": "pypi.org",
    "rtd": "readthedocs.org",
    "py": "endoflife.date Python release dates",
    "web": "scverse.org packages.json",
}

#: Column groups, in the order they appear on the dashboard.
CHECK_CATEGORIES = [
    "Template",
    "Documentation",
    "Packaging",
    "Supply chain",
    "Branch protection",
    "Governance",
]


@dataclass(frozen=True, slots=True)
class Check:
    id: str
    tier: Tier
    category: str
    title: str
    fn: Callable[[RepoData], CheckResult]
    description: str = ""
    #: Human-readable permissions/data sources this check needs, for ``list-checks``.
    needs: tuple[str, ...] = ()
    #: Return ``False`` to short-circuit the check to ``NA``.
    applies_to: Callable[[RepoData], bool] | None = None
    #: Registration index, used to keep column order stable.
    order: int = 0

    @property
    def short_title(self) -> str:
        """Title trimmed for the rotated matrix header."""
        return self.title

    def run(self, repo: RepoData) -> CheckResult:
        """Evaluate against ``repo``, turning waivers, N/A and crashes into cells."""
        if (reason := repo.waivers.get(self.id)) is not None:
            return CheckResult(Status.NA, f"Waived: {reason}")
        try:
            if self.applies_to is not None and not self.applies_to(repo):
                return not_applicable("Does not apply to this repository")
            return self.fn(repo)
        except Exception as exc:
            log.warning(f"check {self.id} raised on {repo.name}: {exc!r}")
            repo.errors.append(f"{self.id}: {exc!r}")
            return unknown(f"Check raised {type(exc).__name__}: {exc}")


class Registry:
    """Ordered mapping of check id -> :class:`Check`."""

    def __init__(self) -> None:
        self._checks: dict[str, Check] = {}
        self._loaded = False

    def add(self, check: Check) -> None:
        if check.id in self._checks:
            msg = f"duplicate check id {check.id!r}"
            raise ValueError(msg)
        if check.category not in CHECK_CATEGORIES:
            msg = f"unknown category {check.category!r} for {check.id!r}"
            raise ValueError(msg)
        if unknown_needs := set(check.needs) - set(NEEDS):
            msg = f"unknown needs {sorted(unknown_needs)} for {check.id!r}"
            raise ValueError(msg)
        self._checks[check.id] = check

    def get(self, check_id: str) -> Check | None:
        self.load()
        return self._checks.get(check_id)

    def load(self) -> None:
        """Import every module in :mod:`scverse_repo_health.checks` exactly once."""
        if self._loaded:
            return
        self._loaded = True  # set first: check modules import this module back
        from . import checks  # noqa: PLC0415 - the check modules import this one back

        for mod in pkgutil.iter_modules(checks.__path__):
            importlib.import_module(f"{checks.__name__}.{mod.name}")

    def all(self) -> list[Check]:
        """Every check, ordered by category then registration order."""
        self.load()
        return sorted(
            self._checks.values(),
            key=lambda c: (CHECK_CATEGORIES.index(c.category), c.order),
        )

    def by_category(self) -> dict[str, list[Check]]:
        grouped: dict[str, list[Check]] = {}
        for c in self.all():
            grouped.setdefault(c.category, []).append(c)
        return grouped

    def __iter__(self) -> Iterator[Check]:
        return iter(self.all())

    def __len__(self) -> int:
        return len(self.all())


REGISTRY = Registry()
_counter = iter(range(1, 10_000))


def check(
    *,
    id: str,  # noqa: A002 - reads best as `id=` at every call site
    tier: Tier,
    category: str,
    title: str,
    description: str = "",
    needs: tuple[str, ...] = (),
    applies_to: Callable[[RepoData], bool] | None = None,
) -> Callable[[Callable[[RepoData], CheckResult]], Callable[[RepoData], CheckResult]]:
    """Register ``fn`` as a check and return it unchanged, so it stays directly callable."""

    def decorate(fn: Callable[[RepoData], CheckResult]) -> Callable[[RepoData], CheckResult]:
        REGISTRY.add(
            Check(
                id=id,
                tier=tier,
                category=category,
                title=title,
                fn=fn,
                description=description or (fn.__doc__ or "").strip().split("\n")[0],
                needs=needs,
                applies_to=applies_to,
                order=next(_counter),
            )
        )
        return fn

    return decorate


def run_all(repo: RepoData, only: list[str] | None = None) -> dict[str, CheckResult]:
    """Run every registered check (or just ``only``) against ``repo``."""
    return {c.id: c.run(repo) for c in REGISTRY.all() if only is None or c.id in only}


@dataclass(slots=True)
class Summary:
    """Aggregate pass/fail counts, used for the dashboard's stat tiles."""

    counts: dict[Status, int] = field(default_factory=lambda: dict.fromkeys(Status, 0))

    def add(self, result: CheckResult) -> None:
        self.counts[result.status] += 1

    @property
    def scored(self) -> int:
        return self.counts[Status.PASS] + self.counts[Status.FAIL] + self.counts[Status.WARN]

    @property
    def percent(self) -> int:
        return round(100 * self.counts[Status.PASS] / self.scored) if self.scored else 0

    def to_dict(self) -> dict[str, Any]:
        return {str(k): v for k, v in self.counts.items()} | {"percent": self.percent}
