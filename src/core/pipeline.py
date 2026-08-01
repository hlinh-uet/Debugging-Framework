from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from src.core.codex_runner import CodexRunner
from src.core.prompts import build_codex_prompt
from src.utils.jsonio import atomic_write_json, load_json, safe_name
from src.utils.workspace import (
    BugWorkspace,
    normalize_relpath,
    repair_candidate_files,
)


STATUS_RANK = {
    "plausible": 0,
    "success": 0,
    "cleanfix": 1,
    "noisefix": 2,
    "nonefix": 3,
    "negfix": 4,
    "invalid": 5,
    "llm_failed": 6,
    "skipped": 7,
}


@dataclass(frozen=True)
class PipelineOptions:
    dataset: str
    attempts: int = 2
    model: Optional[str] = None
    timeout_seconds: int = 1800
    exclude_fixed_fail_tests: bool = True
    evaluate_after_run: bool = True
    only_missing: bool = False
    inherit_codex_config: bool = False


class DebuggingPipeline:
    def __init__(self, *, settings, loader, validator, evaluator, runner_factory=CodexRunner):
        self.settings = settings
        self.loader = loader
        self.validator = validator
        self.evaluator = evaluator
        self.runner_factory = runner_factory

    @property
    def fl_results_path(self) -> Path:
        return self.settings.results_dir / "fault_localization_results.json"

    @property
    def apr_results_path(self) -> Path:
        return self.settings.results_dir / "apr_results.json"

    def run(self, options: PipelineOptions, bug_ids: Optional[Iterable[str]] = None) -> dict:
        bugs = self.loader.load_bugs(options.dataset, bug_ids)
        self.settings.results_dir.mkdir(parents=True, exist_ok=True)
        fl_results = load_json(self.fl_results_path, {})
        apr_results = load_json(self.apr_results_path, {})
        if not isinstance(fl_results, dict):
            fl_results = {}
        if not isinstance(apr_results, dict):
            apr_results = {}

        manifest = {
            "framework": "debugging-framework-codex-cli",
            "version": 1,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "dataset": options.dataset,
            "requested_bug_ids": list(bug_ids or []),
            "attempts": options.attempts,
            "model": options.model or "codex-cli-default",
            "unified_debugging_root": str(self.settings.unified_root),
            "results_dir": str(self.settings.results_dir),
            "exclude_fixed_fail_tests": options.exclude_fixed_fail_tests,
        }
        atomic_write_json(self.settings.results_dir / "run_manifest.json", manifest)

        for index, bug in enumerate(bugs, start=1):
            bug_id = str(bug.bug_id)
            if options.only_missing and bug_id in apr_results:
                print(f"[{index}/{len(bugs)}] {bug_id}: đã có kết quả, bỏ qua.")
                continue
            print(f"[{index}/{len(bugs)}] Codex FL+APR: {bug_id}")
            fl_record, apr_record = self._run_bug(bug, options)
            fl_results[bug_id] = fl_record
            apr_results[bug_id] = apr_record
            atomic_write_json(self.fl_results_path, fl_results)
            atomic_write_json(self.apr_results_path, apr_results)
            print(
                f"    Kết quả: status={apr_record.get('status')} "
                f"real_status={apr_record.get('real_status')}"
            )

        manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
        manifest["processed_bug_count"] = len(bugs)
        atomic_write_json(self.settings.results_dir / "run_manifest.json", manifest)
        if options.evaluate_after_run:
            print(self.evaluate(options.dataset), end="")
        return apr_results

    def _run_bug(self, bug, options: PipelineOptions) -> tuple[dict, dict]:
        initial = self.validator.initial_snapshot(
            list(bug.tests or []), options.exclude_fixed_fail_tests
        )
        allowed = repair_candidate_files(bug)
        runner = self.runner_factory(
            executable=self.settings.codex_executable,
            schema_path=self.settings.output_schema,
            model=options.model,
            timeout_seconds=options.timeout_seconds,
            inherit_user_config=options.inherit_codex_config,
        )
        bug_artifacts = self.settings.results_dir / "codex_artifacts" / safe_name(
            bug.bug_id, 80
        )
        attempts = []
        candidates = []
        localization_payloads = []
        codex_responses = []
        previous_feedback = None

        if not initial.get("comparison_failed"):
            snapshot = self.validator.invalid_snapshot(
                initial,
                "no_actionable_failed_tests",
                options.exclude_fixed_fail_tests,
            )
            snapshot["status"] = "skipped"
            snapshot["real_status"] = "skipped"
            return self._fl_record(bug, options.dataset, [], []), {
                "dataset": options.dataset,
                **snapshot,
                "attempts": [],
                "codex_responses": [],
            }

        for attempt_index in range(1, max(1, options.attempts) + 1):
            attempt_dir = bug_artifacts / f"attempt_{attempt_index:02d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            prompt = build_codex_prompt(
                bug_id=str(bug.bug_id),
                dataset=options.dataset,
                tests=list(bug.tests or []),
                allowed_source_files=allowed,
                attempt=attempt_index,
                previous_attempt=previous_feedback,
            )
            try:
                with BugWorkspace(
                    bug,
                    self.settings.results_dir / "worktrees",
                    repair_candidates=allowed,
                ) as workspace:
                    run = runner.run(
                        workspace=workspace.path,
                        prompt=prompt,
                        artifact_dir=attempt_dir,
                    )
                    # CodexRunner writes the exact final response itself. Keep
                    # a deterministic fallback for custom runners/tests so the
                    # response is always recoverable from the attempt artifact.
                    response_path = attempt_dir / "response.json"
                    if run.payload and not response_path.is_file():
                        atomic_write_json(response_path, run.payload)
                    if run.payload:
                        localization_payloads.append(run.payload)
                    response_artifact = str(
                        (attempt_dir / "response.json").relative_to(
                            self.settings.results_dir
                        )
                    )
                    codex_responses.append(
                        {
                            "attempt": attempt_index,
                            "ok": run.ok,
                            "error": run.error,
                            "response": run.payload,
                            "artifact": response_artifact,
                        }
                    )
                    base_attempt = {
                        "attempt": attempt_index,
                        "codex_ok": run.ok,
                        "codex_error": run.error,
                        "codex_returncode": run.returncode,
                        "elapsed_seconds": round(run.elapsed_seconds, 3),
                        "response": run.payload,
                        "codex_response_artifact": response_artifact,
                    }
                    if not run.ok:
                        attempts.append(base_attempt)
                        previous_feedback = {
                            "error": run.error,
                            "instruction": "Codex invocation failed; return a valid repair next time.",
                        }
                        continue

                    try:
                        workspace.apply_unified_diff(
                            (run.payload.get("repair") or {}).get("diff", "")
                        )
                    except Exception as exc:
                        error = f"codex_diff_apply_failed:{exc}"
                        snapshot = self.validator.invalid_snapshot(
                            initial,
                            error,
                            options.exclude_fixed_fail_tests,
                        )
                        attempts.append({**base_attempt, **snapshot})
                        previous_feedback = {
                            "error": error,
                            "codex_diff": (run.payload.get("repair") or {}).get("diff", ""),
                        }
                        continue

                    changed = workspace.changed_source_files()
                    unexpected = workspace.unexpected_changes()
                    policy_error = self._patch_policy_error(
                        run.payload, changed, unexpected, allowed
                    )
                    if policy_error:
                        snapshot = self.validator.invalid_snapshot(
                            initial,
                            policy_error,
                            options.exclude_fixed_fail_tests,
                        )
                        attempt_record = {**base_attempt, **snapshot}
                        attempts.append(attempt_record)
                        previous_feedback = {
                            "error": policy_error,
                            "changed_source_files": changed,
                            "unexpected_changes": unexpected,
                        }
                        continue

                    relpath = changed[0]
                    patched_text = workspace.patched_text(relpath)
                    diff = workspace.function_diff(relpath)
                    diff_path = attempt_dir / "patch.diff"
                    patched_path = attempt_dir / f"patched__{safe_name(relpath, 160)}"
                    diff_path.write_text(diff, encoding="utf-8")
                    patched_path.write_text(patched_text, encoding="utf-8")
                    selected = select_localization(run.payload, relpath)
                    function = str(selected.get("function") or "").strip()
                    patched_function = self.validator.extract_function(
                        patched_text, function, relpath
                    )
                    snapshot = self.validator.validate(
                        dataset=options.dataset,
                        bug_id=str(bug.bug_id),
                        patched_file=patched_path,
                        source_relpath=relpath,
                        initial=initial,
                        exclude_fixed_fail_tests=options.exclude_fixed_fail_tests,
                    )
                    candidate = {
                        **base_attempt,
                        **snapshot,
                        "repair_target_relpath": relpath,
                        "selected_function": localization_key(relpath, function),
                        "patched_function": patched_function,
                        "patched_file": patched_text,
                        "patch_diff": diff,
                        "patched_file_artifact": str(
                            patched_path.relative_to(self.settings.results_dir)
                        ),
                        "patch_diff_artifact": str(
                            diff_path.relative_to(self.settings.results_dir)
                        ),
                        "baseline_sha256": workspace.baseline_sha256(relpath),
                    }
                    candidates.append(candidate)
                    attempts.append(public_attempt(candidate))
                    previous_feedback = {
                        "summary": run.payload.get("summary"),
                        "repair_target_relpath": relpath,
                        "patch_diff": diff,
                        "status": snapshot.get("status"),
                        "real_status": snapshot.get("real_status"),
                        "post_failed_tests": snapshot.get("post_failed_tests"),
                        "validation_error": snapshot.get("validation_error"),
                        "validation_details": snapshot.get("validation_details"),
                    }
                    if snapshot.get("status") == "plausible":
                        self._save_plausible_patch(bug.bug_id, relpath, patched_path)
                        break
            except Exception as exc:
                error = f"attempt_exception:{type(exc).__name__}:{exc}"
                attempts.append(
                    {
                        "attempt": attempt_index,
                        "codex_ok": False,
                        "codex_error": error,
                        "status": "invalid",
                    }
                )
                previous_feedback = {"error": error}

        fl_record = self._fl_record(
            bug,
            options.dataset,
            localization_payloads,
            codex_responses,
        )
        if not candidates:
            snapshot = self.validator.invalid_snapshot(
                initial,
                "codex_did_not_produce_valid_source_patch",
                options.exclude_fixed_fail_tests,
            )
            latest_response = codex_responses[-1] if codex_responses else {}
            return fl_record, {
                "dataset": options.dataset,
                **snapshot,
                "attempts": attempts,
                "codex_response": latest_response.get("response", {}),
                "codex_response_artifact": latest_response.get("artifact", ""),
                "codex_responses": codex_responses,
            }

        best = min(candidates, key=candidate_sort_key)
        result = {
            "dataset": options.dataset,
            "selected_function": best.get("selected_function", ""),
            "repair_target_relpath": best.get("repair_target_relpath", ""),
            "patched_function": best.get("patched_function", ""),
            "patched_file": best.get("patched_file", ""),
            "patched_file_artifact": best.get("patched_file_artifact", ""),
            "patch_diff_artifact": best.get("patch_diff_artifact", ""),
            "codex_response": best.get("response", {}),
            "codex_response_artifact": best.get("codex_response_artifact", ""),
            "codex_responses": codex_responses,
            "attempts": attempts,
        }
        for key, value in best.items():
            if key in {
                "status",
                "real_status",
                "validation_error",
                "validation_executed",
                "compile_executed",
                "tests_executed",
                "init_passed_tests",
                "init_failed_tests",
                "full_init_passed_tests",
                "full_init_failed_tests",
                "post_passed_tests",
                "post_failed_tests",
                "full_post_passed_tests",
                "full_post_failed_tests",
                "fixed_fail_excluded_tests",
                "validation_details",
            }:
                result[key] = value
        return fl_record, result

    def _fl_record(
        self,
        bug,
        dataset: str,
        payloads: list[dict],
        codex_responses: list[dict],
    ) -> dict:
        scores = {}
        evidence = {}
        for payload in payloads:
            for item in payload.get("fault_localization") or []:
                if not isinstance(item, dict):
                    continue
                key = localization_key(item.get("path"), item.get("function"))
                if not key:
                    continue
                try:
                    score = min(1.0, max(0.0, float(item.get("score"))))
                except (TypeError, ValueError):
                    continue
                if score >= scores.get(key, -1.0):
                    scores[key] = score
                    evidence[key] = str(item.get("reason") or "")
        ordered = dict(sorted(scores.items(), key=lambda item: item[1], reverse=True))
        latest_response = codex_responses[-1] if codex_responses else {}
        return {
            "dataset": dataset,
            "scores": ordered,
            "ground_truth": list(bug.ground_truth or []),
            "codex_evidence": {key: evidence[key] for key in ordered},
            "engine": "codex-cli",
            "ground_truth_used": False,
            "codex_response": latest_response.get("response", {}),
            "codex_response_artifact": latest_response.get("artifact", ""),
            "codex_responses": codex_responses,
        }

    @staticmethod
    def _patch_policy_error(
        payload: dict, changed: list[str], unexpected: list[str], allowed: list[str]
    ) -> str:
        if unexpected:
            return "codex_modified_unexpected_files:" + ",".join(unexpected)
        if len(changed) != 1:
            return f"codex_changed_source_count:{len(changed)}"
        response_path = normalize_relpath((payload.get("repair") or {}).get("path"))
        if response_path != changed[0]:
            return f"codex_repair_path_mismatch:{response_path or '<empty>'}:{changed[0]}"
        if changed[0] not in allowed:
            return f"codex_changed_disallowed_source:{changed[0]}"
        return ""

    def _save_plausible_patch(self, bug_id: str, relpath: str, source: Path) -> None:
        patch_dir = self.settings.results_dir / "patches"
        patch_dir.mkdir(parents=True, exist_ok=True)
        target = patch_dir / f"{safe_name(bug_id, 80)}__{safe_name(relpath, 160)}"
        shutil.copyfile(source, target)

    def revalidate(
        self,
        dataset: str,
        bug_ids: Optional[Iterable[str]],
        exclude_fixed_fail_tests: bool,
    ) -> dict:
        results = load_json(self.apr_results_path, {})
        if not isinstance(results, dict):
            raise ValueError(f"APR results không hợp lệ: {self.apr_results_path}")
        if bug_ids:
            requested = list(bug_ids)
        else:
            requested = [
                bug_id
                for bug_id, record in results.items()
                if isinstance(record, dict)
                and str(record.get("dataset") or "").strip().lower()
                == dataset.strip().lower()
            ]
        bugs = self.loader.load_bugs(dataset, requested)
        for bug in bugs:
            record = results.get(str(bug.bug_id))
            if not isinstance(record, dict):
                print(f"{bug.bug_id}: không có APR result, bỏ qua.")
                continue
            relpath = str(record.get("repair_target_relpath") or "")
            artifact = str(record.get("patched_file_artifact") or "")
            patched_path = Path(artifact)
            if not patched_path.is_absolute():
                patched_path = self.settings.results_dir / patched_path
            if not patched_path.is_file():
                print(f"{bug.bug_id}: thiếu patched artifact {patched_path}, bỏ qua.")
                continue
            initial = self.validator.initial_snapshot(
                list(bug.tests or []), exclude_fixed_fail_tests
            )
            snapshot = self.validator.validate(
                dataset=dataset,
                bug_id=str(bug.bug_id),
                patched_file=patched_path,
                source_relpath=relpath,
                initial=initial,
                exclude_fixed_fail_tests=exclude_fixed_fail_tests,
            )
            record.update(snapshot)
            print(f"{bug.bug_id}: {snapshot.get('status')}")
            atomic_write_json(self.apr_results_path, results)
        return results

    def evaluate(self, dataset: str) -> str:
        return self.evaluator.evaluate(dataset, self.settings.results_dir)


