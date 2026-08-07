from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.core.codex_runner import CodexRunner
from src.core.prompts import build_codex_prompt
from src.utils.jsonio import atomic_write_json, atomic_write_text, safe_name
from src.utils.workspace import (
    CurrentProjectWorkspace,
    ProjectWorkspace,
    normalize_relpath,
    normalize_unified_diff,
)


STATUS_RANK = {
    "plausible": 0,
    "cleanfix": 1,
    "noisefix": 2,
    "nonefix": 3,
    "negfix": 4,
    # Kept for callers that construct PipelineOptions without a baseline.
    "failing": 5,
    "invalid": 6,
    "llm_failed": 7,
}


@dataclass(frozen=True)
class PipelineOptions:
    attempts: int = 2
    model: Optional[str] = None
    codex_timeout_seconds: int = 1800
    inherit_codex_config: bool = False
    failing_tests: tuple[str, ...] = ()
    external_baseline_output: str | None = None
    workspace_mode: str = "auto"


class DebuggingPipeline:
    """Project-in repair pipeline with raw diff retention and isolated validation."""

    def __init__(self, *, settings, validator, runner_factory=CodexRunner):
        self.settings = settings
        self.validator = validator
        self.runner_factory = runner_factory

    def run(self, project, options: PipelineOptions, output_patch: Path | None = None) -> dict:
        project_root = project.path.expanduser().resolve()
        project_results = (
            self.settings.results_dir / safe_name(project.project_id, 100)
        ).expanduser().resolve()
        output_patch = (output_patch or (project_results / "patch.diff")).expanduser().resolve()
        self._require_outside_input(project_root, project_results, "results")
        self._require_outside_input(project_root, output_patch, "patch output")
        if options.workspace_mode not in {"auto", "snapshot", "current"}:
            raise ValueError("workspace_mode phải là auto, snapshot hoặc current")
        workspace_mode = options.workspace_mode
        if workspace_mode == "auto":
            workspace_mode = (
                "current" if options.external_baseline_output is not None else "snapshot"
            )
        project_results.mkdir(parents=True, exist_ok=True)
        self._prepare_output_target(output_patch)

        manifest = {
            "framework": "debugging-framework-project-repair",
            "version": 7,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "project": str(project.path),
            "project_id": project.project_id,
            "attempts": options.attempts,
            "model": options.model or "codex-cli-default",
            "failing_tests": list(options.failing_tests),
            "output_patch": str(output_patch),
            "baseline_source": (
                "external" if options.external_baseline_output is not None else "framework"
            ),
            "codex_workspace": workspace_mode,
        }
        atomic_write_json(project_results / "run_manifest.json", manifest)

        baseline_snapshot: dict | None = None
        if options.external_baseline_output is not None:
            print("[baseline] dùng output do caller cung cấp; bỏ qua chạy lại failing test")
            try:
                baseline_snapshot = self.validator.external_baseline(
                    project,
                    project_results / "baseline",
                    failing_tests=options.failing_tests,
                    failure_output=options.external_baseline_output,
                )
            except Exception as exc:
                baseline_snapshot = self.validator.invalid_snapshot(
                    f"external_baseline_exception:{type(exc).__name__}:{exc}"
                )
        elif options.failing_tests:
            print("[baseline] provision/reproduce failing test trước khi FL/APR")
            try:
                baseline_snapshot = self.validator.baseline(
                    project,
                    project_results / "baseline",
                    failing_tests=options.failing_tests,
                )
            except Exception as exc:
                baseline_snapshot = self.validator.invalid_snapshot(
                    f"baseline_exception:{type(exc).__name__}:{exc}"
                )
        if baseline_snapshot is not None:
            baseline_output_path = project_results / "baseline" / "baseline-output.txt"
            if baseline_output_path.is_file():
                baseline_snapshot["baseline_output_artifact"] = str(
                    baseline_output_path.relative_to(project_results)
                )
                baseline_snapshot["codex_snapshot_output_file"] = (
                    ".debugging-framework/baseline-output.txt"
                )
            atomic_write_json(project_results / "baseline.json", baseline_snapshot)
            manifest["baseline"] = {
                "status": baseline_snapshot.get("status"),
                "validation_error": baseline_snapshot.get("validation_error", ""),
                "plan_digest": baseline_snapshot.get("plan_digest", ""),
                "environment_digest": baseline_snapshot.get("environment_digest", ""),
            }
            baseline_accepted = bool(
                baseline_snapshot.get("baseline_reproduced", False)
                or (
                    baseline_snapshot.get("baseline_external", False)
                    and baseline_snapshot.get("baseline_observed", False)
                )
            )
            if baseline_snapshot.get("status") != "failing" or not baseline_accepted:
                result = {
                    "status": "invalid",
                    "validation_error": baseline_snapshot.get("validation_error")
                    or "baseline_not_reproduced",
                    "project": str(project.path),
                    "output_patch": "",
                    "patch_validation_passed": False,
                    "failing_tests": list(options.failing_tests),
                    "baseline": baseline_snapshot,
                    "attempts": [],
                    "fault_localization": {},
                }
                return self._finish(project_results, manifest, result)

        runner = self.runner_factory(
            executable=self.settings.codex_executable,
            schema_path=self.settings.output_schema,
            api_key=getattr(self.settings, "codex_api_key", ""),
            provider=getattr(self.settings, "codex_provider", ""),
            base_url=getattr(self.settings, "codex_base_url", ""),
            wire_api=getattr(self.settings, "codex_wire_api", "responses"),
            env_key=getattr(self.settings, "codex_env_key", "CODEX_API_KEY"),
            model=options.model,
            timeout_seconds=options.codex_timeout_seconds,
            inherit_user_config=options.inherit_codex_config,
        )
        attempts: list[dict] = []
        candidates: list[dict] = []
        payloads: list[dict] = []
        raw_patches: list[dict] = []
        previous_feedback: dict | None = None
        direct_workspace = (
            CurrentProjectWorkspace(project) if workspace_mode == "current" else None
        )
        if direct_workspace is not None:
            direct_workspace.__enter__()
        for attempt_index in range(1, options.attempts + 1):
            print(f"[attempt {attempt_index}/{options.attempts}] Codex đọc project và tạo patch")
            attempt_dir = project_results / "attempts" / f"attempt_{attempt_index:02d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            prompt = build_codex_prompt(
                project_id=project.project_id,
                attempt=attempt_index,
                failing_tests=options.failing_tests,
                previous_attempt=previous_feedback,
                baseline=baseline_snapshot,
                workspace_mode=workspace_mode,
            )
            try:
                workspace_context = (
                    nullcontext(direct_workspace)
                    if direct_workspace is not None
                    else ProjectWorkspace(project, project_results / "workspaces")
                )
                with workspace_context as workspace:
                    self._write_baseline_context(workspace, baseline_snapshot)
                    run = runner.run(workspace=workspace.path, prompt=prompt, artifact_dir=attempt_dir)
                    payload_artifact = attempt_dir / "codex.payload.json"
                    atomic_write_json(payload_artifact, run.payload)
                    base_attempt = {
                        "attempt": attempt_index,
                        "codex_ok": run.ok,
                        "codex_error": run.error,
                        "codex_returncode": run.returncode,
                        "elapsed_seconds": round(run.elapsed_seconds, 3),
                        "response": run.payload,
                        "codex_response_artifact": str(
                            payload_artifact.relative_to(project_results)
                        ),
                        "codex_events_artifact": str(
                            (attempt_dir / "events.jsonl").relative_to(project_results)
                        ),
                        "codex_stderr_artifact": str(
                            (attempt_dir / "stderr.txt").relative_to(project_results)
                        ),
                    }
                    raw_diff = str(((run.payload or {}).get("repair") or {}).get("diff") or "")
                    if raw_diff.strip():
                        raw_diff_path = attempt_dir / "llm.patch.diff"
                        atomic_write_text(raw_diff_path, raw_diff)
                        normalized_diff, normalization_actions = normalize_unified_diff(raw_diff)
                        if normalization_actions:
                            normalized_path = attempt_dir / "normalized.patch.diff"
                            atomic_write_text(normalized_path, normalized_diff)
                            base_attempt["normalized_patch_artifact"] = str(
                                normalized_path.relative_to(project_results)
                            )
                            base_attempt["diff_normalization_actions"] = normalization_actions
                        raw_diff = normalized_diff
                        raw_record = {
                            "attempt": attempt_index,
                            "diff": raw_diff,
                            "artifact": str(raw_diff_path.relative_to(project_results)),
                        }
                        raw_patches.append(raw_record)
                        base_attempt["llm_patch_artifact"] = raw_record["artifact"]
                    if not run.ok:
                        attempts.append({**base_attempt, "status": "llm_failed"})
                        previous_feedback = {"error": run.error}
                        continue
                    payloads.append(run.payload)

                    try:
                        patch_paths = workspace.unified_diff_paths(raw_diff)
                    except Exception as exc:
                        error = f"codex_diff_parse_failed:{exc}"
                        attempts.append(
                            {**base_attempt, **self.validator.invalid_snapshot(error)}
                        )
                        previous_feedback = {"error": error}
                        continue
                    policy_error = self._response_path_error(run.payload, patch_paths)
                    if policy_error:
                        attempts.append(
                            {**base_attempt, **self.validator.invalid_snapshot(policy_error)}
                        )
                        previous_feedback = {
                            "error": policy_error,
                            "patch_paths": patch_paths,
                        }
                        continue
                    snapshot_hashes = (
                        None
                        if getattr(workspace, "in_place", False)
                        else workspace.snapshot_sha256s(patch_paths)
                    )
                    workspace_changes = workspace.changed_repository_files()
                    localized = select_localizations(run.payload, patch_paths)
                    selected_functions = [
                        localization_key(item.get("path"), item.get("function"))
                        for item in localized
                    ]
                    diff_path = attempt_dir / "validated.patch.diff"
                    atomic_write_text(diff_path, raw_diff)

                    print(
                        f"[attempt {attempt_index}/{options.attempts}] "
                        f"Apply/provision/build/test diff: {len(patch_paths)} file(s)"
                    )
                    validation_context = (
                        workspace.clean_source_for_validation()
                        if getattr(workspace, "in_place", False)
                        else nullcontext()
                    )
                    with validation_context as clean_project_path:
                        validation_project = (
                            replace(project, path=clean_project_path)
                            if clean_project_path is not None
                            else project
                        )
                        try:
                            snapshot = self.validator.validate_diff(
                                project=validation_project,
                                diff=raw_diff,
                                patch_paths=patch_paths,
                                artifact_dir=attempt_dir / "validation",
                                expected_sha256s=snapshot_hashes,
                                failing_tests=options.failing_tests,
                                expected_plan_digest=(baseline_snapshot or {}).get("plan_digest", ""),
                                expected_environment_digest=(baseline_snapshot or {}).get(
                                    "environment_digest", ""
                                ),
                                expected_image_digest=(baseline_snapshot or {}).get(
                                    "provisioned_image_digest", ""
                                ),
                            )
                        except Exception as exc:
                            snapshot = self.validator.invalid_snapshot(
                                f"validation_exception:{type(exc).__name__}:{exc}"
                            )
                    if baseline_snapshot is not None:
                        snapshot = classify_validation_result(baseline_snapshot, snapshot)
                    candidate = {
                        **base_attempt,
                        **snapshot,
                        "repair_paths": patch_paths,
                        "selected_functions": selected_functions,
                        "patch_diff": raw_diff,
                        "llm_patch_artifact": base_attempt.get("llm_patch_artifact", ""),
                        "patch_diff_artifact": str(diff_path.relative_to(project_results)),
                        "snapshot_sha256s": snapshot_hashes,
                        "workspace_changed_files": workspace_changes[:200],
                        "workspace_changed_file_count": len(workspace_changes),
                        "workspace_changes_not_in_patch": sorted(
                            set(workspace_changes) - set(patch_paths)
                        )[:200],
                        "patch_paths_not_changed_in_workspace": sorted(
                            set(patch_paths) - set(workspace_changes)
                        )[:200],
                        "baseline": baseline_snapshot or {},
                    }
                    candidates.append(candidate)
                    attempts.append(public_attempt(candidate))
                    if snapshot.get("status") == "plausible":
                        try:
                            atomic_write_text(output_patch, raw_diff)
                        except OSError as exc:
                            candidate["status"] = "invalid"
                            candidate["validation_error"] = (
                                f"patch_publish_failed:{type(exc).__name__}:{exc}"
                            )
                            attempts[-1] = public_attempt(candidate)
                            previous_feedback = {"error": candidate["validation_error"]}
                            continue
                        result = self._result(
                            project, attempts, candidate, output_patch, payloads
                        )
                        if direct_workspace is not None:
                            direct_workspace.close()
                        return self._finish(project_results, manifest, result)
                    previous_feedback = {
                        "error": snapshot.get("validation_error") or "tests_still_fail",
                        "outcome": snapshot.get("status"),
                        "failed_tests": snapshot.get("failed_test_ids", []),
                        "test_output": snapshot.get("failure_output", ""),
                        "previous_patch": raw_diff,
                    }
            except Exception as exc:
                error = f"attempt_exception:{type(exc).__name__}:{exc}"
                attempts.append({"attempt": attempt_index, "status": "invalid", "error": error})
                previous_feedback = {"error": error}

        if candidates:
            best = min(candidates, key=candidate_sort_key)
            published, publish_error = self._publish_selected_patch(
                output_patch, str(best.get("patch_diff") or "")
            )
            if publish_error:
                best = {
                    **best,
                    "status": "invalid",
                    "validation_error": publish_error,
                }
            result = self._result(
                project, attempts, best, output_patch if published else None, payloads
            )
        elif raw_patches:
            latest = raw_patches[-1]
            published, publish_error = self._publish_selected_patch(output_patch, latest["diff"])
            result = {
                "status": "invalid",
                "validation_error": publish_error or "codex_did_not_produce_applicable_patch",
                "project": str(project.path),
                "output_patch": str(output_patch) if published else "",
                "llm_patch_artifact": latest["artifact"],
                "patch_validation_passed": False,
                "baseline": baseline_snapshot or {},
                "attempts": attempts,
                "fault_localization": self._fault_localization(payloads),
            }
        else:
            terminal_attempt = min(attempts, key=candidate_sort_key) if attempts else {}
            terminal_status = str(terminal_attempt.get("status") or "invalid")
            if terminal_status not in {
                "plausible", "cleanfix", "noisefix", "nonefix", "negfix", "invalid",
            }:
                terminal_status = "invalid"
            terminal_error = str(
                terminal_attempt.get("validation_error")
                or terminal_attempt.get("codex_error")
                or terminal_attempt.get("error")
                or "codex_did_not_produce_applicable_patch"
            )
            result = {
                "status": terminal_status,
                "validation_error": terminal_error,
                "project": str(project.path),
                "output_patch": "",
                "llm_patch_artifact": terminal_attempt.get("llm_patch_artifact", ""),
                "codex_response_artifact": terminal_attempt.get(
                    "codex_response_artifact", ""
                ),
                "codex_events_artifact": terminal_attempt.get("codex_events_artifact", ""),
                "codex_stderr_artifact": terminal_attempt.get("codex_stderr_artifact", ""),
                "patch_validation_passed": False,
                "baseline": baseline_snapshot or {},
                "attempts": attempts,
                "fault_localization": self._fault_localization(payloads),
            }
        if direct_workspace is not None:
            direct_workspace.close()
        return self._finish(project_results, manifest, result)

    @staticmethod
    def _write_baseline_context(workspace: ProjectWorkspace, baseline: dict | None) -> None:
        """Make the complete baseline log available inside Codex's project snapshot.

        The prompt points Codex at this file instead of embedding a potentially very
        large command log. This preserves the full evidence while keeping prompt token
        usage small and makes the input explicit and reproducible for every attempt.
        """
        if not baseline:
            return
        if hasattr(workspace, "write_baseline_context"):
            workspace.write_baseline_context(str(baseline.get("execution_output") or ""))
            return
        context_dir = workspace.path / ".debugging-framework"
        if context_dir.exists() and context_dir.is_symlink():
            return
        context_dir.mkdir(parents=True, exist_ok=True)
        context_file = context_dir / "baseline-output.txt"
        context_file.write_text(
            str(baseline.get("execution_output") or ""),
            encoding="utf-8",
            errors="replace",
        )

    def _result(self, project, attempts, candidate, output_patch, payloads) -> dict:
        return {
            "status": candidate.get("status", "invalid"),
            "validation_error": candidate.get("validation_error", ""),
            "project": str(project.path),
            "output_patch": str(output_patch) if output_patch else "",
            "repair_paths": candidate.get("repair_paths", []),
            "selected_functions": candidate.get("selected_functions", []),
            "patch_diff_artifact": candidate.get("patch_diff_artifact", ""),
            "llm_patch_artifact": candidate.get("llm_patch_artifact", ""),
            "normalized_patch_artifact": candidate.get("normalized_patch_artifact", ""),
            "diff_normalization_actions": candidate.get("diff_normalization_actions", []),
            "codex_response_artifact": candidate.get("codex_response_artifact", ""),
            "codex_events_artifact": candidate.get("codex_events_artifact", ""),
            "codex_stderr_artifact": candidate.get("codex_stderr_artifact", ""),
            "patch_validation_passed": candidate.get("status") == "plausible",
            "failing_tests": candidate.get("target_tests", []),
            "baseline": candidate.get("baseline") or {},
            "validation": {
                key: value for key, value in candidate.items()
                if key in {
                    "status", "validation_error", "build_system", "build_plan",
                    "setup_commands", "build_commands", "test_commands",
                    "setup_executed", "environment_provisioned",
                    "post_passed_tests", "post_failed_tests", "failure_output",
                    "validation_workspace_isolated", "validation_process_sandboxed",
                    "input_project_untouched",
                    "patch_paths",
                    "failed_test_ids", "passed_test_ids", "test_id_granularity",
                    "target_tests", "target_commands", "target_status", "target_passed",
                    "post_validation_status", "initial_failed_test_ids", "post_failed_test_ids",
                    "fixed_test_ids", "regression_test_ids", "classification_basis_valid",
                    "baseline_reproduced", "environment", "environment_digest", "plan_digest",
                    "environment_backend", "provisioned_image", "provisioned_image_digest",
                    "provisioned_container",
                }
            },
            "attempts": attempts,
            "codex_response": candidate.get("response", {}),
            "fault_localization": self._fault_localization(payloads),
        }

    @staticmethod
    def _finish(project_results: Path, manifest: dict, result: dict) -> dict:
        manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
        manifest["status"] = result.get("status")
        atomic_write_json(project_results / "run_manifest.json", manifest)
        atomic_write_json(project_results / "result.json", result)
        return result

    @staticmethod
    def _require_outside_input(project_root: Path, target: Path, label: str) -> None:
        try:
            target.resolve().relative_to(project_root.resolve())
        except ValueError:
            return
        raise ValueError(f"{label} phải nằm ngoài input project: {target}")

    @staticmethod
    def _prepare_output_target(output_patch: Path) -> None:
        if output_patch.suffix.lower() not in {".diff", ".patch"}:
            raise ValueError(
                f"Patch output phải có đuôi .diff hoặc .patch: {output_patch}"
            )
        if output_patch.is_dir():
            raise ValueError(f"Patch output là một thư mục: {output_patch}")
        if output_patch.exists() or output_patch.is_symlink():
            try:
                output_patch.unlink()
            except OSError as exc:
                raise RuntimeError(
                    f"Không thể dọn patch output cũ {output_patch}: {exc}"
                ) from exc

    @staticmethod
    def _publish_selected_patch(output_patch: Path, diff: str) -> tuple[bool, str]:
        if not diff.strip():
            return False, "patch_publish_failed:empty_diff"
        try:
            atomic_write_text(output_patch, diff)
        except OSError as exc:
            return False, f"patch_publish_failed:{type(exc).__name__}:{exc}"
        return True, ""

    @staticmethod
    def _fault_localization(payloads: list[dict]) -> dict[str, float]:
        scores: dict[str, float] = {}
        for payload in payloads:
            for item in payload.get("fault_localization") or []:
                if not isinstance(item, dict):
                    continue
                key = localization_key(item.get("path"), item.get("function"))
                try:
                    score = min(1.0, max(0.0, float(item.get("score"))))
                except (TypeError, ValueError):
                    continue
                if key:
                    scores[key] = max(scores.get(key, 0.0), score)
        return dict(sorted(scores.items(), key=lambda item: item[1], reverse=True))

    @staticmethod
    def _response_path_error(payload: dict, patch_paths: list[str]) -> str:
        if not patch_paths:
            return "codex_diff_changed_path_count:0"
        if any(path.split("/", 1)[0] == ".git" for path in patch_paths):
            return "codex_diff_targets_git_metadata"
        repair = payload.get("repair") or {}
        declared_raw = repair.get("paths")
        if isinstance(declared_raw, list):
            declared = [normalize_relpath(path) for path in declared_raw]
        else:
            legacy_path = normalize_relpath(repair.get("path"))
            declared = [legacy_path] if legacy_path else []
        if not declared:
            return "codex_response_missing_repair_paths"
        if any(not path for path in declared) or len(set(declared)) != len(declared):
            return "codex_response_invalid_repair_paths"
        if set(declared) != set(patch_paths):
            return (
                "codex_repair_paths_mismatch:"
                + ",".join(declared)
                + ":"
                + ",".join(patch_paths)
            )
        return ""


