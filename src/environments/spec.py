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
    file_digests: tuple[tuple[str, str], ...] = ()
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
            "file_digests": {name: digest for name, digest in self.file_digests},
        }
        if include_digest:
            value["digest"] = self.digest
        return value


class EnvironmentResolver:
    """Record an explicit caller-prepared host or image environment."""
    _MANIFESTS = {
        "BUILD", "BUILD.bazel", "CMakeLists.txt", "GNUmakefile", "Makefile",
        "MODULE.bazel", "WORKSPACE", "WORKSPACE.bazel", "conan.lock",
        "conanfile.py", "conanfile.txt", "configure", "configure.ac",
        "configure.in", "meson.build", "vcpkg-configuration.json", "vcpkg.json",
    }
    _VERSION_FILES = {
        ".tool-versions",
    }

    def resolve(
        self,
        root: Path,
        system: str,
        *,
        backend: str,
        image: str = "",
    ) -> EnvironmentSpec:
        backend = backend.strip().lower()
        image = image.strip()
        if backend not in {"host", "image"}:
            raise ValueError("environment mode supports only host or image; no fallback")
        if backend == "image" and not image:
            raise ValueError("environment_image is required for image mode")
        if backend == "host" and image:
            raise ValueError("environment_image can only be used with image mode")
        root = root.resolve()
        manifests = sorted(
            path.name for path in root.iterdir() if path.is_file() and path.name in self._MANIFESTS
        )
        lockfiles = sorted(
            name for name in manifests
            if name.endswith((".lock", ".lockfile", ".lock.json", ".lock.yaml"))
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
        base_image = image if backend == "image" else ""
        # Docker/devcontainer files are evidence only. They are never built;
        # keeping them in the digest makes a changed contract observable.
        for name in ("Dockerfile", "Containerfile", ".devcontainer/devcontainer.json"):
            if (root / name).is_file():
                runtime_hints += (f"project:{name}",)
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
            file_digests=tuple(file_digests),
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
        for name in ci_files:
            try:
                text = (root / name).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for key in ("cmake-version", "gcc-version", "clang-version"):
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
