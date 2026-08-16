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
    """Invoke ``codex exec`` as a bounded worker in a supplied writable workspace."""

    def __init__(
        self,
        *,
        executable: str,
        schema_path: Path,
        api_key: str = "",
        provider: str = "",
        base_url: str = "",
        wire_api: str = "responses",
        env_key: str = "CODEX_API_KEY",
        model: Optional[str] = None,
        timeout_seconds: int = 1800,
        inherit_user_config: bool = False,
    ):
        self.executable = executable
        self.schema_path = schema_path.resolve()
        self.api_key = api_key
        self.provider = provider.strip()
        self.base_url = base_url.strip()
        self.wire_api = wire_api.strip()
        self.env_key = env_key.strip() or "CODEX_API_KEY"
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.inherit_user_config = inherit_user_config

    def run(
        self,
        *,
        workspace: Path,
        prompt: str,
        artifact_dir: Path,
        tool_directories: tuple[Path, ...] = (),
    ) -> CodexRunResult:
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
            "workspace-write",
            "--ephemeral",
            "--json",
            "--color",
            "never",
            "--output-schema",
            str(self.schema_path),
            "--output-last-message",
            str(response_path),
            "-c",
            "approval_policy=\"never\"",
            "-c",
            "sandbox_workspace_write.network_access=true",
        ]
        if not self.inherit_user_config:
            command.extend(["--ignore-user-config", "--ignore-rules"])
        # Keep the Codex CLI harness and agent loop, while allowing the model
        # backend to be configured entirely from this project's .env. These
        # are CLI config overrides, not a direct OpenRouter API call.
        if self.provider:
            command.extend(["-c", f"model_provider={_toml_string(self.provider)}"])
            provider_prefix = f"model_providers.{self.provider}"
            command.extend(
                [
                    "-c",
                    f"{provider_prefix}.name={_toml_string(self.provider)}",
                    "-c",
                    f"{provider_prefix}.env_key={_toml_string(self.env_key)}",
                ]
            )
            if self.base_url:
                command.extend(
                    [
                        "-c",
                        f"{provider_prefix}.base_url={_toml_string(self.base_url)}",
                    ]
                )
            if self.wire_api:
                command.extend(
                    [
                        "-c",
                        f"{provider_prefix}.wire_api={_toml_string(self.wire_api)}",
                    ]
                )
        if self.model:
            command.extend(["--model", self.model])
        command.append("-")

        started = time.monotonic()
        process_env = os.environ.copy()
        resolved_tool_directories = [
            str(path.expanduser().resolve())
            for path in tool_directories
            if path.expanduser().resolve().is_dir()
        ]
        if resolved_tool_directories:
            process_env["PATH"] = os.pathsep.join(
                [*resolved_tool_directories, process_env.get("PATH", "")]
            )
            # Scope these controls to context-enabled attempts so context=off
            # preserves the exact legacy Codex process environment.
            process_env.update({
                "CODEGRAPH_TELEMETRY": "0",
                "DO_NOT_TRACK": "1",
                "CODEGRAPH_NO_DOWNLOAD": "1",
                "CODEGRAPH_NO_WATCH": "1",
                "CODEGRAPH_NO_DAEMON": "1",
                "CODEGRAPH_KERNEL": "0",
            })
        if self.api_key:
            # The key is scoped to `codex exec`; never place it in the command
            # list or any result artifact. The provider's env_key controls the
            # variable Codex reads (CODEX_API_KEY by default).
            process_env[self.env_key] = self.api_key
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
                env=process_env,
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
    paths = (payload.get("repair") or {}).get("paths")
    if (
        not isinstance(paths, list)
        or not paths
        or any(not isinstance(path, str) or not path.strip() for path in paths)
    ):
        return "codex_response_missing_repair_paths"
    return ""


def _toml_string(value: str) -> str:
    """Encode a small config string safely for Codex's ``-c key=value``."""
    return json.dumps(value, ensure_ascii=False)
