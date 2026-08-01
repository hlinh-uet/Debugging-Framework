from __future__ import annotations

import sys
from pathlib import Path


class UnifiedRuntime:
    """Load and expose modules from the current Unified-Debugging project.

    Functional adapters live in ``loaders/``, ``validation/``, and
    ``evaluation/``. This object only owns import-path setup and lazy imports.
    """

    def __init__(self, unified_root: Path):
        self.root = unified_root.resolve()
        root_text = str(self.root)
        if root_text not in sys.path:
            sys.path.insert(0, root_text)
        self._imports_ready = False

    def ensure_imports(self) -> None:
        if self._imports_ready:
            return
        try:
            from data_loaders.base_loader import get_loader
            from core.apr.artifacts import (
                build_initial_test_snapshot,
                build_invalid_snapshot,
                build_validation_snapshot,
            )
            from core.apr.validation import validate_patch
            from core.apr.common import source_language_from_path
            from core.utils import extract_function_code
            from evaluation.eval_apr import evaluate_apr
            from evaluation.eval_fl import evaluate_fl
        except Exception as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "Không import được Unified-Debugging. Hãy chạy bằng môi trường "
                "Python đã cài ../Unified-Debugging/requirements.txt. "
                f"Chi tiết: {exc}"
            ) from exc

        self.get_loader = get_loader
        self.build_initial_test_snapshot = build_initial_test_snapshot
        self.build_invalid_snapshot = build_invalid_snapshot
        self.build_validation_snapshot = build_validation_snapshot
        self.validate_patch = validate_patch
        self.source_language_from_path = source_language_from_path
        self.extract_function_code = extract_function_code
        self.evaluate_apr_impl = evaluate_apr
        self.evaluate_fl_impl = evaluate_fl
        self._imports_ready = True
