from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.core.codex_runner import CodexRunner
from src.core.prompts import build_codex_prompt
from src.utils.jsonio import atomic_write_json, atomic_write_text, safe_name
from src.utils.workspace import ProjectWorkspace, normalize_relpath


STATUS_RANK = {
    "plausible": 0,
    "failing": 1,
    "invalid": 2,
    "llm_failed": 3,
    "baseline_passed": 4,
}


@dataclass(frozen=True)
class PipelineOptions:
    attempts: int = 2
    model: Optional[str] = None
    codex_timeout_seconds: int = 1800
    allow_clean_project: bool = False
    inherit_codex_config: bool = False


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
        project_results.mkdir(parents=True, exist_ok=True)
        self._prepare_output_target(output_patch)

        manifest = {
            "framework": "debugging-framework-project-repair",
            "version": 4,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "project": str(project.path),
            "project_id": project.project_id,
            "attempts": options.attempts,
            "model": options.model or "codex-cli-default",
            "output_patch": str(output_patch),
        }
        atomic_write_json(project_results / "run_manifest.json", manifest)

        print(f"[baseline] Tự nhận diện build/test: {project.path}")
        baseline_dir = project_results / "baseline"
        try:
            baseline = self.validator.baseline(project, baseline_dir)
        except Exception as exc:
            baseline = self.validator.invalid_snapshot(
                f"baseline_exception:{type(exc).__name__}:{exc}"
            )
        atomic_write_json(baseline_dir / "result.json", baseline)

        if baseline.get("status") == "invalid":
            result = {
                "status": "invalid",
                "validation_error": baseline.get("validation_error", "baseline_invalid"),
                "project": str(project.path),
                "output_patch": "",
                "patch_validation_passed": False,
                "baseline": baseline,
                "attempts": [],
            }
            return self._finish(project_results, manifest, result)
        if baseline.get("status") == "plausible" and not options.allow_clean_project:
            result = {
                "status": "baseline_passed",
                "validation_error": "baseline_tests_already_pass",
                "project": str(project.path),
                "output_patch": "",
                "patch_validation_passed": False,
                "baseline": baseline,
                "attempts": [],
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

        for attempt_index in range(1, options.attempts + 1):
            print(f"[attempt {attempt_index}/{options.attempts}] Codex đọc project và tạo patch")
            attempt_dir = project_results / "attempts" / f"attempt_{attempt_index:02d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            prompt = build_codex_prompt(
                project_id=project.project_id,
                attempt=attempt_index,
                baseline=baseline,
                previous_attempt=previous_feedback,
            )
            try:
                with ProjectWorkspace(project, project_results / "workspaces") as workspace:
                    run = runner.run(workspace=workspace.path, prompt=prompt, artifact_dir=attempt_dir)
                    base_attempt = {
                        "attempt": attempt_index,
                        "codex_ok": run.ok,
                        "codex_error": run.error,
                        "codex_returncode": run.returncode,
                        "elapsed_seconds": round(run.elapsed_seconds, 3),
                        "response": run.payload,
                    }
                    raw_diff = str(((run.payload or {}).get("repair") or {}).get("diff") or "")
                    if raw_diff.strip():
                        raw_diff_path = attempt_dir / "llm.patch.diff"
                        atomic_write_text(raw_diff_path, raw_diff)
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
                        repair = run.payload.get("repair") or {}
                        patch_paths = workspace.unified_diff_paths(raw_diff)
                    except Exception as exc:
                        error = f"codex_diff_parse_failed:{exc}"
                        attempts.append(
                            {**base_attempt, **self.validator.invalid_snapshot(error, baseline)}
                        )
                        previous_feedback = {"error": error}
                        continue
                    policy_error = self._response_path_error(run.payload, patch_paths)
                    if policy_error:
                        attempts.append(
                            {**base_attempt, **self.validator.invalid_snapshot(policy_error, baseline)}
                        )
                        previous_feedback = {
                            "error": policy_error,
                            "patch_paths": patch_paths,
                        }
                        continue
                    baseline_hashes = workspace.baseline_sha256s(patch_paths)
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
                        f"Build/test patch: {len(patch_paths)} file(s)"
                    )
                    try:
                        snapshot = self.validator.validate_diff(
                            project=project,
                            diff=raw_diff,
                            patch_paths=patch_paths,
                            artifact_dir=attempt_dir / "validation",
                            expected_sha256s=baseline_hashes,
                        )
                    except Exception as exc:
                        snapshot = self.validator.invalid_snapshot(
                            f"validation_exception:{type(exc).__name__}:{exc}", baseline
                        )
                    candidate = {
                        **base_attempt,
                        **snapshot,
                        "repair_paths": patch_paths,
                        "selected_functions": selected_functions,
                        "patch_diff": raw_diff,
                        "llm_patch_artifact": base_attempt.get("llm_patch_artifact", ""),
                        "patch_diff_artifact": str(diff_path.relative_to(project_results)),
                        "baseline_sha256s": baseline_hashes,
                        "workspace_changed_files": workspace_changes[:200],
                        "workspace_changed_file_count": len(workspace_changes),
                        "workspace_changes_not_in_patch": sorted(
                            set(workspace_changes) - set(patch_paths)
                        )[:200],
                        "patch_paths_not_changed_in_workspace": sorted(
                            set(patch_paths) - set(workspace_changes)
                        )[:200],
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
                            project, baseline, attempts, candidate, output_patch, payloads
                        )
                        return self._finish(project_results, manifest, result)
                    previous_feedback = {
                        "error": snapshot.get("validation_error") or "tests_still_fail",
                        "failed_tests": snapshot.get("post_failed_tests", []),
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
                project, baseline, attempts, best, output_patch if published else None, payloads
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
                "baseline": baseline,
                "attempts": attempts,
                "fault_localization": self._fault_localization(payloads),
            }
        else:
            result = {
                "status": "invalid",
                "validation_error": "codex_did_not_produce_applicable_patch",
                "project": str(project.path),
                "output_patch": "",
                "patch_validation_passed": False,
                "baseline": baseline,
                "attempts": attempts,
                "fault_localization": self._fault_localization(payloads),
            }
        return self._finish(project_results, manifest, result)

    def _result(self, project, baseline, attempts, candidate, output_patch, payloads) -> dict:
        return {
            "status": candidate.get("status", "invalid"),
            "validation_error": candidate.get("validation_error", ""),
            "project": str(project.path),
            "output_patch": str(output_patch) if output_patch else "",
            "repair_paths": candidate.get("repair_paths", []),
            "selected_functions": candidate.get("selected_functions", []),
            "patch_diff_artifact": candidate.get("patch_diff_artifact", ""),
            "llm_patch_artifact": candidate.get("llm_patch_artifact", ""),
            "patch_validation_passed": candidate.get("status") == "plausible",
            "baseline": baseline,
            "validation": {
                key: value for key, value in candidate.items()
                if key in {
                    "status", "validation_error", "build_system", "build_plan",
                    "setup_commands", "build_commands", "test_commands",
                    "post_passed_tests", "post_failed_tests", "failure_output",
                    "validation_workspace_isolated", "validation_process_sandboxed",
                    "input_project_untouched",
                    "patch_paths",
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
    failed = candidate.get("post_failed_tests")
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
