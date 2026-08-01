from __future__ import annotations

import os
from pathlib import Path

from src.utils.unified_runtime import UnifiedRuntime


class UnifiedValidator:
    """Delegate snapshots and patch validation to Unified-Debugging."""

    def __init__(self, runtime: UnifiedRuntime):
        self.runtime = runtime

    def initial_snapshot(self, tests: list, exclude_fixed_fail_tests: bool) -> dict:
        self.runtime.ensure_imports()
        return self.runtime.build_initial_test_snapshot(
            tests,
            exclude_fixed_fail_tests=exclude_fixed_fail_tests,
        )

    def invalid_snapshot(
        self, initial: dict, error: str, exclude_fixed_fail_tests: bool
    ) -> dict:
        self.runtime.ensure_imports()
        return self.runtime.build_invalid_snapshot(
            initial,
            validation_error=error,
            exclude_fixed_fail_tests=exclude_fixed_fail_tests,
        )

    def validate(
        self,
        *,
        dataset: str,
        bug_id: str,
        patched_file: Path,
        source_relpath: str,
        initial: dict,
        exclude_fixed_fail_tests: bool,
    ) -> dict:
        self.runtime.ensure_imports()
        _is_valid, passed, failed = self.runtime.validate_patch(
            str(patched_file),
            bug_id,
            dataset,
            src_basename=os.path.basename(source_relpath),
            src_relpath=source_relpath,
            exclude_fixed_fail_tests=exclude_fixed_fail_tests,
        )
        details = dict(
            getattr(self.runtime.validate_patch, "last_details", {}) or {}
        )
        return self.runtime.build_validation_snapshot(
            initial,
            validation_details=details,
            post_passed=passed,
            post_failed=failed,
            validation_error=str(details.get("validation_error") or ""),
            exclude_fixed_fail_tests=exclude_fixed_fail_tests,
        )

    def extract_function(self, source: str, function: str, source_path: str) -> str:
        self.runtime.ensure_imports()
        if not function:
            return ""
        code, _start, _end = self.runtime.extract_function_code(
            source,
            function,
            language=self.runtime.source_language_from_path(source_path),
        )
        return code or ""

    def docker_status(self, dataset: str) -> tuple[bool, str]:
        self.runtime.ensure_imports()
        try:
            from data_loaders.sandbox_adapter import defects4c_docker_ready

            return defects4c_docker_ready(dataset)
        except Exception as exc:
            return False, str(exc)

