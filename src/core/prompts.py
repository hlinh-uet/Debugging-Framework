from __future__ import annotations

import json
from typing import Any, Optional


def _clip(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def build_codex_prompt(
    *,
    project_id: str,
    attempt: int,
    baseline: dict,
    previous_attempt: Optional[dict] = None,
) -> str:
    baseline_context = {
        "status": baseline.get("status"),
        "build_system": baseline.get("build_system"),
        "build_plan": baseline.get("build_plan"),
        "failed_tests": baseline.get("post_failed_tests", []),
        "failure_output": _clip(baseline.get("failure_output", ""), 16_000),
    }
    feedback = ""
    if previous_attempt:
        feedback = (
            "\nPrevious attempt feedback (fix the cause; do not repeat the failed patch):\n"
            + _clip(json.dumps(previous_attempt, ensure_ascii=False, indent=2), 12_000)
        )

    return f"""You are a software engineer performing fault localization and program repair.
This is a disposable, writable snapshot of the input project. The framework has already
run the project's own build and tests. You may edit files and run commands here. The
framework will extract your patch and independently apply it to a fresh validation copy.

Investigate the repository structure, production source, tests, build files, callers,
callees, data flow, error paths, and invariants. Use the failing output below as evidence.

Constraints:
- Do not use internet, git history, hidden accepted fixes, ground truth, or external artifacts.
- You may change one or more repository files needed for the repair and may run the
  project's build/tests. Do not install dependencies or write outside this workspace.
- Do not weaken or delete tests merely to make validation pass. Do not include generated
  build output, caches, dependency/vendor trees, or credentials in the repair.
- Make the smallest root-cause fix that preserves public APIs and project style.
- `repair.paths` must list every intentionally changed repository-relative file.
- `repair.diff` must be a raw unified/Git diff beginning with `diff --git a/...`
  or `--- a/<path>`, contain all intentional changes, and have no Markdown fence.
- Return only valid JSON matching the supplied schema. Do not claim that tests passed.

Fault-localization entries must contain path, function/symbol, score in [0,1], and
concrete repository evidence. Include every repaired source location among the high scores.

Run context:
{json.dumps({"project_id": project_id, "attempt": attempt}, ensure_ascii=False, indent=2)}

Baseline validation:
{json.dumps(baseline_context, ensure_ascii=False, indent=2)}
{feedback}

Inspect the project, implement and test the repair in this temporary workspace, review
the final diff, then return JSON with exactly `summary`, `fault_localization`, and `repair`.
"""
