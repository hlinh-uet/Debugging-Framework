from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Project:
    """A materialized project checkout supplied directly by the user."""

    path: Path
    project_id: str


class ProjectLoader:
    """Load the caller-selected directory without guessing its build system.

    The CLI is intentionally project-in: when no path is supplied it uses the
    current working directory.  Build/test detection belongs to
    ``BuildDetector``; requiring a fixed marker list here rejected perfectly
    valid custom projects before their explicit project contract could be read.
    """

    def load(self, project_path: Path | None = None) -> Project:
        path = (project_path or Path.cwd()).expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"Project không tồn tại hoặc không phải thư mục: {path}")
        return Project(path=path, project_id=path.name)
