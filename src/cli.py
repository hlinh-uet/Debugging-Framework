from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from src.core.pipeline import (
    DebuggingPipeline,
    PipelineOptions,
    classify_validation_result,
)
from src.loaders.defects4c import Defects4CProjectResolver
from src.loaders.project import ProjectLoader
from src.utils.config import FrameworkConfig, Settings
from src.utils.jsonio import atomic_write_json, atomic_write_text, safe_name
from src.utils.workspace import ProjectWorkspace
from src.validation.project import ProjectValidator


def build_parser(config: FrameworkConfig | None = None) -> argparse.ArgumentParser:
    config = config or FrameworkConfig.load()
    parser = argparse.ArgumentParser(
        prog="debugging-framework",
        description="Nhận project, lưu raw unified patch và validation cách ly.",
    )
    parser.add_argument("--results-dir", type=Path, default=config.results_dir)
    parser.add_argument("--codex-bin", default=config.codex_executable)
    parser.add_argument(
        "--environment-backend",
        choices=("current", "local", "image", "container", "oci", "auto"),
        default=config.environment_backend,
        help="Prepared current env, prebuilt image, running container, OCI, or auto.",
    )
    parser.add_argument(
        "--environment-runtime", default=config.environment_runtime,
        help="OCI runtime executable (docker/podman) or auto.",
    )
    parser.add_argument(
        "--environment-container", default=config.environment_container,
        help="Running Docker container name; defaults to DEFECTS4C_CONTAINER or convention.",
    )
    parser.add_argument(
        "--environment-image", default=config.environment_image,
        help="Prebuilt OCI image name/digest used with backend image.",
    )
    add_defects4c_options(parser, config=config)
    sub = parser.add_subparsers(dest="command", required=True)

    inspect_parser = sub.add_parser(
        "inspect", help="Tự nhận diện provisioning/build/test plan, chưa chạy lệnh."
    )
    inspect_parser.add_argument(
        "project", type=Path, nargs="?",
        help="Project root; optional with --libyang/--fmt/--defects4c.",
    )
    inspect_parser.add_argument("--json", action="store_true")
    add_environment_options(inspect_parser)

    repair_parser = sub.add_parser(
        "repair",
        help="FL/APR trực tiếp trên project hiện tại từ failure evidence đã cung cấp.",
    )
    repair_parser.add_argument(
        "project", type=Path, nargs="?",
        help="Project root; mặc định là thư mục hiện tại.",
    )
    repair_parser.add_argument(
        "--failing-test", "--test-id", dest="failing_tests", action="append",
        help="Tên/ID test đã fail; có thể lặp option.",
    )
    repair_parser.add_argument(
        "--failing-output", "--failure-output", "--baseline-output",
        dest="failing_output", type=Path, required=True,
        help="Actual failing-test output; '-' đọc stdin. Không chạy lại baseline.",
    )
    repair_parser.add_argument("--output", type=Path, help="File patch output.")
    add_environment_options(repair_parser)
    add_repair_options(repair_parser, config, workspace_default="current")

    validate_parser = sub.add_parser("validate", help="Build/test lại một unified diff trên project.")
    validate_parser.add_argument("project", type=Path)
    validate_parser.add_argument("patch", type=Path)
    validate_parser.add_argument(
        "--failing-test", dest="failing_tests", action="append",
        help="Target failing test(s) to reproduce and validate; may be repeated.",
    )
    validate_parser.add_argument("--command-timeout", type=int, default=config.command_timeout_seconds)
    validate_parser.add_argument("--jobs", type=int, default=config.jobs)
    add_environment_options(validate_parser)

    doctor_parser = sub.add_parser(
        "doctor", help="Kiểm tra Codex và provisioning/build/test auto-detection."
    )
    doctor_parser.add_argument(
        "project", type=Path, nargs="?",
        help="Project root; optional with --libyang/--fmt/--defects4c.",
    )
    doctor_parser.add_argument("--jobs", type=int, default=config.jobs)
    add_environment_options(doctor_parser)
    add_defects4c_options(inspect_parser, subcommand=True)
    add_defects4c_options(repair_parser, subcommand=True)
    add_defects4c_options(doctor_parser, subcommand=True)
    return parser


def add_environment_options(parser: argparse.ArgumentParser) -> None:
    # SUPPRESS lets a value written after the subcommand override the global
    # option without replacing the global default when it is omitted.
    parser.add_argument(
        "--environment-backend",
        choices=("current", "local", "image", "container", "oci", "auto"),
        default=argparse.SUPPRESS,
        help="Prepared current env, prebuilt image, running container, OCI, or auto.",
    )
    parser.add_argument(
        "--environment-runtime", default=argparse.SUPPRESS,
        help="OCI runtime executable (docker/podman) or auto.",
    )
    parser.add_argument(
        "--environment-container", default=argparse.SUPPRESS,
        help="Running Docker container name; defaults to DEFECTS4C_CONTAINER or convention.",
    )
    parser.add_argument(
        "--environment-image", default=argparse.SUPPRESS,
        help="Prebuilt OCI image name/digest used with backend image.",
    )


