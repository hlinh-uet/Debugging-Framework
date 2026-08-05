from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from src.core.pipeline import DebuggingPipeline, PipelineOptions
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
    sub = parser.add_subparsers(dest="command", required=True)

    inspect_parser = sub.add_parser("inspect", help="Tự nhận diện build/test plan, chưa chạy lệnh.")
    inspect_parser.add_argument("project", type=Path)
    inspect_parser.add_argument("--json", action="store_true")

    run_parser = sub.add_parser("run", help="Project -> raw patch + validation result.")
    run_parser.add_argument("project", type=Path, help="Đường dẫn trực tiếp tới project root.")
    run_parser.add_argument("--output", type=Path, help="File patch output.")
    add_run_options(run_parser, config)

    batch_parser = sub.add_parser(
        "run-batch", help="Chạy tuần tự mọi project path trong materialization manifest."
    )
    batch_parser.add_argument("manifest", type=Path)
    batch_parser.add_argument(
        "--output-dir", type=Path,
        help="Thư mục patch output; mặc định dùng results/<project>/patch.diff.",
    )
    add_run_options(batch_parser, config)

    validate_parser = sub.add_parser("validate", help="Build/test lại một unified diff trên project.")
    validate_parser.add_argument("project", type=Path)
    validate_parser.add_argument("patch", type=Path)
    validate_parser.add_argument("--command-timeout", type=int, default=config.command_timeout_seconds)
    validate_parser.add_argument("--jobs", type=int, default=config.jobs)

    doctor_parser = sub.add_parser("doctor", help="Kiểm tra Codex và build/test auto-detection.")
    doctor_parser.add_argument("project", type=Path)
    doctor_parser.add_argument("--jobs", type=int, default=config.jobs)
    return parser


