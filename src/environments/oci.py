from __future__ import annotations

"""Run validation in a caller-prepared OCI image.

This backend never builds or pulls an image and never installs dependencies.
If the configured runtime or image is unavailable, validation fails closed.
"""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from src.environments.spec import EnvironmentSpec


@dataclass(frozen=True)
class OCIProvision:
    runtime: str
    image: str
    image_digest: str = ""


class OCIEnvironment:
    def __init__(self, *, runtime: str = "auto"):
        if runtime == "auto":
            runtime = shutil.which("docker") or shutil.which("podman") or ""
        self.runtime = runtime

    @property
    def available(self) -> bool:
        if not (self.runtime and shutil.which(self.runtime)):
            return False
        try:
            probe = subprocess.run(
                [self.runtime, "info"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return probe.returncode == 0

    def provision(
        self,
        spec: EnvironmentSpec,
        artifact_dir: Path,
    ) -> OCIProvision:
        if spec.backend != "image":
            raise RuntimeError(f"environment_mode_unsupported:{spec.backend}")
        if not spec.base_image:
            raise RuntimeError("environment_image_required")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        return self._use_prebuilt_image(spec, artifact_dir)

    def inspect_image(self, image: str) -> str:
        """Return the local image ID; never pull/build as a side effect."""
        if not self.available:
            raise RuntimeError("environment_runtime_unavailable:docker_or_podman")
        image = image.strip()
        if not image:
            raise RuntimeError("environment_image_required")
        command = [self.runtime, "image", "inspect", "--format", "{{.Id}}", image]
        inspected = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        output = (inspected.stdout or "").strip()
        if inspected.returncode != 0 or not output:
            detail = output.splitlines()
            raise RuntimeError(
                "environment_prebuilt_image_unavailable:"
                + (detail[-1] if detail else image)
            )
        return output

    def _use_prebuilt_image(
        self,
        spec: EnvironmentSpec,
        artifact_dir: Path,
    ) -> OCIProvision:
        """Resolve a caller-prepared image without building or pulling anything."""
        image = spec.base_image.strip()
        image_digest = self.inspect_image(image)
        output = f"Using caller-prepared OCI image {image} ({image_digest})\n"
        (artifact_dir / "provision.log").write_text(output, encoding="utf-8")
        return OCIProvision(
            runtime=self.runtime,
            image=image,
            image_digest=image_digest,
        )

    def command(
        self,
        provision: OCIProvision,
        root: Path,
        argv: tuple[str, ...],
        cwd: Path,
    ) -> list[str]:
        relative_cwd = cwd.resolve().relative_to(root.resolve()).as_posix()
        workdir = "/workspace" if relative_cwd == "." else "/workspace/" + relative_cwd
        container_argv = list(argv)
        # BuildDetector uses the framework interpreter for local venv creation.
        # Inside a language image that host path does not exist; map it to the
        # image's standard Python executable while leaving project-local venvs
        # and user commands untouched.
        if container_argv and Path(container_argv[0]).resolve() == Path(sys.executable).resolve():
            container_argv[0] = "python3"
        return [
            provision.runtime, "run", "--rm", "--network=none",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{root.resolve()}:/workspace:rw",
            "-w", workdir, provision.image, *container_argv,
        ]
