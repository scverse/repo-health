from __future__ import annotations

from logging import basicConfig, getLogger

from rich.logging import RichHandler

log = getLogger("scverse_repo_health")


def setup_logging(level: str = "INFO") -> None:
    basicConfig(level=level, handlers=[RichHandler(show_path=False)], format="%(message)s")
    # httpx logs every request at INFO, which drowns out our own progress messages.
    getLogger("httpx").setLevel("WARNING")
