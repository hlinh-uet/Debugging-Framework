from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.pipeline import (
    DebuggingPipeline,
    PipelineOptions,
    classify_validation_result,
)
from src.core.context_layer import CodeGraphBackend
from src.loaders.project import ProjectLoader
from src.utils.config import FrameworkConfig, Settings
from src.utils.jsonio import atomic_write_json, atomic_write_text, safe_name
from src.utils.project_config import (
    PROJECT_CONFIG_NAME,
    PROJECT_CONFIG_SCHEMA_VERSION,
    ProjectConfig,
    load_project_config,
    load_project_config_file,
    read_project_config_data,
    repair_config_template,
)
from src.utils.workspace import ProjectWorkspace
from src.validation.project import BuildDetector, CommandSpec, ProjectValidator


def build_parser(config: FrameworkConfig | None = None) -> argparse.ArgumentParser:
    # Project-level configuration is merged only after its project path is
    # known. Keep parser defaults suppressed so omitted CLI flags cannot
    # overwrite that configuration.
    _ = config
    parser = argparse.ArgumentParser(
        prog="debugging-framework",
        description="Nhận project, lưu raw unified patch và validation cách ly.",
    )
    parser.add_argument("--results-dir", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--codex-bin", default=argparse.SUPPRESS)
    parser.add_argument(
        "--environment",
        dest="environment_backend",
        choices=("host", "image"),
        default=argparse.SUPPRESS,
        help="Explicit caller-prepared host or prebuilt image; no fallback.",
    )
    parser.add_argument(
        "--environment-runtime", default=argparse.SUPPRESS,
        help="OCI runtime executable (docker/podman) or auto.",
    )
    parser.add_argument(
        "--environment-image", default=argparse.SUPPRESS,
        help="Prebuilt OCI image name/digest used with mode image.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    inspect_parser = sub.add_parser(
        "inspect", help="Xem build/full-suite contract hoặc plan tự nhận diện, chưa chạy lệnh."
    )
    inspect_parser.add_argument(
        "project", type=Path, nargs="?",
        help="Project root; mặc định là thư mục hiện tại.",
    )
    inspect_parser.add_argument("--json", action="store_true")
    add_environment_options(inspect_parser)

    repair_parser = sub.add_parser(
        "repair",
        help="FL/APR trên snapshot từ failure evidence đã cung cấp.",
    )
    repair_parser.add_argument(
        "--project", type=Path, default=argparse.SUPPRESS,
        help="Path to the buggy project directory.",
    )
    repair_parser.add_argument(
        "--config", dest="project_config", type=Path, default=argparse.SUPPRESS,
        help="Path to the independent Debugging-Framework input contract JSON.",
    )
    repair_parser.add_argument(
        "--failing-output", "--failure-output", "--baseline-output",
        dest="failing_output", type=Path, default=argparse.SUPPRESS,
        help="Required path to the separate actual failing-test output file.",
    )
    repair_parser.add_argument("--output", type=Path, default=argparse.SUPPRESS, help="File patch output.")
    add_repair_options(repair_parser)

    validate_parser = sub.add_parser("validate", help="Build/test lại một unified diff trên project.")
    validate_parser.add_argument("project", type=Path)
    validate_parser.add_argument("patch", type=Path)
    validate_parser.add_argument(
        "--failing-test", dest="failing_tests", action="append",
        default=argparse.SUPPRESS,
        help="Target failing test(s) to reproduce and validate; may be repeated.",
    )
    validate_parser.add_argument("--command-timeout", type=int, default=argparse.SUPPRESS)
    validate_parser.add_argument("--jobs", type=int, default=argparse.SUPPRESS)
    add_environment_options(validate_parser)

    doctor_parser = sub.add_parser(
        "doctor", help="Kiểm tra Codex, environment và build/full-suite contract."
    )
    doctor_parser.add_argument(
        "project", type=Path, nargs="?",
        help="Project root; mặc định là thư mục hiện tại.",
    )
    doctor_parser.add_argument(
        "--config", dest="project_config", type=Path, default=argparse.SUPPRESS,
        help="Optional independent Debugging-Framework input contract JSON.",
    )
    doctor_parser.add_argument("--jobs", type=int, default=argparse.SUPPRESS)
    add_environment_options(doctor_parser)

    init_parser = sub.add_parser(
        "init", help="Tạo project config cho workflow `debugging-framework repair` một lệnh."
    )
    init_parser.add_argument(
        "project", type=Path, nargs="?", help="Project root; mặc định là thư mục hiện tại."
    )
    init_parser.add_argument(
        "--failing-test", "--test-id", dest="failing_tests", action="append",
        default=argparse.SUPPRESS,
        help="Test ID để ghi sẵn vào repair.failing_tests; có thể lặp option.",
    )
    init_parser.add_argument(
        "--failure-output", dest="failing_output", type=Path, default=argparse.SUPPRESS,
        help="Đường dẫn output fail, relative to project root (mặc định .debugging-framework/failure.log).",
    )
    init_parser.add_argument(
        "--force", action="store_true", help="Thay thế repair settings hiện có, giữ build/test contract."
    )
    add_environment_options(init_parser)
    return parser


def add_environment_options(parser: argparse.ArgumentParser) -> None:
    # SUPPRESS lets a value written after the subcommand override the global
    # option without replacing the global default when it is omitted.
    parser.add_argument(
        "--environment",
        dest="environment_backend",
        choices=("host", "image"),
        default=argparse.SUPPRESS,
        help="Explicit caller-prepared host or prebuilt image; no fallback.",
    )
    parser.add_argument(
        "--environment-runtime", default=argparse.SUPPRESS,
        help="OCI runtime executable (docker/podman) or auto.",
    )
    parser.add_argument(
        "--environment-image", default=argparse.SUPPRESS,
        help="Prebuilt OCI image name/digest used with mode image.",
    )


def add_repair_options(
    parser: argparse.ArgumentParser,
) -> None:
    parser.add_argument("--attempts", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--model", default=argparse.SUPPRESS)
    parser.add_argument("--codex-timeout", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--command-timeout", type=int, default=argparse.SUPPRESS)
    parser.add_argument("--jobs", type=int, default=argparse.SUPPRESS)
    parser.add_argument(
        "--inherit-codex-config", action=argparse.BooleanOptionalAction,
        default=argparse.SUPPRESS,
    )


def _prepare_repair_file_inputs(args: argparse.Namespace) -> None:
    """Resolve the strict three-path repair contract before loading the project."""
    if args.command != "repair":
        return
    project = getattr(args, "project", None)
    if project is None:
        raise ValueError("repair cần --project /path/to/buggy-project")
    project = project.expanduser().resolve()
    if not project.is_dir():
        raise FileNotFoundError(f"Project không tồn tại hoặc không phải thư mục: {project}")
    configured = getattr(args, "project_config", None)
    if configured is None:
        raise ValueError("repair cần --config /path/to/project-config.json")
    configured = configured.expanduser()
    if configured.is_symlink():
        raise ValueError("--config không được là symlink")
    configured = configured.resolve()
    if not configured.is_file():
        raise FileNotFoundError(f"Project config không tồn tại: {configured}")

    failure_output = getattr(args, "failing_output", None)
    if failure_output is None:
        raise ValueError("repair cần --failure-output /path/to/failure_output.log")
    if str(failure_output) == "-":
        raise ValueError(
            "repair contract ba path không hỗ trợ stdin; "
            "--failure-output phải là path tới file"
        )
    failure_output = failure_output.expanduser()
    if failure_output.is_symlink():
        raise ValueError("--failure-output không được là symlink")
    failure_output = failure_output.resolve()
    if not failure_output.is_file():
        raise FileNotFoundError(f"Failing-test output không tồn tại: {failure_output}")
    if failure_output == configured:
        raise ValueError("--config và --failure-output phải là hai file khác nhau")

    args.project = project
    args.project_config = configured
    args.failing_output = failure_output
    try:
        args.project_config_sha256 = hashlib.sha256(configured.read_bytes()).hexdigest()
        args.failure_output_sha256 = hashlib.sha256(failure_output.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeError(f"Không đọc được repair input: {exc}") from exc


def _validate_repair_config_contract(
    args: argparse.Namespace, project_config: ProjectConfig
) -> None:
    """Make the project config authoritative for tests and environment."""
    if args.command != "repair":
        return
    if project_config.raw.get("schema_version") != PROJECT_CONFIG_SCHEMA_VERSION:
        raise ValueError(
            f"{project_config.path}: repair contract yêu cầu "
            f"schema_version={PROJECT_CONFIG_SCHEMA_VERSION}"
        )
    disallowed_overrides = {
        "failing_tests": "--failing-test/--test-id",
        "environment_backend": "--environment",
        "environment_runtime": "--environment-runtime",
        "environment_image": "--environment-image",
    }
    used = [flag for field, flag in disallowed_overrides.items() if hasattr(args, field)]
    if used:
        raise ValueError(
            "Ba-path repair contract yêu cầu test/environment nằm trong --config; "
            "không dùng CLI override: " + ", ".join(used)
        )
    if not project_config.repair.failing_tests:
        raise ValueError(
            f"{project_config.path}: repair.failing_tests là bắt buộc"
        )
    if not project_config.environment.mode:
        raise ValueError(
            f"{project_config.path}: environment.mode là bắt buộc (host hoặc image)"
        )
    regression = project_config.raw.get("regression_test")
    if not regression or not isinstance(regression, (str, dict, list)):
        raise ValueError(
            f"{project_config.path}: regression_test full suite là bắt buộc"
        )
    if "test" in project_config.raw:
        raise ValueError(
            f"{project_config.path}: field test không còn dùng trong schema "
            f"{PROJECT_CONFIG_SCHEMA_VERSION}; hãy dùng regression_test"
        )


_UNSET = object()


@dataclass(frozen=True)
class _ResolvedValue:
    value: Any
    source: str


def _cli_value(args: argparse.Namespace, name: str) -> object:
    return getattr(args, name, _UNSET)


def _is_set(value: object) -> bool:
    return value is not _UNSET and value is not None and value != "" and value != ()


def _first_value(*values: tuple[object, str]) -> _ResolvedValue:
    for value, source in values:
        if _is_set(value):
            return _ResolvedValue(value, source)
    return _ResolvedValue(None, "default")


def _project_relative_path(value: object, root: Path) -> Path | str:
    raw = str(value)
    if raw == "-":
        return raw
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def apply_effective_options(
    args: argparse.Namespace,
    config: FrameworkConfig,
    project_config: ProjectConfig,
) -> None:
    """Merge explicit CLI, project config, dataset defaults, then global config."""
    repair = project_config.repair
    environment = project_config.environment

    args.results_dir = _first_value(
        (_cli_value(args, "results_dir"), "cli"),
        (config.results_dir, "framework"),
    ).value
    args.codex_bin = _first_value(
        (_cli_value(args, "codex_bin"), "cli"),
        (config.codex_executable, "framework"),
    ).value
    selected_environment = _first_value(
        (_cli_value(args, "environment_backend"), "cli"),
        (environment.mode, "project"),
        (config.environment_backend, "framework"),
    )
    args.environment_backend = selected_environment.value
    if selected_environment.source == "cli":
        selected_runtime, selected_image = "", ""
    elif selected_environment.source == "project":
        selected_runtime, selected_image = environment.runtime, environment.image
    else:
        selected_runtime, selected_image = config.environment_runtime, config.environment_image
    args.environment_runtime = _first_value(
        (_cli_value(args, "environment_runtime"), "cli"),
        (selected_runtime, selected_environment.source),
        ("auto", "default"),
    ).value
    args.environment_image = _first_value(
        (_cli_value(args, "environment_image"), "cli"),
        (selected_image, selected_environment.source),
    ).value or ""

    if args.command != "repair":
        args.command_timeout = _first_value(
            (_cli_value(args, "command_timeout"), "cli"),
            (config.command_timeout_seconds, "framework"),
        ).value
        args.jobs = _first_value(
            (_cli_value(args, "jobs"), "cli"),
            (config.jobs, "framework"),
        ).value
        return

    args.failing_tests = list(repair.failing_tests)
    args.failing_output = _cli_value(args, "failing_output")
    output = _first_value(
        (_cli_value(args, "output"), "cli"),
        (repair.output, "project"),
    )
    if output.value is None:
        args.output = None
    elif output.source == "project":
        args.output = _project_relative_path(output.value, Path(args.project))
    else:
        args.output = output.value
    args.attempts = _first_value(
        (_cli_value(args, "attempts"), "cli"),
        (repair.attempts, "project"),
        (config.attempts, "framework"),
    ).value
    args.model = _first_value(
        (_cli_value(args, "model"), "cli"),
        (repair.model, "project"),
        (config.model, "framework"),
    ).value
    args.codex_timeout = _first_value(
        (_cli_value(args, "codex_timeout"), "cli"),
        (repair.codex_timeout, "project"),
        (config.codex_timeout_seconds, "framework"),
    ).value
    args.command_timeout = _first_value(
        (_cli_value(args, "command_timeout"), "cli"),
        (repair.command_timeout, "project"),
        (config.command_timeout_seconds, "framework"),
    ).value
    args.jobs = _first_value(
        (_cli_value(args, "jobs"), "cli"),
        (repair.jobs, "project"),
        (config.jobs, "framework"),
    ).value
    args.inherit_codex_config = _first_value(
        (_cli_value(args, "inherit_codex_config"), "cli"),
        (repair.inherit_codex_config, "project"),
        (config.inherit_codex_config, "framework"),
    ).value


def main(argv: list[str] | None = None) -> int:
    try:
        config = FrameworkConfig.load()
        raw_argv = list(argv if argv is not None else sys.argv[1:])
        args = build_parser(config).parse_args(raw_argv)
        _prepare_repair_file_inputs(args)
        if args.command in {"inspect", "doctor", "init"} and getattr(args, "project", None) is None:
            args.project = Path.cwd()
        explicit_config = getattr(args, "project_config", None)
        project = ProjectLoader().load(args.project, config_path=explicit_config)

        if args.command == "init":
            return init_project(project, args, config)

        project_config = (
            load_project_config_file(explicit_config)
            if explicit_config is not None
            else load_project_config(project.path)
        )
        _validate_repair_config_contract(args, project_config)
        apply_effective_options(args, config, project_config)
        settings = Settings(
            results_dir=args.results_dir,
            codex_executable=args.codex_bin,
            codex_api_key=config.codex_api_key,
            codex_provider=config.codex_provider,
            codex_base_url=config.codex_base_url,
            codex_wire_api=config.codex_wire_api,
            codex_env_key=config.codex_env_key,
            environment_backend=args.environment_backend,
            environment_runtime=args.environment_runtime,
            environment_image=args.environment_image,
            context_mode=config.context_mode,
            codegraph_executable=config.codegraph_executable,
            codegraph_timeout_seconds=config.codegraph_timeout_seconds,
        ).validated()

        if args.command == "inspect":
            validator = ProjectValidator(
                environment_backend=settings.environment_backend,
                environment_runtime=getattr(settings, "environment_runtime", "auto"),
                environment_image=getattr(settings, "environment_image", ""),
            )
            plan = validator.inspect(project)
            environment = validator.resolve_environment(project, plan)
            if args.json:
                print(json.dumps({
                    "plan": plan.as_dict(),
                    "plan_digest": validator.plan_digest(plan),
                    "environment": environment.as_dict(),
                }, indent=2, ensure_ascii=False))
            else:
                _print_plan(project.path, plan.as_dict(), environment.as_dict())
            return 0

        if args.command == "doctor":
            return doctor(settings, project, args.jobs)

        if args.command == "repair":
            validate_repair_arguments(config, settings, args)
            external_output = read_failure_output(args.failing_output)
            result = repair_project(
                settings,
                project,
                args,
                args.output,
                args.failing_tests,
                external_baseline_output=external_output,
            )
            print(json.dumps({
                "status": result.get("status"),
                "output_patch": result.get("output_patch", ""),
                "patch_validation_passed": result.get("patch_validation_passed", False),
                "test_oracle_modified": result.get("test_oracle_modified", False),
                "blocked_patch_paths": result.get("blocked_patch_paths", []),
                "workspace_patch_artifact": result.get("workspace_patch_artifact", ""),
                "repair_paths": result.get("repair_paths", []),
                "validation_error": result.get("validation_error", ""),
            }, indent=2, ensure_ascii=False))
            return 0 if result.get("status") == "plausible" else 1

        if args.command == "validate":
            return validate_patch(
                settings, project, args.patch, args.command_timeout, args.jobs,
                normalize_failing_tests(getattr(args, "failing_tests", None)),
            )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(json.dumps({
            "status": "error",
            "stage": "preflight",
            "error": str(exc),
        }, ensure_ascii=False), file=sys.stderr)
        return 2
    return 2


def init_project(project, args: argparse.Namespace, config: FrameworkConfig) -> int:
    """Create/update an explicit build and full-suite partner contract."""
    # Parse first so init never silently preserves a malformed environment or
    # build/test contract. Existing command fields are intentionally retained.
    existing_config = load_project_config(project.path)
    config_path, raw = read_project_config_data(project.path)
    if "repair" in raw and not args.force:
        raise ValueError(
            f"{PROJECT_CONFIG_NAME} đã có repair settings; dùng --force để thay thế repair settings"
        )

    failing_tests = normalize_failing_tests(getattr(args, "failing_tests", None))
    failure_output = _portable_failure_output(
        getattr(args, "failing_output", ".debugging-framework/failure.log"), project.path
    )
    cli_mode = str(getattr(args, "environment_backend", "") or "").strip()
    if cli_mode:
        environment_mode = cli_mode
        environment_runtime = str(
            getattr(args, "environment_runtime", "") or "auto"
        ).strip()
        environment_image = str(getattr(args, "environment_image", "") or "").strip()
    elif existing_config.environment.mode:
        environment_mode = existing_config.environment.mode
        environment_runtime = str(
            getattr(args, "environment_runtime", "")
            or existing_config.environment.runtime
            or "auto"
        ).strip()
        environment_image = str(
            getattr(args, "environment_image", "") or existing_config.environment.image
        ).strip()
    else:
        environment_mode = str(config.environment_backend or "").strip()
        environment_runtime = str(
            getattr(args, "environment_runtime", "")
            or config.environment_runtime
            or "auto"
        ).strip()
        environment_image = str(
            getattr(args, "environment_image", "") or config.environment_image
        ).strip()
    if not environment_mode:
        raise ValueError("init cần --environment host hoặc --environment image")
    if environment_mode not in {"host", "image"}:
        raise ValueError("init environment mode chỉ hỗ trợ host hoặc image; không fallback")
    if environment_mode == "image" and not environment_image:
        raise ValueError("init --environment image cần --environment-image")
    if environment_mode == "host" and environment_image:
        raise ValueError("--environment-image chỉ dùng với --environment image")
    template = repair_config_template(
        failing_tests=failing_tests,
        environment_mode=environment_mode,
        environment_runtime=environment_runtime,
        environment_image=environment_image,
    )
    merged = dict(raw)
    merged["schema_version"] = template["schema_version"]
    merged["repair"] = template["repair"]
    merged.pop("test", None)
    # Do not overwrite an operator-provided environment, even with --force.
    if "environment" not in merged:
        merged["environment"] = template["environment"]
    if not merged.get("regression_test"):
        detected = BuildDetector(jobs=config.jobs).detect(project.path)
        merged["system"] = detected.system
        merged["setup"] = [
            _command_contract_value(command) for command in detected.setup
        ]
        merged["build"] = [
            _command_contract_value(command) for command in detected.build
        ]
        merged["regression_test"] = [
            _command_contract_value(command)
            for command in detected.regression_test
        ]
    atomic_write_json(config_path, merged)

    _create_failure_output_parent(project.path, failure_output)
    gitignore_updated = _add_failure_output_to_gitignore(project.path, failure_output)
    failure_path = Path(failure_output)
    if not failure_path.is_absolute():
        failure_path = (project.path / failure_path).resolve()
    print(json.dumps({
        "config": str(config_path),
        "failing_tests": list(failing_tests),
        "failure_output": failure_output,
        "gitignore_updated": gitignore_updated,
        "next": (
            "Ghi actual output vào failure_output rồi chạy: "
            f"debugging-framework repair --project {project.path} --config {config_path} "
            f"--failure-output {failure_path}"
        ),
    }, indent=2, ensure_ascii=False))
    return 0


def _command_contract_value(command: CommandSpec):
    """Serialize a detected command into the user-editable JSON contract."""
    if (
        command.cwd == "."
        and not command.evidence_pattern
        and not command.failure_pattern
    ):
        return list(command.argv)
    value: dict[str, object] = {
        "command": list(command.argv),
        "cwd": command.cwd,
    }
    if command.evidence_pattern:
        value["evidence_pattern"] = command.evidence_pattern
    if command.failure_pattern:
        value["failure_pattern"] = command.failure_pattern
    return value


def _portable_failure_output(value: Path | str, root: Path) -> str:
    raw = str(value).strip()
    if not raw or raw == "-":
        raise ValueError("init cần file --failure-output, không hỗ trợ stdin trong project config")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return path.as_posix()
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _create_failure_output_parent(root: Path, configured_path: str) -> None:
    path = Path(configured_path)
    if path.is_absolute():
        return
    destination = (root / path).resolve()
    try:
        destination.relative_to(root.resolve())
    except ValueError:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)


def _add_failure_output_to_gitignore(root: Path, configured_path: str) -> bool:
    path = Path(configured_path)
    if path.is_absolute() or ".." in path.parts:
        return False
    entry = path.as_posix()
    while entry.startswith("./"):
        entry = entry[2:]
    if not entry:
        return False
    gitignore = root / ".gitignore"
    try:
        current = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    except OSError as exc:
        raise RuntimeError(f"Không đọc được {gitignore}: {exc}") from exc
    if entry in {line.strip() for line in current.splitlines()}:
        return False
    updated = current
    if updated and not updated.endswith("\n"):
        updated += "\n"
    updated += entry + "\n"
    atomic_write_text(gitignore, updated)
    return True


def normalize_failing_tests(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        raise ValueError("failing_tests phải là chuỗi hoặc danh sách tên test")
    normalized = tuple(str(item).strip() for item in values if str(item).strip())
    return normalized


def validate_repair_arguments(config: FrameworkConfig, settings: Settings, args) -> None:
    if config.require_api_key and not settings.codex_api_key:
        raise ValueError("CODEX_API_KEY chưa được cấu hình")
    if args.attempts < 1 or args.codex_timeout < 1 or args.command_timeout < 1:
        raise ValueError("attempts/timeout phải >= 1")
    if args.jobs < 0:
        raise ValueError("--jobs phải >= 0")
    if not normalize_failing_tests(args.failing_tests):
        raise ValueError(
            "cần ít nhất một repair.failing_tests trong "
            f"{PROJECT_CONFIG_NAME}"
        )
    if args.failing_output is None:
        raise ValueError("repair cần --failure-output /path/to/failure_output.log")


def read_failure_output(value: Path | str | None) -> str | None:
    if value is None:
        return None
    raw = str(value)
    if raw == "-":
        output = sys.stdin.read()
    else:
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Failing-test output không tồn tại: {path}")
        try:
            output = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise RuntimeError(f"Không đọc được failing-test output {path}: {exc}") from exc
    if not output.strip():
        raise ValueError("Failing-test output không được rỗng")
    return output


def repair_project(
    settings: Settings, project, args, output_patch: Path | None,
    failing_tests: list[str] | tuple[str, ...] | None = None,
    *,
    external_baseline_output: str | None = None,
) -> dict:
    validator = ProjectValidator(
        command_timeout=args.command_timeout,
        jobs=args.jobs,
        environment_backend=settings.environment_backend,
        environment_runtime=getattr(settings, "environment_runtime", "auto"),
        environment_image=getattr(settings, "environment_image", ""),
    )
    pipeline = DebuggingPipeline(settings=settings, validator=validator)
    return pipeline.run(
        project,
        PipelineOptions(
            attempts=args.attempts,
            model=args.model,
            codex_timeout_seconds=args.codex_timeout,
            inherit_codex_config=args.inherit_codex_config,
            failing_tests=tuple(normalize_failing_tests(failing_tests or ())),
            external_baseline_output=external_baseline_output,
            request_config_path=getattr(args, "project_config", None),
            failure_output_path=getattr(args, "failing_output", None),
            request_config_sha256=getattr(args, "project_config_sha256", ""),
            failure_output_sha256=getattr(args, "failure_output_sha256", ""),
        ),
        output_patch=output_patch,
    )


def validate_patch(
    settings: Settings,
    project,
    patch_path: Path,
    timeout: int,
    jobs: int,
    failing_tests: tuple[str, ...] = (),
) -> int:
    patch_path = patch_path.expanduser().resolve()
    if not patch_path.is_file():
        raise FileNotFoundError(f"Patch không tồn tại: {patch_path}")
    diff = patch_path.read_text(encoding="utf-8")
    artifact_dir = settings.results_dir / safe_name(project.project_id, 100) / "manual-validation"
    DebuggingPipeline._require_outside_input(
        project.path.resolve(), artifact_dir.expanduser().resolve(), "results"
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(artifact_dir / "input.patch.diff", diff)
    with ProjectWorkspace(project, artifact_dir / "workspaces") as workspace:
        patch_paths = workspace.unified_diff_paths(diff)
        snapshot_hashes = workspace.snapshot_sha256s(patch_paths)
    validator = ProjectValidator(
        command_timeout=timeout,
        jobs=jobs,
        environment_backend=settings.environment_backend,
        environment_runtime=getattr(settings, "environment_runtime", "auto"),
        environment_image=getattr(settings, "environment_image", ""),
    )
    baseline = None
    if failing_tests:
        baseline = validator.baseline(
            project,
            artifact_dir / "baseline",
            failing_tests=failing_tests,
        )
        atomic_write_json(artifact_dir / "baseline.json", baseline)
        if baseline.get("status") != "failing" or not baseline.get("baseline_reproduced"):
            atomic_write_json(artifact_dir / "result.json", {
                "status": "invalid",
                "validation_error": baseline.get("validation_error") or "baseline_not_reproduced",
                "baseline": baseline,
            })
            print(json.dumps({"status": "invalid", "baseline": baseline}, indent=2, ensure_ascii=False))
            return 1
    result = validator.validate_diff(
        project=project,
        diff=diff,
        patch_paths=patch_paths,
        artifact_dir=artifact_dir / "validation",
        expected_sha256s=snapshot_hashes,
        failing_tests=failing_tests,
        expected_plan_digest=(baseline or {}).get("plan_digest", ""),
        expected_environment_digest=(baseline or {}).get("environment_digest", ""),
        expected_image_digest=(baseline or {}).get("provisioned_image_digest", ""),
    )
    if baseline is not None:
        result = classify_validation_result(baseline, result)
        result["baseline"] = baseline
    atomic_write_json(artifact_dir / "result.json", result)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("status") == "plausible" else 1


def doctor(settings: Settings, project, jobs: int) -> int:
    failures = 0
    prepared_environment_ready = True
    validator = ProjectValidator(
        jobs=jobs,
        environment_backend=settings.environment_backend,
        environment_runtime=getattr(settings, "environment_runtime", "auto"),
        environment_image=getattr(settings, "environment_image", ""),
    )
    if validator.environment_backend == "host":
        print("[OK] Environment mode: host (caller-prepared)")
    else:
        try:
            image_digest = validator.environment_runtime.inspect_image(
                validator.environment_image
            )
            print(
                f"[OK] Environment mode: image {validator.environment_image} "
                f"({image_digest})"
            )
        except RuntimeError as exc:
            failures += 1
            prepared_environment_ready = False
            print(f"[FAIL] {exc}")
    executable = shutil.which(settings.codex_executable)
    if executable:
        completed = subprocess.run(
            [executable, "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", timeout=20, check=False,
        )
        print(f"[OK] Codex CLI: {(completed.stdout or '').strip().splitlines()[-1]}")
    else:
        failures += 1
        print(f"[FAIL] Không tìm thấy Codex CLI: {settings.codex_executable}")
    context = CodeGraphBackend.from_settings(settings).probe()
    if context.mode == "off":
        print("[OK] CodeGraph context: disabled")
    elif context.ready:
        print(
            f"[OK] CodeGraph context: {context.version or 'unknown'} "
            f"({context.target or 'external'})"
        )
    elif context.mode == "required":
        failures += 1
        print(f"[FAIL] CodeGraph context: {context.error}")
    else:
        print(f"[WARN] CodeGraph context unavailable; Codex fallback active: {context.error}")
    try:
        plan = validator.inspect(project)
        environment = validator.resolve_environment(project, plan)
        print(f"[OK] Project: {project.path}")
        print(f"[OK] Build system: {plan.system}")
        print(f"[OK] Environment backend: {environment.backend}")
        print(f"[OK] Environment digest: {environment.digest}")
        image_backend = environment.backend == "image"
        for command in (
            *plan.setup,
            *plan.build,
            *plan.target_test,
            *plan.regression_test,
        ):
            if image_backend:
                state = "READY" if prepared_environment_ready else "BLOCKED"
                print(f"[{state}] {command.label}: image={environment.base_image}")
                continue
            binary = command.argv[0]
            if binary.startswith("./"):
                local_binary = project.path / binary
                exists = local_binary.is_file() and os.access(local_binary, os.X_OK)
            else:
                exists = shutil.which(binary)
            if exists:
                probe_error = _doctor_probe_error(command.argv)
                if probe_error:
                    failures += 1
                    print(f"[FAIL] {command.label}: {probe_error}")
                else:
                    print(f"[OK] {command.label}: {' '.join(command.argv)}")
            else:
                failures += 1
                print(f"[FAIL] Thiếu executable cho {command.label}: {binary}")
    except ValueError as exc:
        failures += 1
        print(f"[FAIL] Auto-detection: {exc}")
    print(f"[OK] Results directory: {settings.results_dir}")
    return 1 if failures else 0


def _doctor_probe_error(argv: tuple[str, ...]) -> str:
    if len(argv) >= 3 and argv[1:3] == ("-m", "pytest"):
        completed = subprocess.run(
            [argv[0], "-m", "pytest", "--version"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", timeout=30, check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stdout or "pytest không khả dụng").strip().splitlines()
            return detail[-1]
    return ""


def _print_plan(project: Path, plan: dict, environment: dict | None = None) -> None:
    print(f"project: {project}")
    print(f"build_system: {plan['system']}")
    if environment:
        print(f"environment_backend: {environment['backend']}")
        print(f"environment_digest: {environment['digest']}")
    for phase in ("setup", "build", "target_test", "regression_test"):
        for item in plan.get(phase, []):
            print(f"{phase}: (cwd={item['cwd']}) {' '.join(item['argv'])}")


if __name__ == "__main__":
    raise SystemExit(main())