def localization_key(path: object, function: object) -> str:
    relpath = normalize_relpath(path)
    if not relpath:
        return ""
    basename = os.path.basename(relpath)
    symbol = str(function or "").strip()
    return f"{basename}:{symbol}" if symbol else basename


def select_localization(payload: dict, relpath: str) -> dict:
    candidates = [
        item
        for item in payload.get("fault_localization") or []
        if isinstance(item, dict)
    ]
    exact = [item for item in candidates if normalize_relpath(item.get("path")) == relpath]
    if exact:
        return max(exact, key=lambda item: float(item.get("score") or 0.0))
    same_basename = [
        item
        for item in candidates
        if os.path.basename(normalize_relpath(item.get("path"))) == os.path.basename(relpath)
    ]
    if same_basename:
        return max(same_basename, key=lambda item: float(item.get("score") or 0.0))
    return candidates[0] if candidates else {}


def candidate_sort_key(candidate: dict) -> tuple:
    status = str(candidate.get("status") or "invalid").lower()
    post_failed = candidate.get("post_failed_tests")
    full_failed = candidate.get("full_post_failed_tests")
    return (
        STATUS_RANK.get(status, 99),
        len(post_failed) if isinstance(post_failed, list) else 10**9,
        len(full_failed) if isinstance(full_failed, list) else 10**9,
        int(candidate.get("attempt") or 0),
    )


def public_attempt(candidate: dict) -> dict:
    omitted = {"patched_file", "patched_function", "patch_diff"}
    return {key: value for key, value in candidate.items() if key not in omitted}
