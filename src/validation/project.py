from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import signal
import subprocess
import time
import xml.etree.ElementTree as ET
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from src.loaders.project import Project
from src.environments.oci import OCIEnvironment, OCIProvision
from src.environments.spec import EnvironmentResolver, EnvironmentSpec
from src.utils.project_config import (
    PROJECT_CONFIG_SCHEMA_VERSION,
    read_project_config_data,
    read_project_config_file,
)
from src.utils.workspace import (
    ProjectWorkspace,
    ValidationWorkspace,
    non_repairable_patch_paths,
    normalize_relpath,
)


BUILTIN_TEST_SYSTEMS = {
    "autotools", "bazel", "cmake", "make", "meson", "ninja",
}

TEST_EXECUTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in (
        r"\b(?:collected|ran)\s+[1-9]\d*\s+(?:items?|tests?)\b",
        r"\b[1-9]\d*\s+(?:passed|failed|errors?|tests?)\b",
        r"\btests?\s+run:\s*[1-9]\d*\b",
        r"\btest result:\s*(?:ok|failed)\b",
        r"^\s*(?:ok|not ok)\s+[1-9]\d*\b",
        r"^\s*(?:ok|fail)\s+\S+",
        r"\b\d+%\s+tests passed\b",
        r"\b(?:ok|fail):\s*[1-9]\d*\b",
        r"\b[1-9]\d*\s+examples?,\s*\d+\s+failures?\b",
        r"\btest suites?:\s*.*\b(?:passed|failed)\b",
        r"\btests?:\s*.*\b(?:passed|failed)\b",
    )
)

ZERO_TEST_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"no tests? (?:were )?(?:found|ran|to run)",
        r"running 0 tests",
        r"collected 0 items",
        r"ran 0 tests",
        r"tests? run:\s*0\b",
        r"\[no test files\]",
        r"\[no tests? to run\]",
        r"no tests? (?:to run|to execute)",
    )
)

TEST_FAILURE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in (
        r"\b[1-9]\d*\s+failed\b",
        r"\btest result:\s*failed\b",
        r"\bfail(?:ures?)?:\s*[1-9]\d*\b",
        r"\berrors?:\s*[1-9]\d*\b",
        r"^\s*not ok\s+[1-9]\d*\b",
        r"^\s*fail\s+\S+",
        r"\bthe following tests failed\b",
    )
)


@dataclass(frozen=True)
class CommandSpec:
    label: str
    argv: tuple[str, ...]
    cwd: str = "."
    evidence_pattern: str = ""
    failure_pattern: str = ""

    def as_dict(self) -> dict:
        value = {"label": self.label, "argv": list(self.argv), "cwd": self.cwd}
        if self.evidence_pattern:
            value["evidence_pattern"] = self.evidence_pattern
        if self.failure_pattern:
            value["failure_pattern"] = self.failure_pattern
        return value


@dataclass(frozen=True)
class BuildPlan:
    """Setup, build, optional target-test and required full-suite contract."""
    system: str
    setup: tuple[CommandSpec, ...]
    build: tuple[CommandSpec, ...]
    regression_test: tuple[CommandSpec, ...]
    target_test: tuple[CommandSpec, ...] = ()

    @property
    def test(self) -> tuple[CommandSpec, ...]:
        """Compatibility alias for callers that still refer to the full suite as test."""
        return self.regression_test

    def as_dict(self) -> dict:
        value = {
            "system": self.system,
            "setup": [item.as_dict() for item in self.setup],
            "build": [item.as_dict() for item in self.build],
            "regression_test": [item.as_dict() for item in self.regression_test],
        }
        if self.target_test:
            value["target_test"] = [item.as_dict() for item in self.target_test]
        return value


@dataclass
class CommandResult:
    label: str
    argv: list[str]
    cwd: str
    returncode: int
    output: str
    elapsed_seconds: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def as_dict(self, output_limit: int = 20_000) -> dict:
        output = self.output
        if len(output) > output_limit:
            output = f"...[truncated {len(output) - output_limit} chars]\n" + output[-output_limit:]
        return {
            "label": self.label,
            "argv": self.argv,
            "cwd": self.cwd,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "output": output,
        }


