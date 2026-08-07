from __future__ import annotations

"""Deterministic environment facts used by baseline and patched validation.

The resolver is deliberately conservative.  It does not pretend that a project
manifest can install a compiler or a system library.  Instead it records the
evidence used to select an execution backend and produces a stable digest that
must be shared by baseline, APR and post-patch validation.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EnvironmentSpec:
    backend: str
    system: str
    base_image: str
    runtime_hints: tuple[str, ...]
    manifests: tuple[str, ...]
    lockfiles: tuple[str, ...]
    ci_files: tuple[str, ...]
    version_files: tuple[str, ...]
    system_packages: tuple[str, ...] = ()
    file_digests: tuple[tuple[str, str], ...] = ()
    project_dockerfile: str = ""
    digest: str = ""

    def __post_init__(self) -> None:
        if self.digest:
            return
        payload = self.as_dict(include_digest=False)
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        object.__setattr__(self, "digest", f"sha256:{digest}")

    def as_dict(self, *, include_digest: bool = True) -> dict:
        value = {
            "backend": self.backend,
            "system": self.system,
            "base_image": self.base_image,
            "runtime_hints": list(self.runtime_hints),
            "manifests": list(self.manifests),
            "lockfiles": list(self.lockfiles),
            "ci_files": list(self.ci_files),
            "version_files": list(self.version_files),
            "system_packages": list(self.system_packages),
            "file_digests": {name: digest for name, digest in self.file_digests},
            "project_dockerfile": self.project_dockerfile,
        }
        if include_digest:
            value["digest"] = self.digest
        return value


class EnvironmentResolver:
    """Resolve a project-native environment without executing project code."""

    _BASE_IMAGES = {
        "python": "python:3.12-slim-bookworm",
        "node": "node:22-bookworm-slim",
        "maven": "maven:3.9-eclipse-temurin-21",
        "gradle": "gradle:8-jdk21",
        "cargo": "rust:1.82-slim-bookworm",
        "go": "golang:1.23-bookworm",
        "dotnet": "mcr.microsoft.com/dotnet/sdk:8.0",
        "ruby": "ruby:3.3-bookworm",
        "swift": "swift:5.10-jammy",
        "composer": "composer:2",
        # These images are intentionally only a base.  The OCI backend adds the
        # compiler/build tools in its generated image layer.
        "cmake": "ubuntu:24.04",
        "make": "ubuntu:24.04",
        "autotools": "ubuntu:24.04",
        "meson": "ubuntu:24.04",
        "ninja": "ubuntu:24.04",
        "bazel": "ubuntu:24.04",
        "defects4c-rendered-recipe": "ubuntu:24.04",
        "project-config": "ubuntu:24.04",
    }
    _MANIFESTS = {
        "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
        "requirements-dev.txt", "requirements-test.txt", "package.json",
        "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
        "Package.swift", "Gemfile", "composer.json", "CMakeLists.txt",
        "meson.build", "Makefile", "GNUmakefile", "configure", "configure.ac",
        "WORKSPACE", "MODULE.bazel", "package-lock.json", "pnpm-lock.yaml",
        "yarn.lock", "Cargo.lock", "go.sum", "poetry.lock", "uv.lock",
        "Pipfile.lock", "Gemfile.lock", "composer.lock", "gradle.lockfile",
    }
    _VERSION_FILES = {
        ".python-version", ".nvmrc", ".node-version", "rust-toolchain",
        "rust-toolchain.toml", ".tool-versions", "global.json", ".java-version",
    }

    def resolve(
        self,
        root: Path,
        system: str,
        *,
        backend: str = "current",
        image: str = "",
    ) -> EnvironmentSpec:
        root = root.resolve()
        manifests = sorted(
            path.name for path in root.iterdir() if path.is_file() and path.name in self._MANIFESTS
        )
        lockfiles = sorted(
            name for name in manifests
            if name.endswith((".lock", ".lockfile", ".lock.json", ".lock.yaml"))
            or name in {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "Cargo.lock", "go.sum", "uv.lock", "poetry.lock"}
        )
        ci_files = []
        for candidate in (
            ".github/workflows", ".gitlab-ci.yml", ".circleci/config.yml",
            "azure-pipelines.yml", "Jenkinsfile", "buildkite.yml",
        ):
            path = root / candidate
            if path.is_file():
                ci_files.append(candidate)
            elif path.is_dir():
                ci_files.extend(
                    item.relative_to(root).as_posix()
                    for item in path.rglob("*") if item.is_file()
                )
        version_files = sorted(
            name for name in self._VERSION_FILES if (root / name).is_file()
        )
        runtime_hints = self._runtime_hints(root, version_files, ci_files)
        base_image = (
            "running-container"
            if backend == "container"
            else image.strip()
            if backend == "image"
            else self._base_image(system, root, runtime_hints)
        )
        # A project-provided devcontainer/Dockerfile is evidence for discovery;
        # it is not copied into the generated image automatically yet.  Keeping
        # it in the digest makes a changed environment contract observable.
        for name in ("Dockerfile", "Containerfile", ".devcontainer/devcontainer.json"):
            if (root / name).is_file():
                runtime_hints += (f"project:{name}",)
        system_packages = self._system_packages(system)
        project_dockerfile = next(
            (
                name for name in ("Dockerfile", "Containerfile")
                if (root / name).is_file()
            ),
            "",
        )
        evidence_files = set(manifests) | set(ci_files) | set(version_files)
        evidence_files.update(
            name for name in ("Dockerfile", "Containerfile", ".devcontainer/devcontainer.json")
            if (root / name).is_file()
        )
        file_digests = []
        for name in sorted(evidence_files):
            path = root / name
            if not path.is_file():
                continue
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
            file_digests.append((name, digest))
        return EnvironmentSpec(
            backend=backend,
            system=system,
            base_image=base_image,
            runtime_hints=tuple(sorted(set(runtime_hints))),
            manifests=tuple(manifests),
            lockfiles=tuple(lockfiles),
            ci_files=tuple(sorted(ci_files)),
            version_files=tuple(version_files),
            system_packages=tuple(system_packages),
            file_digests=tuple(file_digests),
            project_dockerfile=project_dockerfile,
        )

    @staticmethod
    def _runtime_hints(
        root: Path,
        version_files: list[str],
        ci_files: list[str],
    ) -> tuple[str, ...]:
        hints: list[str] = []
        for name in version_files:
            try:
                value = (root / name).read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if value:
                hints.append(f"{name}={value[:120]}")
        pyproject = root / "pyproject.toml"
        if pyproject.is_file():
            text = pyproject.read_text(encoding="utf-8", errors="replace")
            match = re.search(r"requires-python\s*=\s*[\"']([^\"']+)", text)
            if match:
                hints.append(f"requires-python={match.group(1)}")
        package = root / "package.json"
        if package.is_file():
            text = package.read_text(encoding="utf-8", errors="replace")
            match = re.search(r'"node"\s*:\s*"([^\"]+)', text)
            if match:
                hints.append(f"engines.node={match.group(1)}")
        for name in ci_files:
            try:
                text = (root / name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for key in ("python-version", "node-version", "java-version", "go-version"):
                match = re.search(rf"{re.escape(key)}\s*:\s*[\"']?([^\s\"']+)", text)
                if match:
                    hints.append(f"ci:{key}={match.group(1)}")
        for name in ("Dockerfile", "Containerfile"):
            path = root / name
            if path.is_file():
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = ""
                match = re.search(r"(?m)^\s*FROM\s+([^\s]+)", text)
                if match:
                    hints.append(f"docker:FROM={match.group(1)}")
        return tuple(hints)

    @classmethod
    def _base_image(cls, system: str, root: Path, hints: tuple[str, ...]) -> str:
        base = cls._BASE_IMAGES.get(system, "")
        if system == "python":
            version = next(
                (value.split("=", 1)[1] for value in hints if value.startswith(".python-version=")),
                "",
            )
            if re.fullmatch(r"\d+(?:\.\d+){1,2}", version):
                return f"python:{'.'.join(version.split('.')[:2])}-slim-bookworm"
        if system == "node":
            version = next(
                (value.split("=", 1)[1].lstrip("v") for value in hints if value.startswith(".nvmrc=")),
                "",
            )
            if re.fullmatch(r"\d+(?:\.\d+){0,2}", version):
                return f"node:{'.'.join(version.split('.')[:2])}-bookworm-slim"
        if system == "go":
            try:
                text = (root / "go.mod").read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            match = re.search(r"(?m)^\s*go\s+(\d+(?:\.\d+){1,2})", text)
            if match:
                return f"golang:{match.group(1)}-bookworm"
        return base

    @staticmethod
    def _system_packages(system: str) -> tuple[str, ...]:
        return {
            "python": ("build-essential", "git"),
            "node": ("git",),
            "maven": ("git",),
            "gradle": ("git",),
            "cargo": ("git",),
            "go": ("git",),
            "cmake": ("build-essential", "cmake", "ninja-build", "pkg-config"),
            "make": ("build-essential",),
            "autotools": ("build-essential", "autoconf", "automake", "libtool", "pkg-config"),
            "meson": ("build-essential", "meson", "ninja-build", "pkg-config"),
            "ninja": ("build-essential", "ninja-build"),
            "bazel": ("build-essential",),
            "defects4c-rendered-recipe": ("build-essential", "cmake", "pkg-config", "python3"),
            "project-config": (),
        }.get(system, ())