def add_defects4c_options(
    parser: argparse.ArgumentParser,
    *,
    config: FrameworkConfig | None = None,
    subcommand: bool = False,
) -> None:
    default = argparse.SUPPRESS
    parser.add_argument(
        "--defects4c", dest="defects4c_alias", default=default,
        help="Defects4C recipe alias, e.g. libyang or fmt.",
    )
    parser.add_argument(
        "--libyang", dest="defects4c_alias", action="store_const", const="libyang",
        default=default, help="Shortcut for --defects4c libyang.",
    )
    parser.add_argument(
        "--fmt", dest="defects4c_alias", action="store_const", const="fmt",
        default=default, help="Shortcut for --defects4c fmt.",
    )
    parser.add_argument(
        "--bug-id", dest="bug_id", default=default,
        help="Defects4C bug id/commit prefix when using a dataset alias.",
    )
    parser.add_argument(
        "--defects4c-root", dest="defects4c_root", type=Path,
        default=(config.defects4c_root if config is not None and not subcommand else default),
        help="Defects4C repository root (default: sibling ../defects4c).",
    )


def add_repair_options(
    parser: argparse.ArgumentParser,
    config: FrameworkConfig,
    *,
    workspace_default: str | None = None,
) -> None:
    parser.add_argument("--attempts", type=int, default=config.attempts)
    parser.add_argument("--model", default=config.model)
    parser.add_argument("--codex-timeout", type=int, default=config.codex_timeout_seconds)
    parser.add_argument("--command-timeout", type=int, default=config.command_timeout_seconds)
    parser.add_argument("--jobs", type=int, default=config.jobs)
    parser.add_argument(
        "--inherit-codex-config", action=argparse.BooleanOptionalAction,
        default=config.inherit_codex_config,
    )
    parser.add_argument(
        "--codex-workspace", choices=("auto", "snapshot", "current"),
        default=workspace_default or config.codex_workspace,
        help="Codex workspace; repair mặc định sửa trực tiếp project hiện tại.",
    )


def _option_supplied(argv: list[str], name: str) -> bool:
    return any(item == name or item.startswith(name + "=") for item in argv)


def _prepare_defects4c_input(
    args: argparse.Namespace,
    config: FrameworkConfig,
    raw_argv: list[str],
) -> None:
    if args.command not in {"repair", "inspect", "doctor"}:
        return
    alias = str(getattr(args, "defects4c_alias", "") or "").strip()
    project_path = getattr(args, "project", None)
    if not alias and project_path is None and args.command in {"repair", "inspect", "doctor"}:
        args.project = Path.cwd()
        return
    if not alias and project_path is None:
        raise ValueError(
            "Cần project path hoặc alias Defects4C (--libyang, --fmt, --defects4c <name>)"
        )
    if not alias:
        return
    root = getattr(args, "defects4c_root", None) or config.defects4c_root
    selection = Defects4CProjectResolver(root).resolve(
        alias,
        project_path=project_path,
        bug_id=str(getattr(args, "bug_id", "") or ""),
    )
    args.project = selection.project.path
    args.defects4c_selection = selection
    if args.command == "repair" and not normalize_failing_tests(
        getattr(args, "failing_tests", None)
    ):
        args.failing_tests = list(selection.failing_tests)
    # Dataset aliases imply the dataset's already-provisioned container.  An
    # explicit CLI override still wins over the alias default.
    if not _option_supplied(raw_argv, "--environment-container"):
        args.environment_container = selection.recipe.container
    if not _option_supplied(raw_argv, "--environment-backend"):
        args.environment_backend = "container"


