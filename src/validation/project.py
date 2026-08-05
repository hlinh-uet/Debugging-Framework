from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from src.loaders.project import Project
from src.utils.workspace import ValidationWorkspace, normalize_relpath


BUILTIN_TEST_SYSTEMS = {
    "autotools", "bazel", "cargo", "cmake", "composer", "dotnet",
    "gradle", "go", "make", "maven", "meson", "ninja", "node",
    "python", "ruby", "swift", "defects4c-rendered-recipe",
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
    )
)

TEST_FAILURE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in (
        r"\b[1-9]\d*\s+failed\b",
        r"\btest result:\s*failed\b",
        r"\btests? failed\b",
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
    system: str
    setup: tuple[CommandSpec, ...]
    build: tuple[CommandSpec, ...]
    test: tuple[CommandSpec, ...]

    def as_dict(self) -> dict:
        return {
            "system": self.system,
            "setup": [item.as_dict() for item in self.setup],
            "build": [item.as_dict() for item in self.build],
            "test": [item.as_dict() for item in self.test],
        }


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

    def detect(self, root: Path) -> BuildPlan:
        root = root.resolve()
        configured = self._project_configuration(root)
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
        if (root / "Cargo.toml").is_file():
            fetch = ("cargo", "fetch", "--locked") if (root / "Cargo.lock").is_file() else ("cargo", "fetch")
            return BuildPlan(
                "cargo", (CommandSpec("cargo-fetch", fetch),),
                (CommandSpec("cargo-build", ("cargo", "build", "--all-targets")),),
                (CommandSpec("cargo-test", ("cargo", "test", "--all-targets")),),
            )
        if (root / "go.mod").is_file():
            return BuildPlan(
                "go", (CommandSpec("go-mod-download", ("go", "mod", "download")),),
                (CommandSpec("go-build", ("go", "build", "./...")),),
                (CommandSpec("go-test", ("go", "test", "./...")),),
            )
        if (root / "pom.xml").is_file():
            mvn = "./mvnw" if (root / "mvnw").is_file() else "mvn"
            return BuildPlan(
                "maven",
                (CommandSpec("maven-resolve", (mvn, "-q", "-DskipTests", "dependency:go-offline")),),
                (CommandSpec("maven-build", (mvn, "-q", "-DskipTests", "package")),),
                (CommandSpec("maven-test", (mvn, "-q", "test")),),
            )
        if any((root / name).is_file() for name in ("build.gradle", "build.gradle.kts", "gradlew")):
            gradle = "./gradlew" if (root / "gradlew").is_file() else "gradle"
            return BuildPlan(
                "gradle", (),
                (CommandSpec("gradle-build", (gradle, "assemble")),),
                (CommandSpec("gradle-test", (gradle, "test")),),
            )
        if (root / "package.json").is_file():
            return self._node(root)
        if any(
            (root / name).is_file()
            for name in (
                "pyproject.toml", "pytest.ini", "setup.cfg", "setup.py",
                "requirements.txt", "requirements-dev.txt", "requirements-test.txt",
            )
        ):
            return self._python(root)
        if (root / "Package.swift").is_file():
            return BuildPlan(
                "swift", (CommandSpec("swift-resolve", ("swift", "package", "resolve")),),
                (CommandSpec("swift-build", ("swift", "build")),),
                (CommandSpec("swift-test", ("swift", "test")),),
            )
        if (root / "Gemfile").is_file():
            return BuildPlan(
                "ruby",
                (CommandSpec("bundle-install", ("bundle", "install")),),
                (),
                (CommandSpec("ruby-test", ("bundle", "exec", "rake", "test")),),
            )
        if (root / "composer.json").is_file():
            return BuildPlan(
                "composer",
                (
                    CommandSpec(
                        "composer-install",
                        ("composer", "install", "--no-interaction", "--prefer-dist"),
                    ),
                ),
                (),
                (CommandSpec("composer-test", ("composer", "test")),),
            )
        if (root / "WORKSPACE").is_file() or (root / "MODULE.bazel").is_file():
            return BuildPlan(
                "bazel", (), (),
                (CommandSpec("bazel-test", ("bazel", "test", "//...")),),
            )
        dotnet = sorted(root.glob("*.sln")) or sorted(root.glob("*.csproj"))
        if dotnet:
            target = dotnet[0].name
            return BuildPlan(
                "dotnet", (CommandSpec("dotnet-restore", ("dotnet", "restore", target)),),
                (CommandSpec("dotnet-build", ("dotnet", "build", target, "--no-restore")),),
                (CommandSpec("dotnet-test", ("dotnet", "test", target, "--no-build")),),
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

    def _project_configuration(self, root: Path) -> BuildPlan | None:
        path = root / ".debugging-framework.json"
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cấu hình project không hợp lệ {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"Cấu hình project phải là JSON object: {path}")

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
                if evidence_pattern or failure_pattern:
                    if phase != "test":
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

        plan = BuildPlan(
            str(raw.get("system") or "project-config"),
            commands("setup"), commands("build"), commands("test"),
        )
        if not plan.test:
            raise ValueError(f"{path}: cần ít nhất một test command")
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

    @staticmethod
    def _node(root: Path) -> BuildPlan:
        try:
            package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"package.json không hợp lệ: {exc}") from exc
        scripts = package.get("scripts") if isinstance(package, dict) else {}
        scripts = scripts if isinstance(scripts, dict) else {}
        manager = "npm"
        if (root / "pnpm-lock.yaml").is_file():
            manager = "pnpm"
        elif (root / "yarn.lock").is_file():
            manager = "yarn"
        elif isinstance(package, dict):
            declared = str(package.get("packageManager") or "").split("@", 1)[0]
            if declared in {"npm", "pnpm", "yarn"}:
                manager = declared
        if manager == "pnpm":
            install = (
                ("pnpm", "install", "--frozen-lockfile")
                if (root / "pnpm-lock.yaml").is_file()
                else ("pnpm", "install")
            )
        elif manager == "yarn":
            install = (
                ("yarn", "install", "--frozen-lockfile")
                if (root / "yarn.lock").is_file()
                else ("yarn", "install")
            )
        elif (root / "package-lock.json").is_file() or (root / "npm-shrinkwrap.json").is_file():
            install = ("npm", "ci")
        else:
            install = ("npm", "install")
        build = ()
        if "build" in scripts:
            build = (CommandSpec("node-build", (manager, "run", "build")),)
        if "test" not in scripts:
            raise ValueError("package.json không có scripts.test để validation tự động")
        return BuildPlan(
            "node",
            (CommandSpec("node-install", install),),
            build,
            (CommandSpec("node-test", (manager, "test")),),
        )

    @staticmethod
    def _python(root: Path) -> BuildPlan:
        environment = ".debugging-framework/venv"
        python = f"{environment}/bin/python"
        requirements = [
            name
            for name in (
                "requirements.txt", "requirements-dev.txt", "requirements-test.txt",
                "dev-requirements.txt", "requirements/dev.txt", "requirements/test.txt",
            )
            if (root / name).is_file()
        ]
        install_args: list[str] = [python, "-m", "pip", "install", "--disable-pip-version-check"]
        for requirement in requirements:
            install_args.extend(("-r", requirement))
        if BuildDetector._python_is_installable(root):
            install_args.extend(("-e", ".[test]"))
        install_args.append("pytest")
        return BuildPlan(
            "python",
            (
                CommandSpec("python-venv", (sys.executable, "-m", "venv", environment)),
                CommandSpec("python-install", tuple(install_args)),
            ),
            (),
            (CommandSpec("pytest", (python, "-m", "pytest", "-q")),),
        )

    @staticmethod
    def _python_is_installable(root: Path) -> bool:
        if (root / "setup.py").is_file():
            return True
        setup_cfg = root / "setup.cfg"
        if setup_cfg.is_file() and re.search(
            r"(?m)^\s*\[(?:metadata|options)\]\s*(?:#.*)?$",
            setup_cfg.read_text(encoding="utf-8", errors="replace"),
        ):
            return True
        pyproject = root / "pyproject.toml"
        if not pyproject.is_file():
            return False
        text = pyproject.read_text(encoding="utf-8", errors="replace")
        return bool(
            re.search(
                r"(?m)^\s*\[(?:build-system|project|tool\.(?:poetry|pdm|hatch))\]\s*(?:#.*)?$",
                text,
            )
        )


class ProjectValidator:
    """Run isolated validation on a project or an applied diff."""

    def __init__(
        self,
        *,
        command_timeout: int = 1800,
        jobs: int = 0,
        sandbox_executable: str = "bwrap",
    ):
        self.command_timeout = command_timeout
        self.detector = BuildDetector(jobs=jobs)
        self.sandbox_executable = shutil.which(sandbox_executable)

    def inspect(self, project: Project) -> BuildPlan:
        plan = self.detector.detect(project.path)
        if not plan.test:
            raise ValueError(f"Không tìm thấy test command cho project {project.path}")
        return plan

    def baseline(self, project: Project, artifact_dir: Path) -> dict:
        """Run an explicit clean-project diagnostic; the APR pipeline does not call this."""
        self._require_artifacts_outside_input(project, artifact_dir)
        if not self.sandbox_executable:
            return self.invalid_snapshot("validation_sandbox_unavailable:bwrap")
        with ValidationWorkspace(project) as workspace:
            snapshot_project = Project(path=workspace.path, project_id=project.project_id)
            plan = self.inspect(snapshot_project)
            snapshot = self._run_plan(
                workspace.path, plan, artifact_dir, prefix="baseline"
            )
        snapshot["validation_workspace_isolated"] = True
        snapshot["input_project_untouched"] = True
        return snapshot

    def validate_diff(
        self,
        *,
        project: Project,
        diff: str,
        patch_paths: list[str],
        artifact_dir: Path,
        expected_sha256s: dict[str, str | None] | None = None,
    ) -> dict:
        self._require_artifacts_outside_input(project, artifact_dir)
        if not self.sandbox_executable:
            return self.invalid_snapshot("validation_sandbox_unavailable:bwrap")
        normalized_paths = [normalize_relpath(path) for path in patch_paths]
        if not normalized_paths or any(not path for path in normalized_paths):
            return self.invalid_snapshot("patch_contains_unsafe_path")
        if len(set(normalized_paths)) != len(normalized_paths):
            return self.invalid_snapshot("patch_contains_duplicate_path")
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

        with ValidationWorkspace(project) as workspace:
            snapshot_project = Project(path=workspace.path, project_id=project.project_id)
            # Freeze the build/test contract from the clean snapshot. A patch
            # must not be able to select an easier test command by modifying
            # project configuration before auto-detection.
            plan = self.inspect(snapshot_project)
            applied_paths = workspace.apply_unified_diff(diff, normalized_paths)
            snapshot = self._run_plan(
                workspace.path, plan, artifact_dir, prefix="patched"
            )
        snapshot["patch_paths"] = applied_paths
        snapshot["validation_workspace_isolated"] = True
        snapshot["input_project_untouched"] = True
        return snapshot

    def invalid_snapshot(self, error: str) -> dict:
        return {
            "status": "invalid",
            "validation_error": error,
            "build_system": "",
            "tests_executed": False,
            "failed_test_ids": [],
            "passed_test_ids": [],
            "test_id_granularity": "none",
            "environment_provisioned": False,
            "setup_executed": False,
            "validation_workspace_isolated": True,
            "validation_process_sandboxed": bool(self.sandbox_executable),
            "input_project_untouched": True,
        }

    @staticmethod
    def _require_artifacts_outside_input(project: Project, artifact_dir: Path) -> None:
        owner = project.path.expanduser().resolve()
        target = artifact_dir.expanduser().resolve()
        try:
            target.relative_to(owner)
        except ValueError:
            return
        raise ValueError(f"Validation artifacts phải nằm ngoài input project: {target}")

    def _run_plan(self, root: Path, plan: BuildPlan, artifact_dir: Path, prefix: str) -> dict:
        setup = self._run_commands(root, plan.setup, artifact_dir, f"{prefix}-setup")
        if not all(item.ok for item in setup):
            return self._snapshot(plan, setup, [], [], "invalid", "setup_failed")
        build = self._run_commands(root, plan.build, artifact_dir, f"{prefix}-build")
        if not all(item.ok for item in build):
            return self._snapshot(plan, setup, build, [], "invalid", "build_failed")
        reports_before = self._test_report_state(root)
        tests = self._run_commands(root, plan.test, artifact_dir, f"{prefix}-test")
        if not tests:
            return self._snapshot(plan, setup, build, tests, "invalid", "no_test_command")
        report_summary = (
            self._structured_test_summary(root, reports_before)
            if len(plan.test) == 1 else None
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
            or "No module named pytest" in item.output
            or "command not found" in item.output.lower()
            for item in tests
        ):
            return snapshot("invalid", "test_runner_unavailable")
        unverified = [
            result.label
            for spec, result in zip(plan.test, tests)
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
            for spec, result in zip(plan.test, tests)
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
        return snapshot(status, "")

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
            "validation_process_sandboxed": True,
        }

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

    def _run_one(self, spec: CommandSpec, cwd: Path, root: Path) -> CommandResult:
        started = time.monotonic()
        command = self._sandboxed_command(spec.argv, root, cwd)
        try:
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", start_new_session=True,
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

    def _sandboxed_command(
        self, argv: tuple[str, ...], root: Path, cwd: Path
    ) -> list[str]:
        if not self.sandbox_executable:
            raise RuntimeError("validation_sandbox_unavailable:bwrap")
        root = root.resolve()
        cwd = cwd.resolve()
        try:
            cwd.relative_to(root)
            root.relative_to(Path("/tmp"))
        except ValueError as exc:
            raise ValueError("Validation workspace phải nằm dưới /tmp") from exc

        # Make the host filesystem read-only, replace /tmp with a private tmpfs,
        # then mount only this disposable validation snapshot read-write. This
        # prevents project-controlled build/test commands from writing back to
        # the input checkout through absolute paths.
        destination_dirs: list[str] = []
        current = Path("/tmp")
        for part in root.relative_to(Path("/tmp")).parts:
            current /= part
            destination_dirs.extend(["--dir", str(current)])
        environment_root = root / ".debugging-framework" / "environment"
        cache_root = environment_root / "cache"
        for directory in (
            cache_root / "pip",
            cache_root / "npm",
            cache_root / "yarn",
            cache_root / "xdg",
            environment_root / "gradle",
            environment_root / "cargo",
            environment_root / "go-mod",
            environment_root / "go-build",
            environment_root / "maven",
            environment_root / "composer",
            environment_root / "bundle",
            environment_root / "nuget",
            environment_root / "dotnet-home",
        ):
            directory.mkdir(parents=True, exist_ok=True)
        maven_options = " ".join(
            value
            for value in (
                os.environ.get("MAVEN_OPTS", "").strip(),
                f"-Dmaven.repo.local={environment_root / 'maven'}",
            )
            if value
        )
        return [
            self.sandbox_executable,
            "--die-with-parent",
            "--new-session",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            *destination_dirs,
            "--bind",
            str(root),
            str(root),
            "--chdir",
            str(cwd),
            "--setenv",
            "TMPDIR",
            "/tmp",
            "--setenv",
            "PWD",
            str(cwd),
            "--setenv",
            "PIP_CACHE_DIR",
            str(cache_root / "pip"),
            "--setenv",
            "npm_config_cache",
            str(cache_root / "npm"),
            "--setenv",
            "YARN_CACHE_FOLDER",
            str(cache_root / "yarn"),
            "--setenv",
            "XDG_CACHE_HOME",
            str(cache_root / "xdg"),
            "--setenv",
            "GRADLE_USER_HOME",
            str(environment_root / "gradle"),
            "--setenv",
            "CARGO_HOME",
            str(environment_root / "cargo"),
            "--setenv",
            "GOMODCACHE",
            str(environment_root / "go-mod"),
            "--setenv",
            "GOCACHE",
            str(environment_root / "go-build"),
            "--setenv",
            "MAVEN_OPTS",
            maven_options,
            "--setenv",
            "COMPOSER_HOME",
            str(environment_root / "composer"),
            "--setenv",
            "COMPOSER_CACHE_DIR",
            str(cache_root / "composer"),
            "--setenv",
            "BUNDLE_PATH",
            str(environment_root / "bundle"),
            "--setenv",
            "NUGET_PACKAGES",
            str(environment_root / "nuget"),
            "--setenv",
            "DOTNET_CLI_HOME",
            str(environment_root / "dotnet-home"),
            *argv,
        ]

def _safe_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "command"
