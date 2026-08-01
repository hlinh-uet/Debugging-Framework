from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

from src.utils.unified_runtime import UnifiedRuntime


class UnifiedEvaluator:
    """Run the unchanged FL/APR evaluators from Unified-Debugging."""

    def __init__(self, runtime: UnifiedRuntime):
        self.runtime = runtime

    def evaluate(self, dataset: str, results_dir: Path) -> str:
        self.runtime.ensure_imports()
        output = io.StringIO()
        with redirect_stdout(output):
            self.runtime.evaluate_fl_impl(
                dataset=dataset,
                level="combined",
                results_dir=str(results_dir),
            )
            self.runtime.evaluate_apr_impl(
                dataset=dataset,
                results_filename="apr_results.json",
                label="Codex CLI APR",
                results_dir=str(results_dir),
            )
        report = output.getvalue()
        results_dir.mkdir(parents=True, exist_ok=True)
        (results_dir / "evaluation.txt").write_text(report, encoding="utf-8")
        return report