def localization_key(path: object, function: object) -> str:
    relpath = normalize_relpath(path)
    if not relpath:
        return ""
    symbol = str(function or "").strip()
    return f"{relpath}:{symbol}" if symbol else relpath


def select_localizations(payload: dict, relpaths: list[str]) -> list[dict]:
    targets = set(relpaths)
    candidates = [
        item for item in payload.get("fault_localization") or []
        if isinstance(item, dict) and normalize_relpath(item.get("path")) in targets
    ]
    return sorted(candidates, key=lambda item: float(item.get("score") or 0.0), reverse=True)


def candidate_sort_key(candidate: dict) -> tuple:
    status = str(candidate.get("status") or "invalid").lower()
    failed = candidate.get("failed_test_ids")
    if not isinstance(failed, list):
        failed = candidate.get("post_failed_test_ids")
    return (
        STATUS_RANK.get(status, 99),
        len(failed) if isinstance(failed, list) else 10**9,
        int(candidate.get("attempt") or 0),
    )


def public_attempt(candidate: dict) -> dict:
    return {
        key: value for key, value in candidate.items()
        if key not in {"patched_function", "patch_diff", "normalized_patch_diff", "response"}
    }


def validation_failed_test_ids(snapshot: dict | None) -> list[str]:
    snapshot = snapshot or {}
    values = snapshot.get("failed_test_ids")
    if not isinstance(values, list):
        values = snapshot.get("post_failed_tests")
    if not isinstance(values, list):
        return []
    return sorted({str(value).strip() for value in values if str(value).strip()})


