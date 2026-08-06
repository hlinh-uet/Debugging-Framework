from __future__ import annotations

"""Execute validation in an already-running Defects4C-style Docker container.

The legacy Defects4C workflow provisions a broad image once and keeps a named
container alive.  This backend deliberately reuses that container instead of
installing native packages on the host or building a second generic image.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from src.environments.spec import EnvironmentSpec
from src.utils.jsonio import safe_name


@dataclass(frozen=True)
class DockerProvision:
    runtime: str
    container: str
    container_root: str
    environment_digest: str
    command: list[str]
    output: str
    image_digest: str = ""


class RunningDockerEnvironment:
    """Use a running container selected by DEFECTS4C_CONTAINER or convention."""

    def __init__(
        self,
        *,
        runtime: str = "auto",
        container: str = "",
        timeout_seconds: int = 1800,
    ):
        if runtime == "auto":
            runtime = shutil.which("docker") or shutil.which("podman") or ""
        self.runtime = runtime
        self.container = container.strip()
        self.timeout_seconds = timeout_seconds

    @property
    def runtime_available(self) -> bool:
        if not (self.runtime and shutil.which(self.runtime)):
            return False
        try:
            probe = subprocess.run(
                [self.runtime, "info"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return probe.returncode == 0

    def resolve_container(self, project_id: str = "") -> str:
        if not self.runtime_available:
            return ""
        candidates: list[str] = []
        if self.container:
            candidates.append(self.container)
        candidates.extend(self._conventional_names(project_id))
        for name in dict.fromkeys(candidates):
            if self.is_running(name):
                return name
        return ""

    def is_running(self, name: str) -> bool:
        if not (self.runtime and name):
            return False
        try:
            result = subprocess.run(
                [self.runtime, "inspect", "-f", "{{.State.Running}}", name],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and result.stdout.strip().lower() == "true"

    @staticmethod
    def _conventional_names(project_id: str) -> list[str]:
        value = str(project_id or "").strip().lower()
        names: list[str] = []
        # Defects4C uses both `my_defects4c_<project-tail>` (libyang) and
        # `my_defects4c_<dataset-folder>` (some projects retain `___`), so try
        # the complete dataset portion before the short project tail.
        dataset = re.split(r"(?<!_)__(?!_)", value, maxsplit=1)[0]
        dataset = safe_name(dataset, 100).strip("._-") if dataset else ""
        if "___" in dataset:
            tail = safe_name(dataset.split("___", 1)[1], 80).strip("._-")
            if tail:
                names.append("my_defects4c_" + tail)
        if dataset:
            names.append("my_defects4c_" + dataset)
        names.append("my_defects4c")
        return names

    def provision(
        self,
        spec: EnvironmentSpec,
        artifact_dir: Path,
        project_root: Path | None = None,
        *,
        project_id: str = "",
    ) -> DockerProvision:
        if not self.runtime_available:
            raise RuntimeError("environment_runtime_unavailable:docker_or_podman")
        if project_root is None or not project_root.is_dir():
            raise RuntimeError("environment_project_workspace_missing")
        container = self.resolve_container(project_id)
        if not container:
            hint = self.container or "my_defects4c_<project> / my_defects4c"
            raise RuntimeError(f"environment_container_not_running:{hint}")

        artifact_dir.mkdir(parents=True, exist_ok=True)
        suffix = uuid.uuid4().hex[:12]
        container_root = f"/tmp/debugging-framework/{safe_name(project_id, 70)}-{suffix}"
        self._run([self.runtime, "exec", container, "mkdir", "-p", container_root], 60)
        self._run([self.runtime, "cp", f"{project_root.resolve()}/.", f"{container}:{container_root}/"], self.timeout_seconds)
        image = self._inspect_image(container)
        log = (
            f"container={container}\n"
            f"container_root={container_root}\n"
            f"image_digest={image}\n"
        )
        (artifact_dir / "provision.log").write_text(log, encoding="utf-8")
        return DockerProvision(
            runtime=self.runtime,
            container=container,
            container_root=container_root,
            environment_digest=spec.digest,
            command=[self.runtime, "exec", container],
            output=log,
            image_digest=image,
        )

    def command(
        self,
        provision: DockerProvision,
        root: Path,
        argv: tuple[str, ...],
        cwd: Path,
        *,
        network: bool = False,
    ) -> list[str]:
        del network  # A running container owns its network policy.
        relative_cwd = cwd.resolve().relative_to(root.resolve()).as_posix()
        workdir = provision.container_root if relative_cwd == "." else (
            provision.container_root + "/" + relative_cwd
        )
        mapped = [self._map_argument(value, root, provision.container_root) for value in argv]
        if mapped and Path(mapped[0]).resolve() == Path(sys.executable).resolve():
            mapped[0] = "python3"
        script = "cd " + shlex.quote(workdir) + " && exec " + shlex.join(mapped)
        return [provision.runtime, "exec", provision.container, "bash", "-lc", script]

    @staticmethod
    def _map_argument(value: str, root: Path, container_root: str) -> str:
        text = str(value)
        host_root = str(root.resolve())
        if text == host_root:
            return container_root
        prefix = host_root + os.sep
        if text.startswith(prefix):
            return container_root + "/" + text[len(prefix):].replace(os.sep, "/")
        return text

    def sync_from_container(self, provision: DockerProvision, root: Path) -> None:
        # Commands run as the container's default user (usually root). Chown the
        # disposable tree first so the host-side cleanup can always remove it.
        uid = f"{os.getuid()}:{os.getgid()}"
        self._run(
            [provision.runtime, "exec", provision.container, "chown", "-R", uid, provision.container_root],
            120,
        )
        self._run(
            [provision.runtime, "cp", f"{provision.container}:{provision.container_root}/.", f"{root.resolve()}/"],
            self.timeout_seconds,
        )

    def cleanup(self, provision: DockerProvision) -> None:
        try:
            self._run(
                [provision.runtime, "exec", provision.container, "rm", "-rf", provision.container_root],
                120,
            )
        except RuntimeError:
            # Cleanup must not hide the baseline/validation result.
            pass

    def _inspect_image(self, container: str) -> str:
        result = self._run(
            [self.runtime, "inspect", "-f", "{{.Image}}", container],
            30,
            return_output=True,
        )
        return result.strip()

    @staticmethod
    def _run(command: list[str], timeout: int, *, return_output: bool = False) -> str:
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"container_command_failed:{type(exc).__name__}:{exc}") from exc
        if result.returncode != 0:
            detail = (result.stdout or "").strip().splitlines()
            raise RuntimeError(
                "container_command_failed:"
                + (detail[-1] if detail else str(result.returncode))
            )
        return result.stdout if return_output else ""
