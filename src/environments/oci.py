from __future__ import annotations

"""Small OCI backend used when the operator wants toolchain provisioning.

The local backend remains useful for development and for projects whose native
environment is supplied by the host.  OCI is deliberately opt-in/auto-detected
by the CLI, so missing Docker/Podman produces a clear environment error instead
of silently executing arbitrary project commands on the host.
"""

import shutil
import subprocess
import sys
import os
from dataclasses import dataclass
from pathlib import Path

from src.environments.spec import EnvironmentSpec


@dataclass(frozen=True)
class OCIProvision:
    runtime: str
    image: str
    digest: str
    command: list[str]
    output: str
    image_digest: str = ""


class OCIEnvironment:
    def __init__(self, *, runtime: str = "auto", timeout_seconds: int = 1800):
        if runtime == "auto":
            runtime = shutil.which("docker") or shutil.which("podman") or ""
        self.runtime = runtime
        self.timeout_seconds = timeout_seconds

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
        project_root: Path | None = None,
    ) -> OCIProvision:
        if not self.available:
            raise RuntimeError("environment_runtime_unavailable:docker_or_podman")
        if not spec.base_image:
            raise RuntimeError(f"environment_base_image_unknown:{spec.system}")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        if spec.backend == "image":
            return self._use_prebuilt_image(spec, artifact_dir)
        tag = "debugging-framework/" + spec.digest.split(":", 1)[-1][:24]
        existing = subprocess.run(
            [self.runtime, "image", "inspect", "--format", "{{.Id}}", tag],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        existing_digest = (existing.stdout or "").strip()
        if existing.returncode == 0 and existing_digest:
            (artifact_dir / "provision.log").write_text(
                f"Reused existing OCI image {tag} ({existing_digest})\n",
                encoding="utf-8",
            )
            return OCIProvision(
                self.runtime,
                tag,
                spec.digest,
                [self.runtime, "image", "inspect", "--format", "{{.Id}}", tag],
                "reused cached image",
                existing_digest,
            )
        if spec.project_dockerfile and project_root:
            dockerfile = project_root / spec.project_dockerfile
            context = project_root
        else:
            dockerfile = artifact_dir / "Environment.Dockerfile"
            context = artifact_dir
            packages = " ".join(spec.system_packages)
            install = ""
            if packages:
                install = (
                    "RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y "
                    + packages
                    + " && rm -rf /var/lib/apt/lists/*\n"
                )
            dockerfile.write_text(
                f"FROM {spec.base_image}\n"
                "USER root\n"
                + install
                + "WORKDIR /workspace\n",
                encoding="utf-8",
            )
        command = [self.runtime, "build", "--pull", "-t", tag, "-f", str(dockerfile), str(context)]
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            check=False,
        )
        output = completed.stdout or ""
        (artifact_dir / "provision.log").write_text(output, encoding="utf-8", errors="replace")
        if completed.returncode != 0:
            raise RuntimeError(f"environment_provision_failed:{completed.returncode}")
        inspect = subprocess.run(
            [self.runtime, "image", "inspect", "--format", "{{.Id}}", tag],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        image_digest = (inspect.stdout or "").strip()
        if inspect.returncode != 0 or not image_digest:
            raise RuntimeError("environment_image_digest_unavailable")
        return OCIProvision(self.runtime, tag, spec.digest, command, output, image_digest)

    def _use_prebuilt_image(
        self,
        spec: EnvironmentSpec,
        artifact_dir: Path,
    ) -> OCIProvision:
        """Resolve a caller-prepared image without building or pulling anything."""
        image = spec.base_image.strip()
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
        image_digest = (inspected.stdout or "").strip()
        if inspected.returncode != 0 or not image_digest:
            detail = image_digest.splitlines()
            raise RuntimeError(
                "environment_prebuilt_image_unavailable:"
                + (detail[-1] if detail else image)
            )
        output = f"Using caller-prepared OCI image {image} ({image_digest})\n"
        (artifact_dir / "provision.log").write_text(output, encoding="utf-8")
        return OCIProvision(
            self.runtime,
            image,
            spec.digest,
            command,
            output,
            image_digest,
        )

    def command(
        self,
        provision: OCIProvision,
        root: Path,
        argv: tuple[str, ...],
        cwd: Path,
        *,
        network: bool = False,
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
            provision.runtime, "run", "--rm", "--network=" + ("default" if network else "none"),
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{root.resolve()}:/workspace:rw",
            "-w", workdir, provision.image, *container_argv,
        ]
