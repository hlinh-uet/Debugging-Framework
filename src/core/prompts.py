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
    failing_tests: list[str] | tuple[str, ...] | None = None,
    previous_attempt: Optional[dict] = None,
    baseline: Optional[dict] = None,
) -> str:
    failing_tests = tuple(
        str(value).strip() for value in (failing_tests or ()) if str(value).strip()
    )
    known_failure = (
        (
            (
                (
                    "The caller supplied the output for these failing test(s). Read the complete "
                    if baseline and baseline.get("baseline_external")
                    else "The framework has already run these failing test(s). Read the complete "
                )
                + "baseline output file named below and use it as the primary FL/APR signal; "
                + (
                    "do not rerun them:\n"
                    if baseline and baseline.get("baseline_external")
                    else "do not rerun them unless a focused rerun is necessary:\n"
                )
            )
            if baseline
            else
            "The caller has identified these failing test(s). Start by running and "
            "investigating them; use their failure output as the primary FL/APR signal:\n"
        )
        + "\n".join(f"- {test}" for test in failing_tests)
        if failing_tests
        else "No failing test name was supplied; discover the failing test from the project."
    )
    feedback = ""
    if previous_attempt:
        feedback = (
            "\nPrevious attempt feedback (fix the cause; do not repeat the failed patch):\n"
            + _clip(json.dumps(previous_attempt, ensure_ascii=False, indent=2), 12_000)
        )
    baseline_context = ""
    if baseline:
        source_intro = (
            "The caller supplied the failing-test output before this attempt. Treat it "
            "as authoritative baseline evidence; do not rerun the test."
            if baseline.get("baseline_external")
            else
            "The framework used the explicitly selected prepared environment and reproduced the "
            "requested failure before this attempt. Treat this as authoritative baseline "
            "evidence;"
        )
        output_description = (
            "complete supplied failing-test output"
            if baseline.get("baseline_external")
            else "complete raw setup/build/test output"
        )
        baseline_context = (
            "\n" + source_intro + " Do not change the validation contract or spend time guessing a "
            f"different environment. The {output_description} is available "
            "in `.debugging-framework/baseline-output.txt`; read that file before making "
            "diagnostic assumptions. A concise failure excerpt and its audit artifact are "
            "included below.\n"
            + _clip(json.dumps({
                "target_tests": baseline.get("target_tests", failing_tests),
                "failed_test_ids": baseline.get("failed_test_ids", []),
                # Keep the prompt bounded; the complete output is available in
                # the workspace file named immediately above.
                "failure_output": _clip(baseline.get("failure_output", ""), 12_000),
                "baseline_output_file": baseline.get(
                    "codex_snapshot_output_file",
                    ".debugging-framework/baseline-output.txt",
                ),
                "baseline_output_artifact": baseline.get("baseline_output_artifact", ""),
                "build_system": baseline.get("build_system", ""),
                "target_commands": baseline.get("target_commands", []),
                "environment": baseline.get("environment", {}),
                "plan_digest": baseline.get("plan_digest", ""),
                "environment_digest": baseline.get("environment_digest", ""),
            }, ensure_ascii=False, indent=2), 24_000)
        )
    workflow_context = (
        (
            "The caller supplied the failing-test output and the framework selected the "
            "validation workflow; focus on FL/APR and use that supplied baseline."
            if baseline and baseline.get("baseline_external")
            else "The framework used the caller-prepared environment and selected the "
            "validation workflow; focus on FL/APR and use the supplied baseline."
        )
        if baseline
        else
        "The framework has copied the project here. The environment is prepared by the "
        "caller; do not install dependencies or alter its environment contract."
    )
    investigation_context = (
        "Read `.debugging-framework/baseline-output.txt` (the complete supplied baseline output), "
        "inspect the relevant production source and test layout, then investigate the "
        "root cause and implement the repair. Do not rerun setup or the failing test "
        "unless necessary."
        if baseline
        else
        "First inspect repository documentation, manifests, lockfiles, CI configuration, "
        "build files, and test layout. Use only the caller-prepared tools already available; "
        "do not install dependencies or change the environment contract."
    )

    workspace_description = (
        "This is a disposable, writable snapshot of the input project. The framework has "
        "copied the entire project, including source, tests, manifests, lockfiles and documentation."
    )
    return f"""You are a software engineer performing fault localization and program repair.
{workspace_description} {workflow_context}
The framework will extract your patch and independently validate it on fresh copies.

{investigation_context}
{known_failure}
Then investigate production source, callers, callees, data flow, error paths, and
invariants; implement and test the smallest root-cause repair.

Constraints:
- Do not browse for solutions or use git history, hidden accepted fixes, ground truth,
  or external source artifacts. Do not use the network to provision dependencies.
- The repair diff may modify only production C/C++ source/header files. Do not modify
  tests, fixtures, build files, scripts, Dockerfiles, project configuration, or validation
  commands. You may run the project's existing build/tests.
- Never install dependencies or host/system packages, change the selected environment,
  or write outside the workspace. Do not weaken or delete tests merely to make validation
  pass. Do not include generated build output, caches, dependency/vendor trees, or
  credentials in the repair.
- Make the smallest root-cause fix that preserves public APIs and project style.
- `repair.paths` must list every intentionally changed repository-relative file.
- `repair.diff` must be a raw unified/Git diff beginning with `diff --git a/...`
  or `--- a/<path>`, contain all intentional changes, and have no Markdown fence.
- Do not wrap the diff in prose, Markdown fences, or `*** Begin Patch`/`*** End Patch` markers.
- Return only valid JSON matching the supplied schema. Do not claim that tests passed.

Fault-localization entries must contain path, function/symbol, score in [0,1], and
concrete repository evidence. Include every repaired source location among the high scores.

Run context:
{json.dumps({"project_id": project_id, "attempt": attempt}, ensure_ascii=False, indent=2)}

{feedback}

{baseline_context}

Inspect the project, implement and test the repair in this workspace, review
the final diff, then return JSON with exactly `summary`, `fault_localization`, and `repair`.
"""
