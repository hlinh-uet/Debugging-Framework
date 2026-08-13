from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_RESULTS_DIR = (
    Path(os.environ["XDG_STATE_HOME"]).expanduser()
    if os.environ.get("XDG_STATE_HOME")
    else Path.home() / ".local" / "state"
) / "debugging-framework" / "results"
KNOWN_ENV_KEYS = {
    "CODEX_API_KEY", "DEBUGGING_REQUIRE_API_KEY", "DEBUGGING_CODEX_PROVIDER",
    "DEBUGGING_CODEX_BASE_URL", "DEBUGGING_CODEX_WIRE_API", "DEBUGGING_CODEX_ENV_KEY",
    "DEBUGGING_CODEX_MODEL", "DEBUGGING_CODEX_BIN", "DEBUGGING_ATTEMPTS",
    "DEBUGGING_TIMEOUT", "DEBUGGING_COMMAND_TIMEOUT", "DEBUGGING_JOBS",
    "DEBUGGING_INHERIT_CODEX_CONFIG", "DEBUGGING_RESULTS_DIR",
    "DEBUGGING_ENVIRONMENT_BACKEND", "DEBUGGING_ENVIRONMENT_RUNTIME",
    "DEBUGGING_ENVIRONMENT_IMAGE",
    "DEBUGGING_CONTEXT_MODE", "DEBUGGING_CODEGRAPH_BIN",
    "DEBUGGING_CODEGRAPH_TIMEOUT",
}


def read_env_file(path: Path = DEFAULT_ENV_FILE) -> dict[str, str]:
    path = path.expanduser().resolve()
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line_number, original in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            raise ValueError(f"{path}:{line_number}: dòng .env phải có KEY=VALUE")
        key, raw = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "a").isalnum() or not key[0].isalpha():
            raise ValueError(f"{path}:{line_number}: tên biến .env không hợp lệ")
        values[key] = _parse_env_value(raw.strip(), path, line_number)
    unknown = sorted(
        key for key in values
        if key.startswith(("DEBUGGING_", "CODEX_")) and key not in KNOWN_ENV_KEYS
    )
    if unknown:
        raise ValueError(f"Biến .env không được hỗ trợ: {', '.join(unknown)}")
    return values


def _parse_env_value(raw: str, path: Path, line_number: int) -> str:
    if not raw:
        return ""
    if raw[0] in {"'", '"'}:
        try:
            value = ast.literal_eval(raw)
        except (SyntaxError, ValueError) as exc:
            raise ValueError(f"{path}:{line_number}: giá trị quote không hợp lệ") from exc
        if not isinstance(value, str):
            raise ValueError(f"{path}:{line_number}: giá trị phải là chuỗi")
        return value
    return raw.split(" #", 1)[0].rstrip()


def _value(name: str, files: Mapping[str, str], environ: Mapping[str, str], default: str = "") -> str:
    if name in environ:
        return str(environ[name]).strip()
    if name in files:
        return str(files[name]).strip()
    return default


def _bool(name: str, raw: str) -> bool:
    value = raw.lower().strip()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} phải là true/false; nhận {raw!r}")


def _int(name: str, raw: str, minimum: int) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} phải là số nguyên; nhận {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} phải >= {minimum}; nhận {value}")
    return value


def _path(raw: str, default: Path) -> Path:
    value = Path(raw).expanduser() if raw else default
    if not value.is_absolute():
        value = PROJECT_ROOT / value
    return value.resolve()


def _choice(name: str, raw: str, allowed: set[str]) -> str:
    value = raw.lower().strip()
    if value not in allowed:
        raise ValueError(f"{name} phải là một trong: {', '.join(sorted(allowed))}")
    return value