class BuildDetector:
    """Infer setup/build/test commands only from project-native files."""

    def __init__(self, jobs: int = 0):
        self.jobs = jobs or max(1, min(8, os.cpu_count() or 1))

    def detect(self, root: Path, config_path: Path | None = None) -> BuildPlan:
        root = root.resolve()
        configured = self._project_configuration(root, config_path)
        if configured:
            return configured

        cmake = self._cmake(root)
        if cmake:
            return cmake
        if (root / "meson.build").is_file():
            build_dir = ".debugging-framework/build"
            setup = ()
            if not (root / build_dir / "build.ninja").is_file():
                setup = (CommandSpec("meson-setup", ("meson", "setup", build_dir)),)
            return BuildPlan(
                "meson",
                setup,
                (CommandSpec("meson-build", ("meson", "compile", "-C", build_dir)),),
                (CommandSpec("meson-test", ("meson", "test", "-C", build_dir, "--print-errorlogs")),),
            )
        if any(
            (root / name).is_file()
            for name in ("WORKSPACE", "WORKSPACE.bazel", "MODULE.bazel")
        ):
            return BuildPlan(
                "bazel", (), (),
                (CommandSpec("bazel-test", ("bazel", "test", "//...")),),
            )
        make = self._make(root)
        if make:
            return make
        if (root / "build.ninja").is_file():
            return BuildPlan(
                "ninja", (),
                (CommandSpec("ninja-build", ("ninja", f"-j{self.jobs}")),),
                (CommandSpec("ninja-test", ("ninja", "test")),),
            )
        raise ValueError(
            "Không tự nhận diện được build/test workflow từ project. "
            "Có thể thêm .debugging-framework.json ngay tại project root cho build system đặc thù."
        )

    def _project_configuration(
        self, root: Path, config_path: Path | None = None
    ) -> BuildPlan | None:
        path, raw = (
            read_project_config_file(config_path)
            if config_path is not None
            else read_project_config_data(root)
        )
        if not raw:
            return None

        # A project may contain only runtime repair settings. In that case its
        # native build system must still be auto-detected rather than treating
        # the file as an incomplete custom test contract.
        command_fields = {"setup", "build", "test", "target_test", "regression_test"}
        if not any(field in raw for field in command_fields):
            return None

        def commands(phase: str) -> tuple[CommandSpec, ...]:
            value = raw.get(phase, [])
            if isinstance(value, (str, dict)):
                value = [value]
            if not isinstance(value, list):
                raise ValueError(f"{path}: field {phase} phải là command hoặc list")
            out = []
            for index, item in enumerate(value, start=1):
                cwd = "."
                command = item
                evidence_pattern = ""
                failure_pattern = ""
                if isinstance(item, dict):
                    command = item.get("command")
                    cwd = str(item.get("cwd") or ".")
                    evidence_pattern = str(item.get("evidence_pattern") or "").strip()
                    failure_pattern = str(item.get("failure_pattern") or "").strip()
                if isinstance(command, str):
                    argv = tuple(shlex.split(command))
                elif isinstance(command, list) and all(isinstance(arg, str) for arg in command):
                    argv = tuple(command)
                else:
                    raise ValueError(f"{path}: command {phase}[{index}] không hợp lệ")
                if not argv or Path(cwd).is_absolute() or ".." in Path(cwd).parts:
                    raise ValueError(f"{path}: command {phase}[{index}] không an toàn")
                if phase == "regression_test" and any(
                    "{test_id}" in argument for argument in argv
                ):
                    raise ValueError(
                        f"{path}: regression_test[{index}] phải chạy full suite, "
                        "không được chứa {test_id}"
                    )
                if evidence_pattern or failure_pattern:
                    if phase not in {"test", "target_test", "regression_test"}:
                        raise ValueError(
                            f"{path}: evidence/failure pattern chỉ dùng cho test command"
                        )
                    for field_name, pattern in (
                        ("evidence_pattern", evidence_pattern),
                        ("failure_pattern", failure_pattern),
                    ):
                        if not pattern:
                            continue
                        try:
                            re.compile(pattern)
                        except re.error as exc:
                            raise ValueError(
                                f"{path}: {field_name} test[{index}] không hợp lệ: {exc}"
                            ) from exc
                out.append(
                    CommandSpec(
                        f"{phase}-{index}", argv, cwd, evidence_pattern, failure_pattern
                    )
                )
            return tuple(out)

        declared_test = commands("test")
        target_test = commands("target_test")
        regression_test = commands("regression_test")
        schema_version = raw.get("schema_version")
        if schema_version == PROJECT_CONFIG_SCHEMA_VERSION:
            if declared_test:
                raise ValueError(
                    f"{path}: schema_version={PROJECT_CONFIG_SCHEMA_VERSION} "
                    "không dùng field test; "
                    "hãy khai báo full suite bằng regression_test"
                )
            if not regression_test:
                raise ValueError(
                    f"{path}: schema_version={PROJECT_CONFIG_SCHEMA_VERSION} "
                    "yêu cầu regression_test full suite"
                )
            effective_regression = regression_test
        else:
            # Keep inspect/validate compatible with older project-local files.
            # The public repair contract itself requires schema version 6.
            effective_regression = regression_test or declared_test
        plan = BuildPlan(
            str(raw.get("system") or "project-config"),
            commands("setup"),
            commands("build"),
            effective_regression,
            target_test,
        )
        if not plan.regression_test:
            raise ValueError(
                f"{path}: cần ít nhất một regression_test command"
            )
        return plan

    def _cmake(self, root: Path) -> BuildPlan | None:
        if not (root / "CMakeLists.txt").is_file():
            return None
        build_dir = self._existing_cmake_build(root) or Path(".debugging-framework/build")
        rel = build_dir.as_posix()
        setup = (
            CommandSpec(
                "cmake-configure",
                ("cmake", "-S", ".", "-B", rel, "-DBUILD_TESTING=ON", "-DCMAKE_BUILD_TYPE=Debug"),
            ),
        )
        return BuildPlan(
            "cmake", setup,
            (CommandSpec("cmake-build", ("cmake", "--build", rel, "--parallel", str(self.jobs))),),
            (CommandSpec("ctest", ("ctest", "--test-dir", rel, "--output-on-failure")),),
        )

    @staticmethod
    def _existing_cmake_build(root: Path) -> Path | None:
        candidates = []
        for child in root.iterdir():
            if child.is_dir() and (child / "CMakeCache.txt").is_file():
                candidates.append(child)
        for child in sorted(candidates, key=lambda item: (not item.name.startswith("build"), item.name)):
            try:
                cache = (child / "CMakeCache.txt").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            match = re.search(r"^CMAKE_HOME_DIRECTORY:INTERNAL=(.+)$", cache, re.MULTILINE)
            if match and Path(match.group(1)).resolve() == root:
                return child.relative_to(root)
        return None

    def _make(self, root: Path) -> BuildPlan | None:
        makefile = next((root / name for name in ("Makefile", "makefile", "GNUmakefile") if (root / name).is_file()), None)
        setup: tuple[CommandSpec, ...] = ()
        if makefile is None:
            if (root / "configure").is_file():
                setup = (CommandSpec("configure", ("sh", "./configure")),)
            elif (root / "configure.ac").is_file() or (root / "configure.in").is_file():
                setup = (
                    CommandSpec("autoreconf", ("autoreconf", "-fi")),
                    CommandSpec("configure", ("sh", "./configure")),
                )
            else:
                return None
        text = makefile.read_text(encoding="utf-8", errors="replace") if makefile else ""
        target = next(
            (name for name in ("test", "check", "tests", "check-local") if re.search(rf"(?m)^\s*{re.escape(name)}\s*:", text)),
            None,
        )
        if target is None and setup:
            # Automake guarantees the conventional `check` target after configure.
            target = "check"
        script = None
        if target is None:
            script = next(
                (
                    candidate
                    for candidate in (
                        "test.sh", "run_tests.sh", "run-tests.sh",
                        "tests/run.sh", "tests/run_tests.sh", "tests/run-tests.sh",
                    )
                    if (root / candidate).is_file()
                ),
                None,
            )
        if target is None and script is None:
            raise ValueError(
                "Make project không khai báo target test/check/tests và không có test runner chuẩn"
            )
        test_command = (
            CommandSpec("make-test", ("make", target))
            if target
            else CommandSpec("test-script", ("sh", f"./{script}"))
        )
        return BuildPlan(
            "autotools" if setup or (root / "configure").exists() else "make",
            setup,
            (CommandSpec("make-build", ("make", f"-j{self.jobs}")),),
            (test_command,),
        )

