from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from src.core.pipeline import DebuggingPipeline, PipelineOptions
from src.evaluation.unified import UnifiedEvaluator
from src.loaders.defects4c import Defects4CLoader
from src.utils.config import PROJECT_ROOT, Settings
from src.utils.unified_runtime import UnifiedRuntime
from src.validation.unified import UnifiedValidator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="debugging-framework",
        description=(
            "Defects4C FL+APR bằng Codex CLI, dùng loader/validation/evaluation "
            "của Unified-Debugging."
        ),
    )
    parser.add_argument(
        "--unified-root",
        type=Path,
        default=PROJECT_ROOT.parent / "Unified-Debugging",
        help="Đường dẫn dự án Unified-Debugging hiện tại.",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=PROJECT_ROOT / "experiments",
        help="Thư mục artifact/kết quả của framework.",
    )
    parser.add_argument(
        "--codex-bin", default="codex", help="Codex CLI executable (mặc định: codex)."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="Đọc và liệt kê bug qua loader chuẩn.")
    add_dataset_args(list_parser)
    list_parser.add_argument("--json", action="store_true", help="In JSON thay vì bảng ngắn.")

    run_parser = subparsers.add_parser("run", help="Chạy Codex FL+APR rồi validation/evaluation.")
    add_dataset_args(run_parser)
    run_parser.add_argument("--attempts", type=int, default=2, help="Số attempt tối đa/bug.")
    run_parser.add_argument("--model", help="Model override truyền cho `codex exec --model`.")
    run_parser.add_argument(
        "--timeout", type=int, default=1800, help="Timeout giây cho mỗi Codex attempt."
    )
    run_parser.add_argument(
        "--include-fixed-fail-tests",
        action="store_true",
        help="Giữ cả test FAIL trên buggy và fixed version.",
    )
    run_parser.add_argument(
        "--only-missing", action="store_true", help="Bỏ qua bug đã có trong apr_results.json."
    )
    run_parser.add_argument(
        "--no-eval", action="store_true", help="Không tự chạy evaluation sau pipeline."
    )
    run_parser.add_argument(
        "--inherit-codex-config",
        action="store_true",
        help="Cho Codex worker nạp config cá nhân; mặc định dùng automation sạch.",
    )

    validate_parser = subparsers.add_parser(
        "validate", help="Validate lại patched artifact mà không gọi Codex."
    )
    add_dataset_args(validate_parser)
    validate_parser.add_argument(
        "--include-fixed-fail-tests", action="store_true"
    )

    evaluate_parser = subparsers.add_parser(
        "evaluate", help="Chạy evaluator FL+APR chuẩn của Unified-Debugging."
    )
    add_dataset_args(evaluate_parser, include_bug_id=False)

    doctor_parser = subparsers.add_parser(
        "doctor", help="Kiểm tra Codex CLI, imports và Docker validation."
    )
    doctor_parser.add_argument("--dataset", default="fmt")
    return parser


def add_dataset_args(parser: argparse.ArgumentParser, include_bug_id: bool = True) -> None:
    parser.add_argument(
        "--dataset",
        default="fmt",
        help="Defects4C metadata folder, ví dụ fmt/libyang/php/tcpdump.",
    )
    if include_bug_id:
        parser.add_argument(
            "--bug-id",
            action="append",
            default=[],
            help="Giới hạn bug; có thể lặp option này.",
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings(
            unified_root=args.unified_root,
            results_dir=args.results_dir,
            codex_executable=args.codex_bin,
        ).validated()
        runtime = UnifiedRuntime(settings.unified_root)
        loader = Defects4CLoader(runtime)
        validator = UnifiedValidator(runtime)
        evaluator = UnifiedEvaluator(runtime)
        pipeline = DebuggingPipeline(
            settings=settings,
            loader=loader,
            validator=validator,
            evaluator=evaluator,
        )

        if args.command == "list":
            bugs = loader.load_bugs(args.dataset, args.bug_id)
            if args.json:
                print(
                    json.dumps(
                        [
                            {
                                "bug_id": bug.bug_id,
                                "dataset": bug.dataset,
                                "source_file": bug.source_file,
                                "test_count": len(bug.tests or []),
                                "failed_test_count": sum(
                                    1
                                    for test in bug.tests or []
                                    if str(test.get("outcome") or "").upper()
                                    in {"FAIL", "FAILED"}
                                ),
                            }
                            for bug in bugs
                        ],
                        indent=2,
                        ensure_ascii=False,
                    )
                )
            else:
                for bug in bugs:
                    failed = sum(
                        1
                        for test in bug.tests or []
                        if str(test.get("outcome") or "").upper() in {"FAIL", "FAILED"}
                    )
                    print(
                        f"{bug.bug_id}\ttests={len(bug.tests or [])}\t"
                        f"failed={failed}\tsource={bug.source_file}"
                    )
            return 0

        if args.command == "run":
            if args.attempts < 1:
                raise ValueError("--attempts phải >= 1")
            if args.timeout < 1:
                raise ValueError("--timeout phải >= 1")
            pipeline.run(
                PipelineOptions(
                    dataset=args.dataset,
                    attempts=args.attempts,
                    model=args.model,
                    timeout_seconds=args.timeout,
                    exclude_fixed_fail_tests=not args.include_fixed_fail_tests,
                    evaluate_after_run=not args.no_eval,
                    only_missing=args.only_missing,
                    inherit_codex_config=args.inherit_codex_config,
                ),
                bug_ids=args.bug_id,
            )
            return 0

        if args.command == "validate":
            pipeline.revalidate(
                args.dataset,
                args.bug_id,
                exclude_fixed_fail_tests=not args.include_fixed_fail_tests,
            )
            return 0

        if args.command == "evaluate":
            print(pipeline.evaluate(args.dataset), end="")
            return 0

        if args.command == "doctor":
            return doctor(settings, runtime, validator, args.dataset)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    return 2


def doctor(
    settings: Settings,
    runtime: UnifiedRuntime,
    validator: UnifiedValidator,
    dataset: str,
) -> int:
    failures = 0
    executable = shutil.which(settings.codex_executable)
    if executable:
        completed = subprocess.run(
            [executable, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
        version = (completed.stdout or "").strip().splitlines()[-1]
        print(f"[OK] Codex CLI: {version} ({executable})")
    else:
        failures += 1
        print(f"[FAIL] Không tìm thấy Codex CLI: {settings.codex_executable}")

    try:
        runtime.ensure_imports()
        print(f"[OK] Unified-Debugging imports: {settings.unified_root}")
    except RuntimeError as exc:
        failures += 1
        print(f"[FAIL] {exc}")

    ready, detail = validator.docker_status(dataset)
    if ready:
        print(f"[OK] Defects4C validation container: {detail}")
    else:
        failures += 1
        print(f"[FAIL] {detail}")
    print(f"[OK] Results directory: {settings.results_dir}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
