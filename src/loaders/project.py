from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PROJECT_MARKERS = (
    ".debugging-framework.json",
    ".git",
    "CMakeLists.txt",
    "meson.build",
    "Makefile",
    "makefile",
    "configure",
    "configure.ac",
    "Cargo.toml",
    "go.mod",
    "pyproject.toml",
    "pytest.ini",
    "setup.cfg",
    "setup.py",
    "package.json",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "gradlew",
    "Package.swift",
    "Gemfile",
    "composer.json",
    "WORKSPACE",
    "MODULE.bazel",
    "build.ninja",
    "GNUmakefile",
    "configure.in",
)


@dataclass(frozen=True)
class Project:
    """A materialized project checkout supplied directly by the user."""

    path: Path
    project_id: str


class ProjectLoader:
    """Validate a project path without consulting benchmark metadata."""

    def load(self, project_path: Path) -> Project:
        path = project_path.expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"Project không tồn tại hoặc không phải thư mục: {path}")
        fixed_marker = any((path / marker).exists() for marker in PROJECT_MARKERS)
        glob_marker = bool(list(path.glob("*.sln")) or list(path.glob("*.csproj")))
        if not fixed_marker and not glob_marker:
            markers = ", ".join(PROJECT_MARKERS)
            raise ValueError(
                "Không nhận diện được project root. Không tìm thấy build/VCS marker "
                f"tại {path}. Các marker được hỗ trợ: {markers}"
            )
        return Project(path=path, project_id=path.name)