class ProjectValidator:
    """Run isolated validation on a project or an applied diff."""

    def __init__(
        self,
        *,
        command_timeout: int = 1800,
        jobs: int = 0,
        environment_backend: str,
        environment_runtime: str = "auto",
        environment_image: str = "",
    ):
        self.command_timeout = command_timeout
        self.detector = BuildDetector(jobs=jobs)
        if environment_backend not in {"host", "image"}:
            raise ValueError("environment mode chỉ hỗ trợ host hoặc image; không fallback")
        if environment_backend == "image" and not environment_image.strip():
            raise ValueError("environment_image là bắt buộc với mode image")
        if environment_backend == "host" and environment_image.strip():
            raise ValueError("environment_image chỉ được dùng với mode image")
        self.environment_backend = environment_backend
        self.environment_image = environment_image.strip()
        self.environment_runtime = OCIEnvironment(runtime=environment_runtime)
        self.environment_resolver = EnvironmentResolver()
        self._active_provision: OCIProvision | None = None
        self._active_backend = ""

    def resolve_environment(self, project: Project, plan: BuildPlan | None = None) -> EnvironmentSpec:
        plan = plan or self.inspect(project)
        return self.environment_resolver.resolve(
            project.path,
            plan.system,
            backend=self.environment_backend,
            image=self.environment_image,
        )

    @staticmethod
    def plan_digest(plan: BuildPlan) -> str:
        encoded = json.dumps(plan.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def inspect(self, project: Project) -> BuildPlan:
        plan = self.detector.detect(project.path, project.config_path)
        if not plan.regression_test:
            raise ValueError(
                f"Không tìm thấy regression_test full-suite command cho project {project.path}"
            )
        return plan

    def baseline(
        self,
        project: Project,
        artifact_dir: Path,
        *,
        failing_tests: tuple[str, ...] = (),
        expected_plan_digest: str = "",
        expected_environment_digest: str = "",
    ) -> dict:
        """Reproduce the supplied failing test on a clean project snapshot."""
        self._require_artifacts_outside_input(project, artifact_dir)
        with ValidationWorkspace(project) as workspace:
            snapshot_project = Project(
                path=workspace.path,
                project_id=project.project_id,
                config_path=project.config_path,
            )
            plan = self.inspect(snapshot_project)
            plan_digest = self.plan_digest(plan)
            if expected_plan_digest and plan_digest != expected_plan_digest:
                return self.invalid_snapshot("validation_plan_changed_before_baseline")
            environment = self.resolve_environment(snapshot_project, plan)
            if expected_environment_digest and environment.digest != expected_environment_digest:
                return self.invalid_snapshot("validation_environment_changed_before_baseline")
            provision = None
            try:
                provision = self._provision_environment(
                    environment,
                    artifact_dir / "environment",
                )
                self._active_provision = provision
                if failing_tests:
                    snapshot = self._run_target_baseline(
                        workspace.path, plan, artifact_dir, failing_tests
                    )
                else:
                    snapshot = self._run_plan(
                        workspace.path, plan, artifact_dir, prefix="baseline"
                    )
            except Exception as exc:
                snapshot = self.invalid_snapshot(
                    f"environment_provision_failed:{type(exc).__name__}:{exc}"
                )
            finally:
                self._active_provision = None
                self._active_backend = ""
        self._attach_environment_result(snapshot, environment, provision)
        snapshot["environment"] = environment.as_dict()
        snapshot["environment_digest"] = environment.digest
        snapshot["plan_digest"] = plan_digest
        snapshot["validation_workspace_isolated"] = True
        snapshot["input_project_untouched"] = True
        return snapshot

    def external_baseline(
        self,
        project: Project,
        artifact_dir: Path,
        *,
        failing_tests: tuple[str, ...],
        failure_output: str,
    ) -> dict:
        """Trust caller evidence without executing the original project.

        Repair validation remains target-first and runs the full regression suite
        only after the configured target tests pass.  Here we only freeze the
        declared build/environment contract and initial failing-test IDs.
        """
        self._require_artifacts_outside_input(project, artifact_dir)
        output = str(failure_output or "")
        if not output.strip():
            return self.invalid_snapshot("external_baseline_output_empty")
        plan = self.inspect(project)
        plan_digest = self.plan_digest(plan)
        environment = self.resolve_environment(project, plan)
        target_commands = self._target_commands(plan, failing_tests)
        failed_ids = self._canonical_test_ids(list(failing_tests))
        snapshot = {
            "status": "failing",
            "validation_error": "",
            "validation_executed": False,
            "setup_executed": False,
            "compile_executed": False,
            "tests_executed": False,
            "build_system": plan.system,
            "build_plan": plan.as_dict(),
            "setup_commands": [],
            "build_commands": [],
            "test_commands": [],
            "failed_test_ids": failed_ids,
            "passed_test_ids": [],
            "post_failed_tests": failed_ids,
            "post_passed_tests": [],
            "test_id_granularity": "test-case",
            "failure_output": output,
            "execution_output": output,
            "baseline_external": True,
            "baseline_source": "caller-log",
            "baseline_observed": True,
            "baseline_trust": "caller-supplied-log-and-test-ids",
            "baseline_reproduced": False,
            "baseline_executed": False,
            "baseline_suite_status": "not-run",
            "baseline_suite_output": "",
            "baseline_suite_failure_output": "",
            "test_scope": "caller",
            "target_tests": list(failing_tests),
            "target_commands": [item.as_dict() for item in target_commands],
            "target_selection_mode": (
                "configured-target" if target_commands else "full-suite-only"
            ),
            "target_executed": False,
            "target_status": "not-run",
            "target_passed": None,
            "regression_executed": False,
            "regression_status": "not-run",
            "regression_passed": None,
            "environment": environment.as_dict(),
            "environment_digest": environment.digest,
            "plan_digest": plan_digest,
            "validation_workspace_isolated": False,
            "validation_process_sandboxed": False,
            "input_project_untouched": True,
            "codex_snapshot_output_file": ".debugging-framework/baseline-output.txt",
        }
        self._attach_environment_result(snapshot, environment, None)
        return snapshot

    def validate_diff(
        self,
        *,
        project: Project,
        diff: str,
        patch_paths: list[str],
        artifact_dir: Path,
        expected_sha256s: dict[str, str | None] | None = None,
        failing_tests: tuple[str, ...] = (),
        expected_plan_digest: str = "",
        expected_environment_digest: str = "",
        expected_image_digest: str = "",
        reusable_workspace: ProjectWorkspace | None = None,
    ) -> dict:
        self._require_artifacts_outside_input(project, artifact_dir)
        normalized_paths = [normalize_relpath(path) for path in patch_paths]
        if not normalized_paths or any(not path for path in normalized_paths):
            return self.invalid_snapshot("patch_contains_unsafe_path")
        if len(set(normalized_paths)) != len(normalized_paths):
            return self.invalid_snapshot("patch_contains_duplicate_path")
        blocked_paths = non_repairable_patch_paths(normalized_paths)
        if blocked_paths:
            snapshot = self.invalid_snapshot(
                "test_oracle_modified_or_non_production_patch:"
                + ",".join(blocked_paths)
            )
            snapshot["test_oracle_modified"] = True
            snapshot["blocked_patch_paths"] = blocked_paths
            snapshot["patch_paths"] = normalized_paths
            return snapshot
        owner_root = project.path.resolve()
        for relpath, expected in (expected_sha256s or {}).items():
            normalized = normalize_relpath(relpath)
            if not normalized or normalized not in normalized_paths:
                return self.invalid_snapshot("patch_snapshot_hash_path_mismatch")
            entry = owner_root / normalized
            if entry.is_symlink():
                return self.invalid_snapshot(f"patch_targets_symlink:{normalized}")
            current = hashlib.sha256(entry.read_bytes()).hexdigest() if entry.is_file() else None
            if current != expected:
                return self.invalid_snapshot(
                    f"input_path_changed_since_llm_snapshot:{normalized}"
                )

        workspace_context = (
            nullcontext(reusable_workspace)
            if reusable_workspace is not None
            else ValidationWorkspace(project)
        )
        with workspace_context as workspace:
            if reusable_workspace is not None:
                workspace.reset_to_snapshot()
            snapshot_project = Project(
                path=workspace.path,
                project_id=project.project_id,
                config_path=project.config_path,
            )
            # Freeze the build/test contract from the clean snapshot. A patch
            # must not be able to select an easier test command by modifying
            # project configuration before auto-detection.
            plan = self.inspect(snapshot_project)
            plan_digest = self.plan_digest(plan)
            if expected_plan_digest and plan_digest != expected_plan_digest:
                return self.invalid_snapshot("validation_plan_changed_before_patch")
            environment = self.resolve_environment(snapshot_project, plan)
            if expected_environment_digest and environment.digest != expected_environment_digest:
                return self.invalid_snapshot("validation_environment_changed_before_patch")
            applied_paths = workspace.apply_unified_diff(diff, normalized_paths)
            provision = None
            try:
                provision = self._provision_environment(
                    environment,
                    artifact_dir / "environment",
                )
                if (
                    expected_image_digest
                    and provision is not None
                    and provision.image_digest != expected_image_digest
                ):
                    snapshot = self.invalid_snapshot("validation_environment_image_changed")
                else:
                    self._active_provision = provision
                    if failing_tests:
                        snapshot = self._run_target_and_regression(
                            workspace.path, plan, artifact_dir, failing_tests
                        )
                    else:
                        snapshot = self._run_plan(
                            workspace.path, plan, artifact_dir, prefix="patched"
                        )
            except Exception as exc:
                snapshot = self.invalid_snapshot(
                    f"environment_provision_failed:{type(exc).__name__}:{exc}"
                )
            finally:
                self._active_provision = None
                self._active_backend = ""
        snapshot["patch_paths"] = applied_paths
        self._attach_environment_result(snapshot, environment, provision)
        snapshot["environment"] = environment.as_dict()
        snapshot["environment_digest"] = environment.digest
        snapshot["plan_digest"] = plan_digest
        snapshot["validation_workspace_isolated"] = True
        snapshot["input_project_untouched"] = True
        return snapshot

    def invalid_snapshot(self, error: str) -> dict:
        return {
            "status": "invalid",
            "validation_error": error,
            "validation_executed": False,
            "build_system": "",
            "tests_executed": False,
            "failed_test_ids": [],
            "passed_test_ids": [],
            "test_id_granularity": "none",
            "target_tests": [],
            "target_commands": [],
            "target_passed": False,
            "environment_provisioned": False,
            "setup_executed": False,
            "validation_workspace_isolated": True,
            "validation_process_sandboxed": self._validation_process_sandboxed(),
            "input_project_untouched": True,
        }

    def _validation_process_sandboxed(self) -> bool:
        """Only the prebuilt image mode isolates validation from the host."""
        backend = self._active_backend or self.environment_backend
        return backend == "image" and self.environment_runtime.available

    def _provision_environment(
        self,
        spec: EnvironmentSpec,
        artifact_dir: Path,
    ):
        self._active_backend = spec.backend
        if spec.backend == "image":
            return self.environment_runtime.provision(spec, artifact_dir)
        if spec.backend == "host":
            return None
        raise RuntimeError(f"environment_mode_unsupported:{spec.backend}")

    @staticmethod
    def _attach_environment_result(snapshot: dict, spec: EnvironmentSpec, provision) -> None:
        snapshot["environment_backend"] = spec.backend
        snapshot["environment_ready"] = spec.backend == "host" or provision is not None
        if provision is not None:
            snapshot["environment_provisioned"] = True
            snapshot["provisioned_image"] = getattr(provision, "image", "")
            snapshot["provisioned_image_digest"] = getattr(provision, "image_digest", "")
        else:
            snapshot["environment_provisioned"] = False
            snapshot["provisioned_image"] = ""
            snapshot["provisioned_image_digest"] = ""

    def _run_target_baseline(
        self,
        root: Path,
        plan: BuildPlan,
        artifact_dir: Path,
        failing_tests: tuple[str, ...],
    ) -> dict:
        target_commands = self._target_commands(plan, failing_tests)
        active_commands = target_commands or plan.regression_test
        snapshot = self._run_plan(
            root,
            plan,
            artifact_dir,
            prefix="baseline-target",
            test_commands=active_commands,
            test_scope="target" if target_commands else "regression",
        )
        snapshot["target_tests"] = list(failing_tests)
        snapshot["target_commands"] = [item.as_dict() for item in target_commands]
        snapshot["target_selection_mode"] = (
            "configured-target" if target_commands else "full-suite-only"
        )
        snapshot["target_executed"] = bool(target_commands)
        output_path = artifact_dir / "baseline-output.txt"
        output_path.write_text(
            str(snapshot.get("execution_output") or ""),
            encoding="utf-8",
            errors="replace",
        )
        snapshot["baseline_output_artifact"] = str(output_path)
        if snapshot.get("status") == "failing":
            if not self._target_ids_verified(
                snapshot.get("failed_test_ids", []), failing_tests
            ):
                snapshot["status"] = "invalid"
                snapshot["validation_error"] = "baseline_target_test_unverified"
                snapshot["baseline_reproduced"] = False
                return snapshot
            snapshot["baseline_reproduced"] = True
            snapshot["validation_error"] = ""
            return snapshot
        if snapshot.get("status") == "plausible":
            snapshot["status"] = "invalid"
            snapshot["validation_error"] = "baseline_not_reproduced"
        elif snapshot.get("validation_error"):
            snapshot["validation_error"] = "baseline_" + str(snapshot["validation_error"])
        else:
            snapshot["validation_error"] = "baseline_not_reproduced"
        snapshot["baseline_reproduced"] = False
        return snapshot

    def _run_target_and_regression(
        self,
        root: Path,
        plan: BuildPlan,
        artifact_dir: Path,
        failing_tests: tuple[str, ...],
    ) -> dict:
        target_commands = self._target_commands(plan, failing_tests)
        if not target_commands:
            regression = self._run_plan(
                root,
                plan,
                artifact_dir,
                prefix="patched-regression",
                test_commands=plan.regression_test,
                test_scope="regression",
            )
            regression["target_tests"] = list(failing_tests)
            regression["target_commands"] = []
            regression["target_selection_mode"] = "full-suite-only"
            regression["target_executed"] = False
            regression["target_status"] = "not-run"
            regression["target_passed"] = None
            regression["regression_executed"] = bool(
                regression.get("tests_executed")
            )
            regression["regression_status"] = regression.get("status")
            regression["regression_passed"] = regression.get("status") == "plausible"
            return regression

        target = self._run_plan(
            root,
            plan,
            artifact_dir,
            prefix="patched-target",
            test_commands=target_commands,
            test_scope="target",
        )
        target["target_tests"] = list(failing_tests)
        target["target_commands"] = [item.as_dict() for item in target_commands]
        target["target_selection_mode"] = "configured-target"
        target["target_executed"] = True
        target["target_status"] = target.get("status")
        if target.get("status") not in {"plausible", "failing"}:
            target["status"] = "invalid"
            target["validation_error"] = (
                "target_test_invalid:" + str(target.get("validation_error") or "unknown")
            )
            return target

        matched_failed = self._matching_requested_ids(
            target.get("failed_test_ids", []), failing_tests
        )
        matched_passed = self._matching_requested_ids(
            target.get("passed_test_ids", []), failing_tests
        )
        observed_requested = matched_failed | matched_passed
        if observed_requested != set(failing_tests):
            target["status"] = "invalid"
            target["validation_error"] = "target_test_unverified"
            return target

        target_failed_ids = sorted(matched_failed)
        target_passed_ids = sorted(set(failing_tests) - matched_failed)

        if target_failed_ids:
            target["target_passed"] = False
            target["regression_executed"] = False
            target["regression_status"] = "not-run"
            target["regression_passed"] = None
            target["failed_test_ids"] = target_failed_ids
            target["passed_test_ids"] = target_passed_ids
            target["post_failed_tests"] = target_failed_ids
            target["post_passed_tests"] = target_passed_ids
            target["test_id_granularity"] = "test-case"
            return target

        regression = self._run_plan(
            root,
            plan,
            artifact_dir,
            prefix="patched-regression",
            test_commands=plan.regression_test,
            test_scope="regression",
        )
        regression["target_tests"] = list(failing_tests)
        regression["target_commands"] = [item.as_dict() for item in target_commands]
        regression["target_status"] = target.get("status")
        regression["target_passed"] = target.get("status") == "plausible"
        regression["target_selection_mode"] = "configured-target"
        regression["target_executed"] = True
        regression["target_output"] = "\n\n".join(
            item.get("output", "") for item in target.get("test_commands", [])
        )
        regression["regression_status"] = regression.get("status")
        regression["regression_passed"] = regression.get("status") == "plausible"
        regression["regression_executed"] = bool(regression.get("tests_executed"))
        if regression.get("status") not in {"plausible", "failing"}:
            regression["status"] = "invalid"
            regression["validation_error"] = (
                "regression_invalid:"
                + str(regression.get("validation_error") or "unknown")
            )
            return regression

        regression_failed_ids = self._canonical_test_ids(
            regression.get("failed_test_ids", [])
        )
        regression_passed_ids = self._canonical_test_ids(
            regression.get("passed_test_ids", [])
        )
        failed_ids = sorted(set(target_failed_ids) | set(regression_failed_ids))
        passed_ids = sorted(
            (set(target_passed_ids) | set(regression_passed_ids)) - set(failed_ids)
        )
        regression["status"] = "failing" if failed_ids else "plausible"
        regression["validation_error"] = ""
        regression["failed_test_ids"] = failed_ids
        regression["passed_test_ids"] = passed_ids
        regression["post_failed_tests"] = failed_ids
        regression["post_passed_tests"] = passed_ids
        regression["test_id_granularity"] = "test-case"
        return regression

    @classmethod
    def _canonical_test_ids(cls, observed: object) -> list[str]:
        values: set[str] = set()
        for raw in observed if isinstance(observed, list) else []:
            value = cls._canonical_test_id(str(raw))
            if not value:
                continue
            parts = value.split("::")
            if len(parts) == 2 and parts[0] == parts[1]:
                value = parts[0]
            values.add(value)
        return sorted(values)

    @staticmethod
    def _target_ids_verified(observed: object, requested: tuple[str, ...]) -> bool:
        values = {
            str(value).strip() for value in (observed if isinstance(observed, list) else [])
            if str(value).strip() and not str(value).startswith("command:")
        }
        if not values:
            return False
        return all(
            any(
                ProjectValidator._canonical_test_id(requested_id)
                == ProjectValidator._canonical_test_id(value)
                or ProjectValidator._canonical_test_id(requested_id).endswith(
                    ProjectValidator._canonical_test_id(value)
                )
                or ProjectValidator._canonical_test_id(value).endswith(
                    ProjectValidator._canonical_test_id(requested_id)
                )
                for value in values
            )
            for requested_id in requested
        )

    @classmethod
    def _matching_requested_ids(
        cls, observed: object, requested: tuple[str, ...]
    ) -> set[str]:
        values = [
            cls._canonical_test_id(str(value))
            for value in (observed if isinstance(observed, list) else [])
            if str(value).strip() and not str(value).startswith("command:")
        ]
        return {
            requested_id
            for requested_id in requested
            if any(
                cls._canonical_test_id(requested_id) == value
                or cls._canonical_test_id(requested_id).endswith(value)
                or value.endswith(cls._canonical_test_id(requested_id))
                for value in values
            )
        }

    @staticmethod
    def _canonical_test_id(value: str) -> str:
        return str(value).strip().replace("#", "::")

    @staticmethod
    def _target_commands(plan: BuildPlan, failing_tests: tuple[str, ...]) -> tuple[CommandSpec, ...]:
        """Resolve only the optional target command declared by the partner."""
        selectors = tuple(str(value).strip() for value in failing_tests if str(value).strip())
        if plan.target_test:
            commands: list[CommandSpec] = []
            for spec in plan.target_test:
                has_placeholder = any("{test_id}" in value for value in spec.argv)
                active_selectors = selectors if has_placeholder and selectors else ("",)
                for index, selector in enumerate(active_selectors, start=1):
                    argv = tuple(
                        value.replace("{test_id}", selector) for value in spec.argv
                    )
                    suffix = f"-{index}" if len(active_selectors) > 1 else ""
                    commands.append(CommandSpec(
                        label=f"target-{spec.label}{suffix}",
                        argv=argv,
                        cwd=spec.cwd,
                        evidence_pattern=spec.evidence_pattern,
                        failure_pattern=spec.failure_pattern,
                    ))
            return tuple(commands)
        return ()

    @staticmethod
    def _require_artifacts_outside_input(project: Project, artifact_dir: Path) -> None:
        owner = project.path.expanduser().resolve()
        target = artifact_dir.expanduser().resolve()
        try:
            target.relative_to(owner)
        except ValueError:
            return
        raise ValueError(f"Validation artifacts phải nằm ngoài input project: {target}")

    def _run_plan(
        self,
        root: Path,
        plan: BuildPlan,
        artifact_dir: Path,
        prefix: str,
        test_commands: Iterable[CommandSpec] | None = None,
        test_scope: str = "regression",
    ) -> dict:
        setup = self._run_commands(root, plan.setup, artifact_dir, f"{prefix}-setup")
        if not all(item.ok for item in setup):
            return self._snapshot(plan, setup, [], [], "invalid", "setup_failed")
        build = self._run_commands(root, plan.build, artifact_dir, f"{prefix}-build")
        if not all(item.ok for item in build):
            return self._snapshot(plan, setup, build, [], "invalid", "build_failed")
        active_tests = tuple(
            plan.regression_test if test_commands is None else test_commands
        )
        reports_before = self._test_report_state(root)
        tests = self._run_commands(root, active_tests, artifact_dir, f"{prefix}-test")
        if not tests:
            return self._snapshot(plan, setup, build, tests, "invalid", "no_test_command")
        report_summary = (
            self._structured_test_summary(root, reports_before)
            if len(active_tests) == 1 else None
        )
        test_case_results = self._test_case_results(root, reports_before, tests)

        def snapshot(status: str, error: str) -> dict:
            return self._snapshot(
                plan, setup, build, tests, status, error,
                test_case_results=test_case_results,
            )

        report_count = report_summary[0] if report_summary is not None else None
        report_failures = report_summary[1] if report_summary is not None else None
        no_test_outputs = [
            any(pattern.search(item.output) for pattern in ZERO_TEST_PATTERNS)
            for item in tests
        ]
        if no_test_outputs and (
            (len(tests) == 1 and no_test_outputs[0]) or all(no_test_outputs)
        ) or report_count == 0:
            return snapshot("invalid", "no_tests_discovered")
        if any(item.timed_out for item in tests):
            return snapshot("invalid", "test_timeout")
        if any(
            item.returncode == 127
            or "command not found" in item.output.lower()
            for item in tests
        ):
            return snapshot("invalid", "test_runner_unavailable")
        unverified = [
            result.label
            for spec, result in zip(active_tests, tests)
            if not self._has_test_execution_evidence(
                plan.system, spec, result, report_count if len(tests) == 1 else None
            )
        ]
        if unverified:
            return snapshot("invalid", "test_execution_unverified:" + ",".join(unverified))
        output_failure = [
            bool(
                (report_failures is not None and report_failures > 0)
                or self._output_reports_test_failure(spec, result)
            )
            for spec, result in zip(active_tests, tests)
        ]
        conflicts = [
            result.label
            for result, reports_failure in zip(tests, output_failure)
            if result.ok and reports_failure
        ]
        if conflicts:
            return snapshot("invalid", "test_status_output_conflict:" + ",".join(conflicts))
        unverified_failures = [
            result.label
            for result, reports_failure in zip(tests, output_failure)
            if not result.ok and not reports_failure
        ]
        if unverified_failures:
            return snapshot("invalid", "test_failure_unverified:" + ",".join(unverified_failures))
        tests_ok = all(item.ok for item in tests)
        status = "plausible" if tests_ok else "failing"
        value = snapshot(status, "")
        value["test_scope"] = test_scope
        value["test_commands"] = [item.as_dict() for item in active_tests]
        return value

    @staticmethod
    def _has_test_execution_evidence(
        system: str,
        spec: CommandSpec,
        result: CommandResult,
        report_count: int | None,
    ) -> bool:
        if spec.evidence_pattern:
            return bool(re.search(spec.evidence_pattern, result.output, re.MULTILINE))
        if system.strip().lower() not in BUILTIN_TEST_SYSTEMS:
            return False
        if report_count is not None and report_count > 0:
            return True
        return any(pattern.search(result.output) for pattern in TEST_EXECUTION_PATTERNS)

    @staticmethod
    def _output_reports_test_failure(spec: CommandSpec, result: CommandResult) -> bool:
        if spec.failure_pattern:
            return bool(re.search(spec.failure_pattern, result.output, re.MULTILINE))
        return any(pattern.search(result.output) for pattern in TEST_FAILURE_PATTERNS)

    @staticmethod
    def _test_report_paths(root: Path) -> set[Path]:
        candidates: set[Path] = set()
        for pattern in (
            "**/surefire-reports/TEST-*.xml",
            "**/failsafe-reports/TEST-*.xml",
            "**/test-results/**/*.xml",
            "**/TestResults/**/*.trx",
            ".debugging-framework/*.xml",
        ):
            candidates.update(path for path in root.glob(pattern) if path.is_file())
        return candidates

    @classmethod
    def _test_report_state(cls, root: Path) -> dict[Path, tuple[int, int]]:
        state: dict[Path, tuple[int, int]] = {}
        for path in cls._test_report_paths(root):
            try:
                stat = path.stat()
            except OSError:
                continue
            state[path] = (stat.st_mtime_ns, stat.st_size)
        return state

    @classmethod
    def _changed_test_reports(
        cls, root: Path, before: dict[Path, tuple[int, int]]
    ) -> set[Path]:
        changed = set()
        for path in cls._test_report_paths(root):
            try:
                stat = path.stat()
            except OSError:
                continue
            if before.get(path) != (stat.st_mtime_ns, stat.st_size):
                changed.add(path)
        return changed

    @classmethod
    def _structured_test_summary(
        cls, root: Path, before: dict[Path, tuple[int, int]] | None = None
    ) -> tuple[int, int] | None:
        candidates = cls._changed_test_reports(root, before or {})
        if not candidates:
            return None
        total = 0
        failures = 0
        parsed = False
        for path in candidates:
            try:
                tree = ET.parse(path)
            except (ET.ParseError, OSError):
                continue
            parsed = True
            document = tree.getroot()
            count_found = False
            for node in document.iter():
                local_name = node.tag.rsplit("}", 1)[-1]
                if local_name in {"testsuite", "testsuites"} and "tests" in node.attrib:
                    try:
                        total += int(node.attrib["tests"])
                    except ValueError:
                        pass
                    for attribute in ("failures", "errors"):
                        try:
                            failures += int(node.attrib.get(attribute, "0"))
                        except ValueError:
                            pass
                    count_found = True
                    break
            if not count_found:
                test_nodes = [
                    node for node in document.iter()
                    if node.tag.rsplit("}", 1)[-1] in {"testcase", "UnitTestResult"}
                ]
                total += len(test_nodes)
                for node in test_nodes:
                    local_name = node.tag.rsplit("}", 1)[-1]
                    if local_name == "UnitTestResult":
                        if str(node.attrib.get("outcome") or "").lower() not in {
                            "passed", "completed"
                        }:
                            failures += 1
                    elif any(
                        child.tag.rsplit("}", 1)[-1] in {"failure", "error"}
                        for child in node
                    ):
                        failures += 1
        return (total, failures) if parsed else None

    @classmethod
    def _test_case_results(
        cls,
        root: Path,
        before: dict[Path, tuple[int, int]],
        commands: list[CommandResult],
    ) -> dict[str, bool]:
        cases: dict[str, bool] = {}
        for path in cls._changed_test_reports(root, before):
            try:
                document = ET.parse(path).getroot()
            except (ET.ParseError, OSError):
                continue
            for node in document.iter():
                local_name = node.tag.rsplit("}", 1)[-1]
                if local_name == "testcase":
                    name = str(node.attrib.get("name") or "").strip()
                    scope = str(
                        node.attrib.get("classname") or node.attrib.get("file") or ""
                    ).strip()
                    test_id = f"{scope}::{name}" if scope and name else name
                    if not test_id or any(
                        child.tag.rsplit("}", 1)[-1] == "skipped" for child in node
                    ):
                        continue
                    passed = not any(
                        child.tag.rsplit("}", 1)[-1] in {"failure", "error"}
                        for child in node
                    )
                    cases[test_id] = cases.get(test_id, True) and passed
                elif local_name == "UnitTestResult":
                    test_id = str(
                        node.attrib.get("testName") or node.attrib.get("testId") or ""
                    ).strip()
                    outcome = str(node.attrib.get("outcome") or "").strip().lower()
                    if test_id and outcome:
                        cases[test_id] = cases.get(test_id, True) and outcome in {
                            "passed", "completed"
                        }

        output_ids_found = False
        for command in commands:
            extracted = cls._failed_test_ids_from_output(command.output)
            if extracted:
                output_ids_found = True
                for test_id in extracted:
                    cases[test_id] = False
            passed = cls._passed_test_ids_from_output(command.output)
            if passed:
                output_ids_found = True
                for test_id in passed:
                    cases[test_id] = cases.get(test_id, True) and True
        if not any(not passed for passed in cases.values()) and not output_ids_found:
            for command in commands:
                if not command.ok:
                    cases[f"command:{command.label}"] = False
        return cases

    @staticmethod
    def _failed_test_ids_from_output(output: str) -> set[str]:
        failed: set[str] = set()
        patterns = (
            r"(?m)^FAILED\s+([^\s]+)",
            r"(?m)^test\s+(.+?)\s+\.\.\.\s+FAILED\s*$",
            r"(?m)^\s*\d+/\d+\s+Test\s+#\d+:\s+(.+?)\s+\.{2,}\*{3}Failed",
            r"(?m)^\s*---\s+FAIL:\s+(\S+)",
        )
        for pattern in patterns:
            failed.update(
                match.strip() for match in re.findall(pattern, output) if match.strip()
            )
        for line in output.splitlines():
            if not line.lstrip().startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(event, dict)
                and event.get("Action") == "fail"
                and str(event.get("Test") or "").strip()
            ):
                package = str(event.get("Package") or "").strip()
                name = str(event["Test"]).strip()
                failed.add(f"{package}::{name}" if package else name)
        return failed

    @staticmethod
    def _passed_test_ids_from_output(output: str) -> set[str]:
        passed: set[str] = set()
        for pattern in (
            r"(?m)^PASSED\s+([^\s]+)",
            r"(?m)^test\s+(.+?)\s+\.\.\.\s+PASSED\s*$",
            r"(?m)^\s*---\s+PASS:\s+(\S+)",
            r"(?m)^\s*test\s+(.+?)\s+\.\.\.\s+ok\s*$",
            r"(?m)^\s*\d+/\d+\s+Test\s+#\d+:\s+(.+?)\s+\.{2,}Passed",
        ):
            passed.update(
                match.strip() for match in re.findall(pattern, output) if match.strip()
            )
        return passed

    def _snapshot(
        self,
        plan: BuildPlan,
        setup: list[CommandResult],
        build: list[CommandResult],
        tests: list[CommandResult],
        status: str,
        error: str,
        test_case_results: dict[str, bool] | None = None,
    ) -> dict:
        test_case_results = test_case_results or {}
        return {
            "status": status,
            "validation_error": error,
            "validation_executed": bool(setup or build or tests),
            "setup_executed": bool(setup),
            "environment_provisioned": all(item.ok for item in setup),
            "compile_executed": bool(build),
            "tests_executed": bool(tests),
            "build_system": plan.system,
            "build_plan": plan.as_dict(),
            "setup_commands": [item.as_dict() for item in setup],
            "build_commands": [item.as_dict() for item in build],
            "test_commands": [item.as_dict() for item in tests],
            "post_passed_tests": [item.label for item in tests if item.ok],
            "post_failed_tests": [item.label for item in tests if not item.ok],
            "failed_test_ids": sorted(
                test_id for test_id, passed in test_case_results.items() if not passed
            ),
            "passed_test_ids": sorted(
                test_id for test_id, passed in test_case_results.items() if passed
            ),
            "test_id_granularity": (
                "test-case"
                if any(not test_id.startswith("command:") for test_id in test_case_results)
                else "command"
                if test_case_results
                else "none"
            ),
            "failure_output": "\n\n".join(item.output[-20_000:] for item in tests if not item.ok),
            "execution_output": self._execution_output(setup, build, tests),
            "validation_process_sandboxed": self._validation_process_sandboxed(),
        }

    @staticmethod
    def _execution_output(
        setup: list[CommandResult],
        build: list[CommandResult],
        tests: list[CommandResult],
    ) -> str:
        sections: list[str] = []
        for phase, commands in (("setup", setup), ("build", build), ("test", tests)):
            for result in commands:
                sections.append(
                    f"===== {phase}:{result.label} "
                    f"(returncode={result.returncode}, timed_out={result.timed_out}) =====\n"
                    f"{result.output or ''}"
                )
        return "\n\n".join(sections)

    def _run_commands(
        self,
        root: Path,
        commands: Iterable[CommandSpec],
        artifact_dir: Path,
        log_prefix: str,
    ) -> list[CommandResult]:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        results = []
        for index, spec in enumerate(commands, start=1):
            cwd = (root / spec.cwd).resolve()
            try:
                cwd.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"Command cwd thoát khỏi project: {spec.cwd}") from exc
            if not cwd.is_dir():
                result = CommandResult(spec.label, list(spec.argv), str(cwd), 127, "cwd_missing", 0.0)
            else:
                result = self._run_one(spec, cwd, root)
            results.append(result)
            log = artifact_dir / f"{log_prefix}-{index:02d}-{_safe_label(spec.label)}.log"
            log.write_text(result.output, encoding="utf-8", errors="replace")
            if not result.ok:
                break
        return results

    def _run_one(
        self,
        spec: CommandSpec,
        cwd: Path,
        root: Path,
    ) -> CommandResult:
        started = time.monotonic()
        command = self._execution_command(spec.argv, root, cwd)
        try:
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", start_new_session=True,
                cwd=str(cwd),
            )
            try:
                output, _ = process.communicate(timeout=self.command_timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=5)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                output, _ = process.communicate()
        except OSError as exc:
            return CommandResult(
                spec.label, list(spec.argv), str(cwd), 127, f"command_start_error:{exc}",
                time.monotonic() - started,
            )
        return CommandResult(
            spec.label, list(spec.argv), str(cwd), process.returncode if not timed_out else 124,
            output or "", time.monotonic() - started, timed_out,
        )

    def _execution_command(
        self,
        argv: tuple[str, ...],
        root: Path,
        cwd: Path,
    ) -> list[str]:
        if isinstance(self._active_provision, OCIProvision):
            return self.environment_runtime.command(
                self._active_provision,
                root,
                argv,
                cwd,
            )
        if self._active_backend == "host":
            return list(argv)
        raise RuntimeError(
            f"environment_not_ready: mode={self._active_backend or self.environment_backend}"
        )

def _safe_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "command"