def add_run_options(parser: argparse.ArgumentParser, config: FrameworkConfig) -> None:
    parser.add_argument("--attempts", type=int, default=config.attempts)
    parser.add_argument("--model", default=config.model)
    parser.add_argument("--codex-timeout", type=int, default=config.codex_timeout_seconds)
    parser.add_argument("--command-timeout", type=int, default=config.command_timeout_seconds)
    parser.add_argument("--jobs", type=int, default=config.jobs)
    parser.add_argument(
        "--allow-clean-project", action="store_true",
        help="Cho phép đề xuất patch dù baseline test đã pass (không khuyến nghị).",
    )
    parser.add_argument(
        "--inherit-codex-config", action=argparse.BooleanOptionalAction,
        default=config.inherit_codex_config,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        config = FrameworkConfig.load()
        args = build_parser(config).parse_args(argv)
        settings = Settings(
            results_dir=args.results_dir,
            codex_executable=args.codex_bin,
            codex_api_key=config.codex_api_key,
            codex_provider=config.codex_provider,
            codex_base_url=config.codex_base_url,
            codex_wire_api=config.codex_wire_api,
            codex_env_key=config.codex_env_key,
        ).validated()
        if args.command == "run-batch":
            return run_batch(settings, config, args)

        project = ProjectLoader().load(args.project)

        if args.command == "inspect":
            plan = ProjectValidator().inspect(project)
            if args.json:
                print(json.dumps(plan.as_dict(), indent=2, ensure_ascii=False))
            else:
                _print_plan(project.path, plan.as_dict())
            return 0

        if args.command == "doctor":
            return doctor(settings, project, args.jobs)

        if args.command == "run":
            validate_run_arguments(config, settings, args)
            result = run_one_project(settings, project, args, args.output)
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
            return validate_patch(settings, project, args.patch, args.command_timeout, args.jobs)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    return 2


def validate_run_arguments(config: FrameworkConfig, settings: Settings, args) -> None:
    if config.require_api_key and not settings.codex_api_key:
        raise ValueError("CODEX_API_KEY chưa được cấu hình")
    if args.attempts < 1 or args.codex_timeout < 1 or args.command_timeout < 1:
        raise ValueError("attempts/timeout phải >= 1")
    if args.jobs < 0:
        raise ValueError("--jobs phải >= 0")


def run_one_project(settings: Settings, project, args, output_patch: Path | None) -> dict:
    validator = ProjectValidator(command_timeout=args.command_timeout, jobs=args.jobs)
    pipeline = DebuggingPipeline(settings=settings, validator=validator)
    return pipeline.run(
        project,
        PipelineOptions(
            attempts=args.attempts,
            model=args.model,
            codex_timeout_seconds=args.codex_timeout,
            allow_clean_project=args.allow_clean_project,
            inherit_codex_config=args.inherit_codex_config,
        ),
        output_patch=output_patch,
    )


def run_batch(settings: Settings, config: FrameworkConfig, args) -> int:
    validate_run_arguments(config, settings, args)
    records = load_batch_manifest(args.manifest)
    loader = ProjectLoader()
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else None
    results = []
    for index, record in enumerate(records, start=1):
        project = loader.load(Path(record["project_path"]))
        print(f"[batch {index}/{len(records)}] {project.path}")
        output = output_dir / f"{safe_name(project.project_id, 180)}.patch" if output_dir else None
        result = run_one_project(settings, project, args, output)
        results.append(
            {
                "project": str(project.path),
                "bug_id": record.get("bug_id", ""),
                "status": result.get("status"),
                "output_patch": result.get("output_patch", ""),
                "patch_validation_passed": result.get("patch_validation_passed", False),
                "llm_patch_artifact": result.get("llm_patch_artifact", ""),
                "validation_error": result.get("validation_error", ""),
            }
        )
    summary = {
        "manifest": str(args.manifest.expanduser().resolve()),
        "project_count": len(results),
        "plausible_count": sum(item["status"] == "plausible" for item in results),
        "results": results,
    }
    settings.results_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(settings.results_dir / "batch_result.json", summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if summary["plausible_count"] == len(results) else 1


def load_batch_manifest(path: Path) -> list[dict]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Batch manifest không tồn tại: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Batch manifest không hợp lệ {path}: {exc}") from exc
    records = payload.get("projects") if isinstance(payload, dict) else None
    if not isinstance(records, list) or not records:
        raise ValueError(f"Batch manifest không có projects: {path}")
    normalized = []
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict) or not str(record.get("project_path") or "").strip():
            raise ValueError(f"Batch manifest projects[{index}] thiếu project_path")
        project_path = Path(str(record["project_path"])).expanduser()
        if not project_path.is_absolute():
            project_path = path.parent / project_path
        normalized.append({**record, "project_path": str(project_path.resolve())})
    return normalized


def validate_patch(settings: Settings, project, patch_path: Path, timeout: int, jobs: int) -> int:
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
        baseline_hashes = workspace.baseline_sha256s(patch_paths)
    validator = ProjectValidator(command_timeout=timeout, jobs=jobs)
    baseline = validator.baseline(project, artifact_dir / "baseline")
    if baseline.get("status") == "invalid":
        result = {"status": "invalid", "baseline": baseline, "validation_error": "baseline_invalid"}
    else:
        result = validator.validate_diff(
            project=project,
            diff=diff,
            patch_paths=patch_paths,
            artifact_dir=artifact_dir / "patched-validation",
            expected_sha256s=baseline_hashes,
        )
        result["baseline"] = baseline
    atomic_write_json(artifact_dir / "result.json", result)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("status") == "plausible" else 1


def doctor(settings: Settings, project, jobs: int) -> int:
    failures = 0
    sandbox = shutil.which("bwrap")
    if sandbox:
        print(f"[OK] Validation filesystem sandbox: {sandbox}")
    else:
        failures += 1
        print("[FAIL] Thiếu Bubblewrap (bwrap); validation sẽ fail-closed")
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
        plan = ProjectValidator(jobs=jobs).inspect(project)
        print(f"[OK] Project: {project.path}")
        print(f"[OK] Build system: {plan.system}")
        for command in (*plan.setup, *plan.build, *plan.test):
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


def _print_plan(project: Path, plan: dict) -> None:
    print(f"project: {project}")
    print(f"build_system: {plan['system']}")
    for phase in ("setup", "build", "test"):
        for item in plan[phase]:
            print(f"{phase}: (cwd={item['cwd']}) {' '.join(item['argv'])}")


if __name__ == "__main__":
    raise SystemExit(main())