def main(argv: list[str] | None = None) -> int:
    try:
        config = FrameworkConfig.load()
        raw_argv = list(argv if argv is not None else sys.argv[1:])
        args = build_parser(config).parse_args(raw_argv)
        _prepare_defects4c_input(args, config, raw_argv)
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
            environment_container=args.environment_container,
            environment_image=args.environment_image,
            defects4c_root=getattr(args, "defects4c_root", config.defects4c_root),
        ).validated()
        project = ProjectLoader().load(args.project)

        if args.command == "inspect":
            validator = ProjectValidator(
                environment_backend=getattr(settings, "environment_backend", "auto"),
                environment_runtime=getattr(settings, "environment_runtime", "auto"),
                environment_container=getattr(settings, "environment_container", ""),
                environment_image=getattr(settings, "environment_image", ""),
                prepared_environment=getattr(settings, "environment_backend", "") in {
                    "current", "image",
                },
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
                "llm_patch_artifact": result.get("llm_patch_artifact", ""),
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
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    return 2


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
        raise ValueError("cần ít nhất một --failing-test")
    if args.failing_output is None:
        raise ValueError("repair cần --failure-output")


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
        environment_backend=getattr(settings, "environment_backend", "auto"),
        environment_runtime=getattr(settings, "environment_runtime", "auto"),
        environment_container=getattr(settings, "environment_container", ""),
        environment_image=getattr(settings, "environment_image", ""),
        prepared_environment=True,
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
            workspace_mode=getattr(args, "codex_workspace", "auto"),
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
        environment_backend=getattr(settings, "environment_backend", "auto"),
        environment_runtime=getattr(settings, "environment_runtime", "auto"),
        environment_container=getattr(settings, "environment_container", ""),
        environment_image=getattr(settings, "environment_image", ""),
        prepared_environment=True,
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
    validator = ProjectValidator(
        jobs=jobs,
        environment_backend=getattr(settings, "environment_backend", "auto"),
        environment_runtime=getattr(settings, "environment_runtime", "auto"),
        environment_container=getattr(settings, "environment_container", ""),
        environment_image=getattr(settings, "environment_image", ""),
        prepared_environment=True,
    )
    if validator.environment_backend == "container":
        container = validator.environment_container.resolve_container(project.project_id)
        if container:
            print(f"[OK] Running environment container: {container}")
        else:
            failures += 1
            print(
                "[FAIL] Không tìm thấy container Defects4C đang chạy; "
                "đặt DEFECTS4C_CONTAINER hoặc khởi my_defects4c_<project>"
            )
    elif validator.environment_backend == "image":
        if validator.environment_runtime.available:
            print(
                f"[OK] Prebuilt OCI image: {validator.environment_image} "
                f"(runtime={validator.environment_runtime.runtime})"
            )
        else:
            failures += 1
            print("[FAIL] OCI runtime không khả dụng cho prebuilt image")
    elif validator.environment_backend == "current":
        print("[OK] Dùng current environment do caller chuẩn bị")
    elif validator.environment_backend == "auto" and validator.environment_container.resolve_container(
        project.project_id
    ):
        print(
            f"[OK] Running environment container: "
            f"{validator.environment_container.resolve_container(project.project_id)}"
        )
    elif validator._requires_bwrap():
        sandbox = shutil.which("bwrap")
        if sandbox:
            print(f"[OK] Validation filesystem sandbox: {sandbox}")
        else:
            failures += 1
            print("[FAIL] Thiếu Bubblewrap (bwrap); validation sẽ fail-closed")
    elif validator.environment_runtime.available:
        print(f"[OK] OCI runtime: {validator.environment_runtime.runtime}")
    else:
        failures += 1
        print("[FAIL] OCI runtime không khả dụng (docker/podman)")
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
    try:
        plan = validator.inspect(project)
        environment = validator.resolve_environment(project, plan)
        print(f"[OK] Project: {project.path}")
        print(f"[OK] Build system: {plan.system}")
        print(f"[OK] Environment backend: {environment.backend}")
        print(f"[OK] Environment digest: {environment.digest}")
        oci_backend = environment.backend in {"image", "oci", "container"}
        for command in (
            *plan.setup,
            *plan.build,
            *plan.target_test,
            *plan.regression_test,
            *plan.test,
        ):
            if oci_backend:
                print(f"[AUTO] {command.label}: sẽ chạy trong {environment.base_image}")
                continue
            binary = command.argv[0]
            if binary.startswith("./"):
                local_binary = project.path / binary
                exists = local_binary.is_file() and os.access(local_binary, os.X_OK)
            else:
                exists = shutil.which(binary)
            generated_by_setup = (
                bool(plan.setup)
                and not Path(binary).is_absolute()
                and ".debugging-framework" in Path(binary).parts
            )
            if exists:
                probe_error = _doctor_probe_error(command.argv)
                if probe_error:
                    failures += 1
                    print(f"[FAIL] {command.label}: {probe_error}")
                else:
                    print(f"[OK] {command.label}: {' '.join(command.argv)}")
            elif generated_by_setup:
                print(
                    f"[AUTO] {command.label}: {binary} sẽ được tạo bởi provisioning"
                )
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
    for phase in ("setup", "build", "target_test", "regression_test", "test"):
        for item in plan.get(phase, []):
            print(f"{phase}: (cwd={item['cwd']}) {' '.join(item['argv'])}")


if __name__ == "__main__":
    raise SystemExit(main())
