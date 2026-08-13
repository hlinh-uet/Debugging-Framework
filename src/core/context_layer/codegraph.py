from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from src.core.context_layer.models import CodeGraphPreparation
from src.core.context_layer.runtime import resolve_codegraph_runtime
from src.utils.jsonio import atomic_write_json, atomic_write_text


class CodeGraphError(RuntimeError):
    pass


class CodeGraphBackend:
    """Prepare a bundled, repository-only CodeGraph index for one snapshot."""

    def __init__(
        self,
        *,
        mode: str = "auto",
        executable: str = "",
        timeout_seconds: int = 180,
    ):
        mode = str(mode or "auto").strip().lower()
        if mode not in {"off", "auto", "required"}:
            raise ValueError("context mode must be off, auto, or required")
        if timeout_seconds < 1:
            raise ValueError("CodeGraph timeout must be >= 1")
        self.mode = mode
        self.executable = str(executable or "").strip()
        self.timeout_seconds = int(timeout_seconds)

    @classmethod
    def from_settings(cls, settings) -> "CodeGraphBackend":
        return cls(
            mode=getattr(settings, "context_mode", "auto"),
            executable=getattr(settings, "codegraph_executable", ""),
            timeout_seconds=getattr(settings, "codegraph_timeout_seconds", 180),
        )

    def probe(self) -> CodeGraphPreparation:
        if self.mode == "off":
            return CodeGraphPreparation(mode=self.mode, status="disabled")
        started = time.monotonic()
        runtime = resolve_codegraph_runtime(self.executable)
        if not runtime.available or runtime.executable is None:
            return CodeGraphPreparation(
                mode=self.mode,
                status="unavailable",
                target=runtime.target,
                elapsed_seconds=round(time.monotonic() - started, 3),
                error=runtime.error,
            )
        completed, error = self._run(
            [str(runtime.executable), "version"],
            cwd=runtime.executable.parent,
            timeout=min(self.timeout_seconds, 20),
        )
        output = (completed.stdout or "").strip() if completed else ""
        version = _parse_version(output)
        if error:
            return CodeGraphPreparation(
                mode=self.mode,
                status="unavailable",
                target=runtime.target,
                executable=runtime.executable,
                elapsed_seconds=round(time.monotonic() - started, 3),
                error=error,
            )
        if runtime.expected_version and version != runtime.expected_version:
            return CodeGraphPreparation(
                mode=self.mode,
                status="unavailable",
                target=runtime.target,
                version=version,
                executable=runtime.executable,
                elapsed_seconds=round(time.monotonic() - started, 3),
                error=(
                    "codegraph_version_mismatch:"
                    f"expected={runtime.expected_version}:actual={version or output}"
                ),
            )
        return CodeGraphPreparation(
            mode=self.mode,
            status="available",
            ready=True,
            target=runtime.target,
            version=version,
            executable=runtime.executable,
            elapsed_seconds=round(time.monotonic() - started, 3),
        )

    def prepare(self, workspace: Path, artifact_dir: Path) -> CodeGraphPreparation:
        started = time.monotonic()
        workspace = workspace.expanduser().resolve()
        artifact_dir = artifact_dir.expanduser().resolve()
        artifact_dir.mkdir(parents=True, exist_ok=True)
        if self.mode == "off":
            report = CodeGraphPreparation(mode=self.mode, status="disabled")
            atomic_write_json(artifact_dir / "report.json", report.to_dict())
            return report

        probe = self.probe()
        if not probe.ready or probe.executable is None:
            report = CodeGraphPreparation(
                **{
                    **probe.to_dict(),
                    "executable": probe.executable,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            )
            atomic_write_json(artifact_dir / "report.json", report.to_dict())
            return self._enforce(report)

        self._write_extension_config(workspace)

        init_log = artifact_dir / "codegraph-init.log"
        status_log = artifact_dir / "codegraph-status.txt"
        completed, error = self._run(
            [str(probe.executable), "init", str(workspace)],
            cwd=workspace,
            timeout=self.timeout_seconds,
        )
        init_output = _command_log(completed, error)
        atomic_write_text(init_log, _bounded(init_output))
        if error or completed is None or completed.returncode != 0:
            detail = error or f"codegraph_init_exit_{completed.returncode}"
            report = CodeGraphPreparation(
                mode=self.mode,
                status="degraded",
                target=probe.target,
                version=probe.version,
                executable=probe.executable,
                elapsed_seconds=round(time.monotonic() - started, 3),
                error=detail,
                init_log_artifact=init_log.name,
            )
            atomic_write_json(artifact_dir / "report.json", report.to_dict())
            return self._enforce(report)

        status_completed, status_error = self._run(
            [str(probe.executable), "status", str(workspace)],
            cwd=workspace,
            timeout=min(self.timeout_seconds, 60),
        )
        status_output = _command_log(status_completed, status_error)
        atomic_write_text(status_log, _bounded(status_output))
        if (
            status_error
            or status_completed is None
            or status_completed.returncode != 0
            or not (workspace / ".codegraph").is_dir()
        ):
            detail = status_error or (
                f"codegraph_status_exit_{status_completed.returncode}"
                if status_completed is not None
                else "codegraph_status_failed"
            )
            report = CodeGraphPreparation(
                mode=self.mode,
                status="degraded",
                target=probe.target,
                version=probe.version,
                executable=probe.executable,
                elapsed_seconds=round(time.monotonic() - started, 3),
                error=detail,
                init_log_artifact=init_log.name,
                status_log_artifact=status_log.name,
            )
            atomic_write_json(artifact_dir / "report.json", report.to_dict())
            return self._enforce(report)

        report = CodeGraphPreparation(
            mode=self.mode,
            status="ready",
            ready=True,
            target=probe.target,
            version=probe.version,
            executable=probe.executable,
            elapsed_seconds=round(time.monotonic() - started, 3),
            init_log_artifact=init_log.name,
            status_log_artifact=status_log.name,
        )
        atomic_write_json(artifact_dir / "report.json", report.to_dict())
        return report

    @staticmethod
    def _write_extension_config(workspace: Path) -> None:
        """Cover C/C++ extensions accepted by the repair-path policy.

        A caller-owned CodeGraph configuration remains authoritative. The
        generated file is repository metadata for the disposable snapshot only
        and is ignored by its internal Git repository.
        """
        destination = workspace / "codegraph.json"
        if destination.exists() or destination.is_symlink():
            return
        atomic_write_json(destination, {
            "extensions": {
                ".hh": "cpp",
                ".inl": "cpp",
                ".inc": "cpp",
                ".ipp": "cpp",
                ".tpp": "cpp",
            }
        })

    def _enforce(self, report: CodeGraphPreparation) -> CodeGraphPreparation:
        if self.mode == "required" and not report.ready:
            raise CodeGraphError(report.error or "codegraph_context_unavailable")
        return report

    @staticmethod
    def _run(
        command: list[str],
        *,
        cwd: Path,
        timeout: int,
    ) -> tuple[subprocess.CompletedProcess[str] | None, str]:
        env = os.environ.copy()
        # `init` is a short-lived index build and does not start the MCP file
        # watcher. In CodeGraph 1.5, NO_WATCH also enables an interactive Git
        # hook offer after indexing, so do not inherit it from the host.
        env.pop("CODEGRAPH_NO_WATCH", None)
        env.update({
            "CODEGRAPH_TELEMETRY": "0",
            "DO_NOT_TRACK": "1",
            "CODEGRAPH_NO_DOWNLOAD": "1",
            "CODEGRAPH_NO_DAEMON": "1",
        })
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                # CodeGraph 1.5 may offer to install Git sync hooks after a
                # successful index when live watching is disabled. Preparation
                # is a batch operation and must never inherit the caller's TTY.
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # Preserve partial progress/prompt output for the attempt artifact;
            # otherwise a completed index followed by a prompt looks like an
            # opaque indexing timeout.
            output = _subprocess_text(exc.stdout or exc.output)
            completed = subprocess.CompletedProcess(
                command,
                returncode=-1,
                stdout=output,
            )
            return completed, f"codegraph_timeout:{timeout}s"
        except OSError as exc:
            return None, f"codegraph_start_error:{type(exc).__name__}:{exc}"
        if completed.returncode != 0:
            tail = "\n".join((completed.stdout or "").splitlines()[-20:])
            return completed, f"codegraph_exit_{completed.returncode}:{tail}"
        return completed, ""


def _parse_version(value: str) -> str:
    match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", value)
    return match.group(1) if match else ""


def _subprocess_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _command_log(
    completed: subprocess.CompletedProcess[str] | None,
    error: str,
) -> str:
    output = _subprocess_text(completed.stdout if completed is not None else "")
    if error:
        suffix = f"[debugging-framework] {error}"
        output = f"{output.rstrip()}\n{suffix}\n" if output.strip() else suffix + "\n"
    return output


def _bounded(value: str, limit: int = 200_000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]\n"
