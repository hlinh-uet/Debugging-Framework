from __future__ import annotations

"""Project-local configuration for the one-command repair workflow.

The project file deliberately has two independent concerns:

* build/test contract fields consumed by :mod:`src.validation.project`; and
* runtime ``repair``/``environment`` fields consumed by the CLI.

Keeping the runtime parser here lets conventional projects add only a
``repair`` section without accidentally becoming a custom build contract.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_CONFIG_NAME = ".debugging-framework.json"
PROJECT_CONFIG_SCHEMA_VERSION = 6
ENVIRONMENT_MODES = frozenset({"host", "image"})


@dataclass(frozen=True)
class RepairProjectConfig:
    failing_tests: tuple[str, ...] = ()
    attempts: int | None = None
    model: str = ""
    codex_timeout: int | None = None
    command_timeout: int | None = None
    jobs: int | None = None
    inherit_codex_config: bool | None = None
    output: str = ""
    source_extensions: tuple[str, ...] = ()


@dataclass(frozen=True)
class EnvironmentProjectConfig:
    mode: str = ""
    runtime: str = ""
    image: str = ""


@dataclass(frozen=True)
class WorkspaceProjectConfig:
    """Authorization for operating directly on the supplied project tree."""

    disposable: bool = False
    initialize_git_if_missing: bool = False


@dataclass(frozen=True)
class ProjectConfig:
    path: Path
    raw: dict[str, Any]
    repair: RepairProjectConfig = RepairProjectConfig()
    environment: EnvironmentProjectConfig = EnvironmentProjectConfig()
    workspace: WorkspaceProjectConfig = WorkspaceProjectConfig()

    @property
    def exists(self) -> bool:
        return bool(self.raw)


def project_config_path(root: Path) -> Path:
    return root.expanduser().resolve() / PROJECT_CONFIG_NAME


def read_project_config_data(root: Path) -> tuple[Path, dict[str, Any]]:
    """Read the raw project config without interpreting its build/test fields."""
    return read_project_config_file(project_config_path(root))


def read_project_config_file(path: Path) -> tuple[Path, dict[str, Any]]:
    """Read a project contract from an explicit path."""
    path = path.expanduser().resolve()
    if not path.is_file():
        return path, {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cấu hình project không hợp lệ {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Cấu hình project phải là JSON object: {path}")
    return path, raw


def load_project_config(root: Path) -> ProjectConfig:
    return load_project_config_file(project_config_path(root))


def load_project_config_file(path: Path) -> ProjectConfig:
    path, raw = read_project_config_file(path)
    return ProjectConfig(
        path=path,
        raw=raw,
        repair=_parse_repair(raw.get("repair"), path),
        environment=_parse_environment(raw.get("environment"), path),
        workspace=_parse_workspace(raw.get("workspace"), path),
    )


def repair_config_template(
    *,
    failing_tests: tuple[str, ...] = (),
    environment_mode: str,
    environment_runtime: str = "auto",
    environment_image: str = "",
) -> dict[str, Any]:
    """Return the runtime portion of the explicit partner contract."""
    environment: dict[str, str] = {"mode": environment_mode}
    if environment_mode == "image":
        environment["runtime"] = environment_runtime
        environment["image"] = environment_image
    return {
        "schema_version": PROJECT_CONFIG_SCHEMA_VERSION,
        "repair": {
            "failing_tests": list(failing_tests),
            "attempts": 2,
        },
        "environment": environment,
    }


def _parse_repair(value: object, path: Path) -> RepairProjectConfig:
    if value is None:
        return RepairProjectConfig()
    if not isinstance(value, dict):
        raise ValueError(f"{path}: repair phải là JSON object")
    _reject_unknown_fields(
        value,
        path,
        "repair",
        {
            "failing_tests", "attempts", "model",
            "codex_timeout", "command_timeout", "jobs", "inherit_codex_config", "output",
            "source_extensions",
        },
    )
    return RepairProjectConfig(
        failing_tests=_string_list(value.get("failing_tests", []), path, "repair.failing_tests"),
        attempts=_optional_int(value.get("attempts"), path, "repair.attempts", minimum=1),
        model=_optional_string(value.get("model"), path, "repair.model"),
        codex_timeout=_optional_int(
            value.get("codex_timeout"), path, "repair.codex_timeout", minimum=1
        ),
        command_timeout=_optional_int(
            value.get("command_timeout"), path, "repair.command_timeout", minimum=1
        ),
        jobs=_optional_int(value.get("jobs"), path, "repair.jobs", minimum=0),
        inherit_codex_config=_optional_bool(
            value.get("inherit_codex_config"), path, "repair.inherit_codex_config"
        ),
        output=_optional_string(value.get("output"), path, "repair.output"),
        source_extensions=_source_extensions(
            value.get("source_extensions", []), path, "repair.source_extensions"
        ),
    )


def _parse_workspace(value: object, path: Path) -> WorkspaceProjectConfig:
    if value is None:
        return WorkspaceProjectConfig()
    if not isinstance(value, dict):
        raise ValueError(f"{path}: workspace phải là JSON object")
    _reject_unknown_fields(
        value,
        path,
        "workspace",
        {"disposable", "initialize_git_if_missing"},
    )
    workspace = WorkspaceProjectConfig(
        disposable=bool(_optional_bool(value.get("disposable"), path, "workspace.disposable")),
        initialize_git_if_missing=bool(
            _optional_bool(
                value.get("initialize_git_if_missing"),
                path,
                "workspace.initialize_git_if_missing",
            )
        ),
    )
    if workspace.initialize_git_if_missing and not workspace.disposable:
        raise ValueError(
            f"{path}: workspace.initialize_git_if_missing cần workspace.disposable=true"
        )
    return workspace


def _parse_environment(value: object, path: Path) -> EnvironmentProjectConfig:
    if value is None:
        return EnvironmentProjectConfig()
    if not isinstance(value, dict):
        raise ValueError(f"{path}: environment phải là JSON object")
    _reject_unknown_fields(value, path, "environment", {"mode", "runtime", "image"})
    environment = EnvironmentProjectConfig(
        mode=_choice(value.get("mode"), path, "environment.mode", ENVIRONMENT_MODES),
        runtime=_optional_string(value.get("runtime"), path, "environment.runtime"),
        image=_optional_string(value.get("image"), path, "environment.image"),
    )
    if not environment.mode:
        raise ValueError(f"{path}: environment.mode là bắt buộc (host hoặc image)")
    if environment.mode == "image" and not environment.image:
        raise ValueError(f"{path}: environment.image là bắt buộc với mode image")
    if environment.mode == "host" and environment.image:
        raise ValueError(f"{path}: environment.image chỉ dùng với mode image")
    return environment


def _string_list(value: object, path: Path, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list):
        raise ValueError(f"{path}: {name} phải là chuỗi hoặc list chuỗi")
    output: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{path}: {name} không được chứa giá trị rỗng")
        text = item.strip()
        if text not in output:
            output.append(text)
    return tuple(output)


def _source_extensions(value: object, path: Path, name: str) -> tuple[str, ...]:
    values = _string_list(value, path, name)
    output: list[str] = []
    for item in values:
        extension = item.lower()
        if not extension.startswith(".") or len(extension) < 2:
            raise ValueError(f"{path}: {name} phải chứa extension dạng .ext")
        if any(character not in ".abcdefghijklmnopqrstuvwxyz0123456789_+-" for character in extension):
            raise ValueError(f"{path}: {name} chứa extension không hợp lệ: {item}")
        if extension not in output:
            output.append(extension)
    return tuple(output)


def _optional_string(value: object, path: Path, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: {name} phải là chuỗi không rỗng")
    return value.strip()


def _choice(value: object, path: Path, name: str, allowed: frozenset[str]) -> str:
    text = _optional_string(value, path, name)
    if text and text not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{path}: {name} phải là một trong: {choices}")
    return text


def _optional_int(value: object, path: Path, name: str, *, minimum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{path}: {name} phải là số nguyên >= {minimum}")
    return value


def _optional_bool(value: object, path: Path, name: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{path}: {name} phải là true hoặc false")
    return value


def _reject_unknown_fields(
    value: dict[str, Any], path: Path, name: str, allowed: set[str]
) -> None:
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ValueError(f"{path}: {name} có field không được hỗ trợ: {', '.join(unknown)}")
