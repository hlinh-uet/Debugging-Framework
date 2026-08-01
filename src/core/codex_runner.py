from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class CodexRunResult:
    ok: bool
    payload: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    returncode: int = -1
    elapsed_seconds: float = 0.0
    command: list[str] = field(default_factory=list)


class CodexRunner:
    """Invoke `codex exec` as a bounded, read-only FL/APR worker."""

    def __init__(
        self,
        *,
        executable: str,
        schema_path: Path,
        model: Optional[str] = None,
        timeout_seconds: int = 1800,
        inherit_user_config: bool = False,
    ):
        self.executable = executable
        self.schema_path = schema_path.resolve()
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.inherit_user_config = inherit_user_config

    def run(self, *, workspace: Path, prompt: str, artifact_dir: Path) -> CodexRunResult:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = artifact_dir / "prompt.txt"
        events_path = artifact_dir / "events.jsonl"
        stderr_path = artifact_dir / "stderr.txt"
        response_path = artifact_dir / "response.json"
        prompt_path.write_text(prompt, encoding="utf-8")

        command = [
            self.executable,
            "exec",
            "--cd",
            str(workspace),
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--json",
            "--color",
            "never",
            "--output-schema",
            str(self.schema_path),
            "--output-last-message",
            str(response_path),
        ]
        if not self.inherit_user_config:
            command.extend(["--ignore-user-config", "--ignore-rules"])
        if self.model:
            command.extend(["--model", self.model])
        command.append("-")

        started = time.monotonic()
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(
                    input=prompt,
                    timeout=self.timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=10)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                stdout, stderr = process.communicate()
                events_path.write_text(stdout or "", encoding="utf-8")
                stderr_path.write_text(stderr or "", encoding="utf-8")
                return CodexRunResult(
                    ok=False,
                    error=f"codex_timeout:{self.timeout_seconds}s",
                    returncode=process.returncode or -1,
                    elapsed_seconds=time.monotonic() - started,
                    command=command,
                )
        except OSError as exc:
            return CodexRunResult(
                ok=False,
                error=f"codex_start_error:{exc}",
                elapsed_seconds=time.monotonic() - started,
                command=command,
            )

        events_path.write_text(stdout or "", encoding="utf-8")
        stderr_path.write_text(stderr or "", encoding="utf-8")
        elapsed = time.monotonic() - started
        if process.returncode != 0:
            tail = "\n".join((stderr or stdout or "").splitlines()[-20:])
            return CodexRunResult(
                ok=False,
                error=f"codex_exit_{process.returncode}:{tail}",
                returncode=process.returncode,
                elapsed_seconds=elapsed,
                command=command,
            )
        try:
            payload = json.loads(response_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return CodexRunResult(
                ok=False,
                error=f"codex_response_invalid:{exc}",
                returncode=process.returncode,
                elapsed_seconds=elapsed,
                command=command,
            )
        error = _validate_payload(payload)
        return CodexRunResult(
            ok=not error,
            payload=payload if isinstance(payload, dict) else {},
            error=error,
            returncode=process.returncode,
            elapsed_seconds=elapsed,
            command=command,
        )


def _validate_payload(payload: object) -> str:
    if not isinstance(payload, dict):
        return "codex_response_not_object"
    if not isinstance(payload.get("fault_localization"), list):
        return "codex_response_missing_fault_localization"
    if not isinstance(payload.get("repair"), dict):
        return "codex_response_missing_repair"
    diff = (payload.get("repair") or {}).get("diff")
    if not isinstance(diff, str) or not diff.strip():
        return "codex_response_missing_repair_diff"
    if not diff.startswith("--- a/") or "\n+++ b/" not in diff:
        return "codex_response_invalid_repair_diff_format"
    return ""