def classify_patch_outcome(baseline: dict | None, patched: dict | None) -> str:
    """Classify a patch by comparing the same validation scope before and after it."""
    baseline = baseline or {}
    patched = patched or {}
    baseline_status = str(baseline.get("status") or "invalid")
    patched_status = str(patched.get("status") or "invalid")
    if baseline_status not in {"plausible", "failing"} or patched_status not in {
        "plausible", "failing",
    }:
        return "invalid"
    if str(baseline.get("validation_error") or "").strip() or str(
        patched.get("validation_error") or ""
    ).strip():
        return "invalid"

    initial = set(validation_failed_test_ids(baseline))
    post = set(validation_failed_test_ids(patched))
    if (baseline_status == "failing") != bool(initial):
        return "invalid"
    if (patched_status == "failing") != bool(post):
        return "invalid"
    if not post:
        return "plausible" if initial else "nonefix"
    fixed = initial - post
    regressions = post - initial
    if fixed and regressions:
        return "noisefix"
    if fixed:
        return "cleanfix"
    if regressions:
        return "negfix"
    return "nonefix"


def classify_validation_result(baseline: dict | None, patched: dict | None) -> dict:
    """Attach the public APR outcome and its auditable test-set comparison."""
    result = dict(patched or {})
    post_validation_status = str(result.get("status") or "invalid")
    outcome = classify_patch_outcome(baseline, result)
    initial = validation_failed_test_ids(baseline)
    post = validation_failed_test_ids(result)
    fixed: list[str] = []
    regressions: list[str] = []
    if outcome != "invalid":
        fixed = sorted(set(initial) - set(post))
        regressions = sorted(set(post) - set(initial))
    result.update(
        {
            "post_validation_status": post_validation_status,
            "status": outcome,
            "initial_failed_test_ids": initial,
            "post_failed_test_ids": post,
            "fixed_test_ids": fixed,
            "regression_test_ids": regressions,
            "classification_basis_valid": outcome != "invalid",
        }
    )
    return result