@dataclass(frozen=True)
class FrameworkConfig:
    env_file: Path = DEFAULT_ENV_FILE
    results_dir: Path = DEFAULT_RESULTS_DIR
    codex_executable: str = "codex"
    codex_api_key: str = field(default="", repr=False)
    codex_provider: str = ""
    codex_base_url: str = ""
    codex_wire_api: str = "responses"
    codex_env_key: str = "CODEX_API_KEY"
    require_api_key: bool = False
    model: str = "gpt-5.6-sol"
    attempts: int = 2
    codex_timeout_seconds: int = 1800
    command_timeout_seconds: int = 1800
    jobs: int = 0
    inherit_codex_config: bool = False
    # No implicit execution fallback: a project/CLI/.env must explicitly choose
    # the caller-prepared host or a caller-packaged image.
    environment_backend: str = ""
    environment_runtime: str = "auto"
    environment_image: str = ""
    context_mode: str = "auto"
    codegraph_executable: str = ""
    codegraph_timeout_seconds: int = 180

    @classmethod
    def load(
        cls, path: Path = DEFAULT_ENV_FILE, environ: Mapping[str, str] | None = None
    ) -> "FrameworkConfig":
        files = read_env_file(path)
        active = os.environ if environ is None else environ

        def get(name: str, default: str = "") -> str:
            return _value(name, files, active, default)

        timeout = get("DEBUGGING_TIMEOUT", "1800")
        return cls(
            env_file=path.expanduser().resolve(),
            results_dir=_path(get("DEBUGGING_RESULTS_DIR"), DEFAULT_RESULTS_DIR),
            codex_executable=get("DEBUGGING_CODEX_BIN", "codex"),
            codex_api_key=get("CODEX_API_KEY"),
            codex_provider=get("DEBUGGING_CODEX_PROVIDER"),
            codex_base_url=get("DEBUGGING_CODEX_BASE_URL"),
            codex_wire_api=get("DEBUGGING_CODEX_WIRE_API", "responses"),
            codex_env_key=get("DEBUGGING_CODEX_ENV_KEY", "CODEX_API_KEY"),
            require_api_key=_bool("DEBUGGING_REQUIRE_API_KEY", get("DEBUGGING_REQUIRE_API_KEY", "false")),
            model=get("DEBUGGING_CODEX_MODEL", "gpt-5.6-sol"),
            attempts=_int("DEBUGGING_ATTEMPTS", get("DEBUGGING_ATTEMPTS", "2"), 1),
            codex_timeout_seconds=_int("DEBUGGING_TIMEOUT", timeout, 1),
            command_timeout_seconds=_int(
                "DEBUGGING_COMMAND_TIMEOUT", get("DEBUGGING_COMMAND_TIMEOUT", timeout), 1
            ),
            jobs=_int("DEBUGGING_JOBS", get("DEBUGGING_JOBS", "0"), 0),
            inherit_codex_config=_bool(
                "DEBUGGING_INHERIT_CODEX_CONFIG", get("DEBUGGING_INHERIT_CODEX_CONFIG", "false")
            ),
            environment_backend=get("DEBUGGING_ENVIRONMENT_BACKEND", ""),
            environment_runtime=get("DEBUGGING_ENVIRONMENT_RUNTIME", "auto"),
            environment_image=get("DEBUGGING_ENVIRONMENT_IMAGE", ""),
            context_mode=_choice(
                "DEBUGGING_CONTEXT_MODE",
                get("DEBUGGING_CONTEXT_MODE", "auto"),
                {"off", "auto", "required"},
            ),
            codegraph_executable=get("DEBUGGING_CODEGRAPH_BIN", ""),
            codegraph_timeout_seconds=_int(
                "DEBUGGING_CODEGRAPH_TIMEOUT",
                get("DEBUGGING_CODEGRAPH_TIMEOUT", "180"),
                1,
            ),
        )


@dataclass(frozen=True)
class Settings:
    results_dir: Path = DEFAULT_RESULTS_DIR
    codex_executable: str = "codex"
    codex_api_key: str = field(default="", repr=False)
    codex_provider: str = ""
    codex_base_url: str = ""
    codex_wire_api: str = "responses"
    codex_env_key: str = "CODEX_API_KEY"
    environment_backend: str = ""
    environment_runtime: str = "auto"
    environment_image: str = ""
    context_mode: str = "auto"
    codegraph_executable: str = ""
    codegraph_timeout_seconds: int = 180

    @property
    def output_schema(self) -> Path:
        return Path(__file__).resolve().parents[1] / "schemas" / "codex_result.schema.json"

    def validated(self) -> "Settings":
        if not self.output_schema.is_file():
            raise FileNotFoundError(f"Thiếu Codex output schema: {self.output_schema}")
        if not self.environment_backend:
            raise ValueError(
                "Phải chọn rõ environment mode: --environment host hoặc image; "
                "framework không fallback môi trường"
            )
        if self.environment_backend not in {"host", "image"}:
            raise ValueError("Environment mode chỉ hỗ trợ host hoặc image")
        if self.environment_backend == "image" and not self.environment_image.strip():
            raise ValueError(
                "DEBUGGING_ENVIRONMENT_IMAGE/--environment-image là bắt buộc với mode image"
            )
        if self.environment_backend == "host" and self.environment_image.strip():
            raise ValueError("--environment-image chỉ được dùng với environment image")
        if self.context_mode not in {"off", "auto", "required"}:
            raise ValueError("Context mode chỉ hỗ trợ off, auto hoặc required")
        if self.codegraph_timeout_seconds < 1:
            raise ValueError("CodeGraph timeout phải >= 1")
        return Settings(
            results_dir=self.results_dir.expanduser().resolve(),
            codex_executable=self.codex_executable,
            codex_api_key=self.codex_api_key,
            codex_provider=self.codex_provider,
            codex_base_url=self.codex_base_url,
            codex_wire_api=self.codex_wire_api,
            codex_env_key=self.codex_env_key,
            environment_backend=self.environment_backend,
            environment_runtime=self.environment_runtime,
            environment_image=self.environment_image.strip(),
            context_mode=self.context_mode,
            codegraph_executable=self.codegraph_executable.strip(),
            codegraph_timeout_seconds=self.codegraph_timeout_seconds,
        )
