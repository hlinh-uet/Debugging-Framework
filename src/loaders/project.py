from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Project:
    """A materialized project checkout supplied directly by the user."""

    path: Path
    project_id: str
    config_path: Path | None = None


class ProjectLoader:
    """Load the caller-selected directory without guessing its build system.

    The CLI is intentionally project-in: when no path is supplied it uses the
    current working directory.  Build/test detection belongs to
    ``BuildDetector``; requiring a fixed marker list here rejected perfectly
    valid custom projects before their explicit project contract could be read.
    """

    def load(
        self,
        project_path: Path | None = None,
        *,
        config_path: Path | None = None,
    ) -> Project:
        path = (project_path or Path.cwd()).expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"Project does not exist or is not a directory: {path}")
        resolved_config = config_path.expanduser().resolve() if config_path else None
        return Project(path=path, project_id=path.name, config_path=resolved_config)
