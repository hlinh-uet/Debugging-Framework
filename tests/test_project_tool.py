from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import src.cli as cli
from src.cli import build_parser, load_batch_manifest
from src.core.codex_runner import CodexRunResult, CodexRunner
from src.core.pipeline import (
    DebuggingPipeline,
    PipelineOptions,
    classify_patch_outcome,
    classify_validation_result,
)
from src.loaders.project import ProjectLoader
from src.utils.config import DEFAULT_RESULTS_DIR, FrameworkConfig
from src.utils.workspace import ProjectWorkspace, is_production_source_path
from src.validation.project import BuildDetector, ProjectValidator


def _python_project(root: Path) -> Path:
    root.mkdir()
    (root / "pyproject.toml").write_text(
        "[build-system]\nrequires = ['setuptools']\nbuild-backend = 'setuptools.build_meta'\n",
        encoding="utf-8",
    )
    (root / "src").mkdir()
    (root / "src" / "value.py").write_text(
        "def value():\n    return 0\n", encoding="utf-8"
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_value.py").write_text(
        "from src.value import value\n\ndef test_value():\n    assert value() == 1\n",
        encoding="utf-8",
    )
    return root


def _custom_project(root: Path) -> Path:
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "value.py").write_text(
        "def value():\n    return 0\n", encoding="utf-8"
    )
    test_code = (
        "from pathlib import Path; "
        "ok = 'return 1' in Path('src/value.py').read_text(); "
        "print('1 passed' if ok else '1 failed'); "
        "raise SystemExit(0 if ok else 1)"
    )
    (root / ".debugging-framework.json").write_text(
        json.dumps(
            {
                "system": "custom",
                "test": [
                    {
                        "command": [sys.executable, "-c", test_code],
                        "evidence_pattern": "1 (?:passed|failed)",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return root


def test_loader_takes_a_direct_project_path(tmp_path: Path):
    root = _python_project(tmp_path / "input-project")
    project = ProjectLoader().load(root)
    assert project.path == root.resolve()
    assert project.project_id == "input-project"


def test_installed_default_results_directory_is_not_package_or_cwd_relative(tmp_path: Path):
    config = FrameworkConfig.load(tmp_path / "missing.env", environ={})
    assert config.results_dir == DEFAULT_RESULTS_DIR.resolve()
    assert config.results_dir.is_absolute()


def test_codex_runner_uses_writable_disposable_workspace(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    schema = Path(__file__).resolve().parents[1] / "src" / "schemas" / "codex_result.schema.json"
    result = CodexRunner(
        executable="/usr/bin/false",
        schema_path=schema,
        timeout_seconds=5,
    ).run(workspace=workspace, prompt="test", artifact_dir=tmp_path / "runner-artifacts")
    sandbox_index = result.command.index("--sandbox")
    assert result.command[sandbox_index + 1] == "workspace-write"
    assert "sandbox_workspace_write.network_access=true" in result.command
    assert "approval_policy=\"never\"" in result.command


def test_build_detector_uses_project_conventions(tmp_path: Path):
    root = _python_project(tmp_path / "python-project")
    plan = BuildDetector().detect(root)
    assert plan.system == "python"
    assert [command.label for command in plan.setup] == ["python-venv", "python-install"]
    assert plan.setup[0].argv[-1] == ".debugging-framework/venv"
    assert plan.test[0].argv[0] == ".debugging-framework/venv/bin/python"
    assert plan.test[0].argv[1:] == ("-m", "pytest", "-q")


def test_detector_provisions_lockfile_aware_project_dependencies(tmp_path: Path):
    node = tmp_path / "node-project"
    node.mkdir()
    (node / "package.json").write_text(
        json.dumps({"scripts": {"test": "node --test"}}), encoding="utf-8"
    )
    (node / "package-lock.json").write_text("{}\n", encoding="utf-8")
    node_plan = BuildDetector().detect(node)
    assert node_plan.setup[0].argv == ("npm", "ci")

    cargo = tmp_path / "cargo-project"
    cargo.mkdir()
    (cargo / "Cargo.toml").write_text(
        "[package]\nname='sample'\nversion='0.1.0'\n", encoding="utf-8"
    )
    (cargo / "Cargo.lock").write_text("# lock\n", encoding="utf-8")
    cargo_plan = BuildDetector().detect(cargo)
    assert cargo_plan.setup[0].argv == ("cargo", "fetch", "--locked")


def test_python_tool_only_config_does_not_force_editable_install(tmp_path: Path):
    root = tmp_path / "pytest-config-only"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n", encoding="utf-8"
    )
    plan = BuildDetector().detect(root)
    install = plan.setup[1].argv
    assert "-e" not in install
    assert install[-1] == "pytest"


def test_loader_accepts_every_marker_supported_by_detector(tmp_path: Path):
    markers = {
        "Package.swift": "",
        "Gemfile": "",
        "composer.json": "{}\n",
        "WORKSPACE": "",
        "build.ninja": "",
        "setup.py": "",
        "GNUmakefile": "test:\n\t@true\n",
        "sample.csproj": "",
    }
    for index, (marker, contents) in enumerate(markers.items()):
        root = tmp_path / f"marker-{index}"
        root.mkdir()
        (root / marker).write_text(contents, encoding="utf-8")
        assert ProjectLoader().load(root).path == root.resolve()


def test_project_local_custom_plan_is_data_not_a_named_adapter(tmp_path: Path):
    root = tmp_path / "custom"
    root.mkdir()
    (root / ".debugging-framework.json").write_text(
        json.dumps({"build": [["make"]], "test": [{"command": ["make", "check"]}]}),
        encoding="utf-8",
    )
    plan = BuildDetector().detect(root)
    assert plan.system == "project-config"
    assert plan.build[0].argv == ("make",)
    assert plan.test[0].argv == ("make", "check")


def test_production_source_detection_rejects_tests_and_generated_files():
    assert is_production_source_path("src/service.ts")
    assert not is_production_source_path("tests/test_service.py")
    assert not is_production_source_path("node_modules/lib/index.js")


def test_patch_outcome_categories_compare_initial_and_post_failures():
    def snapshot(*failed: str, status: str = "failing") -> dict:
        return {"status": status, "validation_error": "", "failed_test_ids": list(failed)}

    baseline = snapshot("test-a", "test-b")
    assert classify_patch_outcome(baseline, snapshot(status="plausible")) == "plausible"
    assert classify_patch_outcome(baseline, snapshot("test-b")) == "cleanfix"
    assert classify_patch_outcome(baseline, snapshot("test-b", "test-c")) == "noisefix"
    assert classify_patch_outcome(baseline, snapshot("test-a", "test-b")) == "nonefix"
    assert classify_patch_outcome(baseline, snapshot("test-a", "test-b", "test-c")) == "negfix"
    assert classify_patch_outcome(
        snapshot(status="plausible"), snapshot(status="plausible")
    ) == "nonefix"
    assert classify_patch_outcome(
        baseline, {"status": "invalid", "validation_error": "build_failed"}
    ) == "invalid"
    assert classify_patch_outcome(
        baseline, {"status": "failing", "validation_error": "", "failed_test_ids": []}
    ) == "invalid"
    assert classify_patch_outcome(
        baseline,
        {
            "status": "plausible",
            "validation_error": "",
            "failed_test_ids": ["contradictory-test"],
        },
    ) == "invalid"

    invalid = classify_validation_result(
        baseline, {"status": "invalid", "validation_error": "build_failed"}
    )
    assert invalid["classification_basis_valid"] is False
    assert invalid["fixed_test_ids"] == []
    assert invalid["regression_test_ids"] == []


def test_pipeline_outputs_only_a_validated_patch(tmp_path: Path):
    root = _custom_project(tmp_path / "project")
    project = ProjectLoader().load(root)

    class FakeRunner:
        def __init__(self, **_kwargs):
            pass

        def run(self, *, workspace, prompt, artifact_dir):
            assert "has not detected, installed, built, or tested it" in prompt
            assert not (settings.results_dir / project.project_id / "baseline").exists()
            return CodexRunResult(
                ok=True,
                returncode=0,
                payload={
                    "summary": "return expected value",
                    "fault_localization": [
                        {
                            "path": "src/value.py",
                            "function": "value",
                            "score": 0.99,
                            "reason": "failing assertion expects one",
                        }
                    ],
                    "repair": {
                        "paths": ["src/value.py"],
                        "description": "fix value",
                        "diff": (
                            "--- a/src/value.py\n"
                            "+++ b/src/value.py\n"
                            "@@ -1,2 +1,2 @@\n"
                            " def value():\n"
                            "-    return 0\n"
                            "+    return 1\n"
                        ),
                    },
                },
            )

    settings = SimpleNamespace(
        results_dir=tmp_path / "results",
        codex_executable="fake-codex",
        output_schema=tmp_path / "schema.json",
        codex_api_key="",
        codex_provider="",
        codex_base_url="",
        codex_wire_api="responses",
        codex_env_key="CODEX_API_KEY",
    )
    pipeline = DebuggingPipeline(
        settings=settings,
        validator=ProjectValidator(command_timeout=30),
        runner_factory=FakeRunner,
    )
    output = tmp_path / "answer.patch"
    result = pipeline.run(
        project,
        PipelineOptions(attempts=1),
        output_patch=output,
    )
    assert result["status"] == "plausible"
    assert result["output_patch"] == str(output.resolve())
    assert result["validation"]["initial_failed_test_ids"] == ["command:test-1"]
    assert result["validation"]["post_failed_test_ids"] == []
    payload = (
        settings.results_dir
        / project.project_id
        / "attempts"
        / "attempt_01"
        / "codex.payload.json"
    )
    assert payload.is_file()
    assert "+++ b/src/value.py" in output.read_text(encoding="utf-8")
    assert "return 0" in (root / "src" / "value.py").read_text(encoding="utf-8")


def test_codex_runs_before_clean_baseline_is_classified_as_nonefix(tmp_path: Path):
    root = _custom_project(tmp_path / "clean-project")
    (root / "src" / "value.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    project = ProjectLoader().load(root)

    class CleanProjectRunner:
        def __init__(self, **_kwargs):
            pass

        def run(self, **_kwargs):
            return CodexRunResult(
                ok=True,
                returncode=0,
                payload={
                    "summary": "non-functional comment",
                    "fault_localization": [
                        {
                            "path": "src/value.py",
                            "function": "value",
                            "score": 1.0,
                            "reason": "inspected clean project",
                        }
                    ],
                    "repair": {
                        "paths": ["src/value.py"],
                        "description": "comment only",
                        "diff": (
                            "--- a/src/value.py\n+++ b/src/value.py\n"
                            "@@ -1,2 +1,2 @@\n def value():\n"
                            "-    return 1\n+    return 1  # unchanged behavior\n"
                        ),
                    },
                },
            )

    settings = SimpleNamespace(
        results_dir=tmp_path / "results",
        codex_executable="unused",
        output_schema=tmp_path / "schema.json",
    )
    output = tmp_path / "clean.diff"
    output.write_text("stale patch", encoding="utf-8")
    result = DebuggingPipeline(
        settings=settings,
        validator=ProjectValidator(command_timeout=30),
        runner_factory=CleanProjectRunner,
    ).run(project, PipelineOptions(attempts=1), output_patch=output)
    assert result["status"] == "nonefix"
    assert result["validation"]["fixed_test_ids"] == []
    assert result["validation"]["regression_test_ids"] == []
    assert output.is_file()


def test_manual_validate_uses_the_same_public_outcome_classification(tmp_path: Path):
    root = _custom_project(tmp_path / "manual-validate-project")
    original = (root / "src" / "value.py").read_text(encoding="utf-8")
    repaired = original.replace("return 0", "return 1")
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            repaired.splitlines(keepends=True),
            fromfile="a/src/value.py",
            tofile="b/src/value.py",
        )
    )
    patch_path = tmp_path / "manual.patch"
    patch_path.write_text(diff, encoding="utf-8")
    settings = SimpleNamespace(results_dir=tmp_path / "manual-results")

    returncode = cli.validate_patch(
        settings,
        ProjectLoader().load(root),
        patch_path,
        timeout=30,
        jobs=1,
    )

    result_path = (
        settings.results_dir
        / root.name
        / "manual-validation"
        / "result.json"
    )
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert returncode == 0
    assert result["status"] == "plausible"
    assert result["post_validation_status"] == "plausible"
    assert result["initial_failed_test_ids"] == ["command:test-1"]
    assert result["post_failed_test_ids"] == []
    assert (root / "src" / "value.py").read_text(encoding="utf-8") == original


def test_pipeline_rejects_artifacts_inside_input_project_before_writing(tmp_path: Path):
    root = _custom_project(tmp_path / "protected-input")
    output = root / "answer.diff"
    output.write_text("existing user file\n", encoding="utf-8")

    settings = SimpleNamespace(
        results_dir=tmp_path / "external-results",
        codex_executable="unused",
        output_schema=tmp_path / "schema.json",
    )
    try:
        DebuggingPipeline(
            settings=settings,
            validator=ProjectValidator(command_timeout=30),
        ).run(
            ProjectLoader().load(root),
            PipelineOptions(attempts=1),
            output_patch=output,
        )
    except ValueError as exc:
        assert "patch output phải nằm ngoài input project" in str(exc)
    else:
        raise AssertionError("an output inside the input project must be rejected")
    assert output.read_text(encoding="utf-8") == "existing user file\n"

    settings.results_dir = root / "results"
    try:
        DebuggingPipeline(
            settings=settings,
            validator=ProjectValidator(command_timeout=30),
        ).run(ProjectLoader().load(root), PipelineOptions(attempts=1))
    except ValueError as exc:
        assert "results phải nằm ngoài input project" in str(exc)
    else:
        raise AssertionError("results inside the input project must be rejected")
    assert not (root / "results").exists()


def test_validator_rejects_a_vacuous_test_command(tmp_path: Path):
    root = tmp_path / "vacuous"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / ".debugging-framework.json").write_text(
        json.dumps({"system": "custom", "test": [["/usr/bin/true"]]}),
        encoding="utf-8",
    )

    result = ProjectValidator(command_timeout=30).baseline(
        ProjectLoader().load(root), tmp_path / "vacuous-result"
    )

    assert result["status"] == "invalid"
    assert result["validation_error"] == "test_execution_unverified:test-1"


def test_validator_rejects_success_code_that_reports_failed_tests(tmp_path: Path):
    root = tmp_path / "conflicting-test-status"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / ".debugging-framework.json").write_text(
        json.dumps(
            {
                "system": "custom",
                "test": [
                    {
                        "command": [sys.executable, "-c", "print('1 failed')"],
                        "evidence_pattern": "1 failed",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = ProjectValidator(command_timeout=30).baseline(
        ProjectLoader().load(root), tmp_path / "conflict-result"
    )

    assert result["status"] == "invalid"
    assert result["validation_error"] == "test_status_output_conflict:test-1"


def test_validator_extracts_individual_pytest_failure_ids(tmp_path: Path):
    root = tmp_path / "pytest-id-project"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    output = "FAILED tests/test_api.py::test_retries - AssertionError\n1 failed"
    (root / ".debugging-framework.json").write_text(
        json.dumps(
            {
                "system": "custom",
                "test": [
                    {
                        "command": [
                            sys.executable, "-c",
                            f"print({output!r}); raise SystemExit(1)",
                        ],
                        "evidence_pattern": "1 failed",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = ProjectValidator(command_timeout=30).baseline(
        ProjectLoader().load(root), tmp_path / "pytest-id-result"
    )

    assert result["status"] == "failing"
    assert result["failed_test_ids"] == ["tests/test_api.py::test_retries"]
    assert result["test_id_granularity"] == "test-case"


def test_validation_side_effects_stay_inside_disposable_copy(tmp_path: Path):
    root = tmp_path / "side-effect-project"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    test_code = (
        "from pathlib import Path; "
        "Path('validation-side-effect.txt').write_text('temporary'); "
        "print('1 passed')"
    )
    (root / ".debugging-framework.json").write_text(
        json.dumps(
            {
                "system": "custom",
                "test": [
                    {
                        "command": [sys.executable, "-c", test_code],
                        "evidence_pattern": "1 passed",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = ProjectValidator(command_timeout=30).baseline(
        ProjectLoader().load(root), tmp_path / "side-effect-result"
    )

    assert result["status"] == "plausible"
    assert result["validation_process_sandboxed"] is True
    assert result["input_project_untouched"] is True
    assert not (root / "validation-side-effect.txt").exists()


def test_setup_is_automatic_and_stays_inside_disposable_copy(tmp_path: Path):
    root = tmp_path / "automatic-setup-project"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    setup_code = "from pathlib import Path; Path('installed.marker').write_text('ready')"
    test_code = (
        "from pathlib import Path; "
        "ok = Path('installed.marker').read_text() == 'ready'; "
        "print('1 passed' if ok else '1 failed'); "
        "raise SystemExit(0 if ok else 1)"
    )
    (root / ".debugging-framework.json").write_text(
        json.dumps(
            {
                "system": "custom",
                "setup": [[sys.executable, "-c", setup_code]],
                "test": [
                    {
                        "command": [sys.executable, "-c", test_code],
                        "evidence_pattern": "1 (?:passed|failed)",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = ProjectValidator(command_timeout=30).baseline(
        ProjectLoader().load(root), tmp_path / "automatic-setup-result"
    )

    assert result["status"] == "plausible"
    assert result["setup_executed"] is True
    assert result["environment_provisioned"] is True
    assert result["setup_commands"][0]["returncode"] == 0
    assert not (root / "installed.marker").exists()


def test_setup_failure_stops_before_build_and_tests(tmp_path: Path):
    root = tmp_path / "failed-setup-project"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / ".debugging-framework.json").write_text(
        json.dumps(
            {
                "system": "custom",
                "setup": [[sys.executable, "-c", "raise SystemExit(3)"]],
                "build": [[sys.executable, "-c", "raise AssertionError('must not run')"]],
                "test": [
                    {
                        "command": [sys.executable, "-c", "print('1 passed')"],
                        "evidence_pattern": "1 passed",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = ProjectValidator(command_timeout=30).baseline(
        ProjectLoader().load(root), tmp_path / "failed-setup-result"
    )

    assert result["status"] == "invalid"
    assert result["validation_error"] == "setup_failed"
    assert result["setup_executed"] is True
    assert result["environment_provisioned"] is False
    assert result["build_commands"] == []
    assert result["test_commands"] == []


def test_validation_sandbox_blocks_absolute_write_to_input_project(tmp_path: Path):
    root = tmp_path / "absolute-write-project"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
    forbidden = root / "must-not-be-created.txt"
    test_code = (
        "from pathlib import Path; "
        f"target = Path({str(forbidden)!r}); "
        "blocked = False; "
        "\ntry:\n target.write_text('escaped')\n"
        "except OSError:\n blocked = True\n"
        "print('1 passed' if blocked else '1 failed'); "
        "raise SystemExit(0 if blocked else 1)"
    )
    (root / ".debugging-framework.json").write_text(
        json.dumps(
            {
                "system": "custom",
                "test": [
                    {
                        "command": [sys.executable, "-c", test_code],
                        "evidence_pattern": "1 (?:passed|failed)",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = ProjectValidator(command_timeout=30).baseline(
        ProjectLoader().load(root), tmp_path / "absolute-write-result"
    )

    assert result["status"] == "plausible"
    assert result["validation_process_sandboxed"] is True
    assert not forbidden.exists()


def test_validation_fails_closed_without_filesystem_sandbox(tmp_path: Path):
    root = _custom_project(tmp_path / "no-sandbox")

    result = ProjectValidator(
        command_timeout=30, sandbox_executable="definitely-not-installed-bwrap"
    ).baseline(ProjectLoader().load(root), tmp_path / "no-sandbox-result")

    assert result["status"] == "invalid"
    assert result["validation_error"] == "validation_sandbox_unavailable:bwrap"
    assert result["validation_process_sandboxed"] is False


def test_patch_cannot_replace_the_validation_contract(tmp_path: Path):
    root = _custom_project(tmp_path / "validation-contract")
    contract_path = root / ".debugging-framework.json"
    original = contract_path.read_text(encoding="utf-8") + "\n"
    contract_path.write_text(original, encoding="utf-8")
    replacement = json.dumps(
        {
            "system": "custom",
            "test": [
                {
                    "command": [sys.executable, "-c", "print('1 passed')"],
                    "evidence_pattern": "1 passed",
                }
            ],
        }
    ) + "\n"
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            replacement.splitlines(keepends=True),
            fromfile="a/.debugging-framework.json",
            tofile="b/.debugging-framework.json",
        )
    )

    result = ProjectValidator(command_timeout=30).validate_diff(
        project=ProjectLoader().load(root),
        diff=diff,
        patch_paths=[".debugging-framework.json"],
        artifact_dir=tmp_path / "validation-contract-result",
    )

    assert result["status"] == "failing"
    assert result["post_failed_tests"] == ["test-1"]


def test_workspace_parses_a_multifile_repository_diff(tmp_path: Path):
    root = _python_project(tmp_path / "multi-file")
    project = ProjectLoader().load(root)
    diff = (
        "--- a/src/value.py\n+++ b/src/value.py\n"
        "@@ -1,2 +1,2 @@\n def value():\n-    return 0\n+    return 1\n"
        "--- a/tests/test_value.py\n+++ b/tests/test_value.py\n"
        "@@ -1,2 +1,2 @@\n-from src.value import value\n"
        "+from src.value import value  # repaired\n \n"
    )

    with ProjectWorkspace(project, tmp_path / "multi-workspaces") as workspace:
        paths = workspace.unified_diff_paths(diff)
        assert paths == ["src/value.py", "tests/test_value.py"]


def test_repair_paths_must_match_every_file_in_diff():
    payload = {
        "fault_localization": [
            {"path": "src/other.py", "function": "other", "score": 1.0},
            {"path": "src/value.py", "function": "value", "score": 0.5},
        ],
        "repair": {"paths": ["src/value.py", "src/helper.py"]},
    }
    error = DebuggingPipeline._response_path_error(payload, ["src/value.py"])
    assert error.startswith("codex_repair_paths_mismatch:")


def test_failed_validation_still_outputs_raw_diff_without_modifying_input(tmp_path: Path):
    root = _custom_project(tmp_path / "failed-repair")
    project = ProjectLoader().load(root)
    original = (root / "src" / "value.py").read_bytes()
    raw_diff = (
        "--- a/src/value.py\n+++ b/src/value.py\n"
        "@@ -1,2 +1,2 @@\n def value():\n-    return 0\n+    return 2\n"
    )

    class FailingPatchRunner:
        def __init__(self, **_kwargs):
            pass

        def run(self, **_kwargs):
            return CodexRunResult(
                ok=True,
                returncode=0,
                payload={
                    "summary": "wrong candidate retained for audit",
                    "fault_localization": [
                        {
                            "path": "src/value.py",
                            "function": "value",
                            "score": 1.0,
                            "reason": "failing value test",
                        }
                    ],
                    "repair": {
                        "paths": ["src/value.py"],
                        "description": "wrong value",
                        "diff": raw_diff,
                    },
                },
            )

    settings = SimpleNamespace(
        results_dir=tmp_path / "failed-results",
        codex_executable="fake-codex",
        output_schema=tmp_path / "schema.json",
        codex_api_key="",
        codex_provider="",
        codex_base_url="",
        codex_wire_api="responses",
        codex_env_key="CODEX_API_KEY",
    )
    output = tmp_path / "failed-answer.diff"
    output.write_text("stale", encoding="utf-8")

    result = DebuggingPipeline(
        settings=settings,
        validator=ProjectValidator(command_timeout=30),
        runner_factory=FailingPatchRunner,
    ).run(project, PipelineOptions(attempts=1), output_patch=output)

    assert result["status"] == "nonefix"
    assert result["output_patch"] == str(output.resolve())
    assert output.read_text(encoding="utf-8") == raw_diff
    assert (root / "src" / "value.py").read_bytes() == original
    raw_artifact = settings.results_dir / "failed-repair" / "attempts" / "attempt_01" / "llm.patch.diff"
    assert raw_artifact.read_text(encoding="utf-8") == raw_diff


def test_writable_codex_workspace_can_return_a_validated_multifile_patch(tmp_path: Path):
    root = tmp_path / "multi-repair"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "first.py").write_text("FIRST = 0\n", encoding="utf-8")
    (root / "src" / "second.py").write_text("SECOND = 0\n", encoding="utf-8")
    test_code = (
        "from pathlib import Path; "
        "ok = 'FIRST = 1' in Path('src/first.py').read_text() and "
        "'SECOND = 1' in Path('src/second.py').read_text(); "
        "print('2 passed' if ok else '2 failed'); "
        "raise SystemExit(0 if ok else 1)"
    )
    (root / ".debugging-framework.json").write_text(
        json.dumps(
            {
                "system": "custom",
                "test": [
                    {
                        "command": [sys.executable, "-c", test_code],
                        "evidence_pattern": "2 (?:passed|failed)",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    raw_diff = (
        "--- a/src/first.py\n+++ b/src/first.py\n"
        "@@ -1 +1 @@\n-FIRST = 0\n+FIRST = 1\n"
        "--- a/src/second.py\n+++ b/src/second.py\n"
        "@@ -1 +1 @@\n-SECOND = 0\n+SECOND = 1\n"
    )

    class WritableMultiFileRunner:
        def __init__(self, **_kwargs):
            pass

        def run(self, *, workspace, **_kwargs):
            # Simulate Codex exercising workspace-write before returning its patch.
            (workspace / "src" / "first.py").write_text("FIRST = 1\n", encoding="utf-8")
            (workspace / "src" / "second.py").write_text("SECOND = 1\n", encoding="utf-8")
            return CodexRunResult(
                ok=True,
                returncode=0,
                payload={
                    "summary": "repair both coupled values",
                    "fault_localization": [
                        {"path": "src/first.py", "function": "FIRST", "score": 1.0, "reason": "test"},
                        {"path": "src/second.py", "function": "SECOND", "score": 0.9, "reason": "test"},
                    ],
                    "repair": {
                        "paths": ["src/first.py", "src/second.py"],
                        "description": "update both values",
                        "diff": raw_diff,
                    },
                },
            )

    settings = SimpleNamespace(
        results_dir=tmp_path / "multi-results",
        codex_executable="fake-codex",
        output_schema=tmp_path / "schema.json",
        codex_api_key="",
        codex_provider="",
        codex_base_url="",
        codex_wire_api="responses",
        codex_env_key="CODEX_API_KEY",
    )
    output = tmp_path / "multi-answer.diff"
    result = DebuggingPipeline(
        settings=settings,
        validator=ProjectValidator(command_timeout=30),
        runner_factory=WritableMultiFileRunner,
    ).run(ProjectLoader().load(root), PipelineOptions(attempts=1), output_patch=output)

    assert result["status"] == "plausible"
    assert result["patch_validation_passed"] is True
    assert result["validation"]["validation_process_sandboxed"] is True
    assert result["repair_paths"] == ["src/first.py", "src/second.py"]
    assert output.read_text(encoding="utf-8") == raw_diff
    assert (root / "src" / "first.py").read_text(encoding="utf-8") == "FIRST = 0\n"
    assert (root / "src" / "second.py").read_text(encoding="utf-8") == "SECOND = 0\n"


def test_make_without_a_test_workflow_is_rejected(tmp_path: Path):
    root = tmp_path / "make-only"
    root.mkdir()
    (root / "Makefile").write_text("all:\n\t@true\n", encoding="utf-8")
    try:
        BuildDetector().detect(root)
    except ValueError as exc:
        assert "không khai báo target test" in str(exc)
    else:
        raise AssertionError("a project without tests must not be accepted")


def test_batch_manifest_is_a_list_of_direct_project_inputs(tmp_path: Path):
    first = tmp_path / "versions" / "bug-1"
    second = tmp_path / "versions" / "bug-2"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    manifest = tmp_path / "versions.json"
    manifest.write_text(
        json.dumps(
            {
                "version_count": 2,
                "projects": [
                    {"bug_id": "A.1", "project_path": "versions/bug-1"},
                    {"bug_id": "A.2", "project_path": str(second)},
                ],
            }
        ),
        encoding="utf-8",
    )

    records = load_batch_manifest(manifest)

    assert records[0]["project_path"] == str(first.resolve())
    assert records[1]["project_path"] == str(second.resolve())


def test_cli_accepts_materialized_batch_manifest(tmp_path: Path):
    args = build_parser().parse_args(["run-batch", str(tmp_path / "versions.json")])
    assert args.command == "run-batch"
    assert args.manifest == tmp_path / "versions.json"


def test_run_batch_processes_each_project_and_writes_summary(tmp_path: Path, monkeypatch):
    manifest = tmp_path / "versions.json"
    manifest.write_text(
        json.dumps(
            {
                "projects": [
                    {"bug_id": "A.1", "project_path": "/data/bug-1"},
                    {"bug_id": "A.2", "project_path": "/data/bug-2"},
                ]
            }
        ),
        encoding="utf-8",
    )
    seen = []

    def fake_load(_loader, path):
        return SimpleNamespace(path=path, project_id=path.name)

    def fake_run(_settings, project, _args, output):
        seen.append((project.project_id, output))
        return {"status": "plausible", "output_patch": str(output)}

    monkeypatch.setattr(cli.ProjectLoader, "load", fake_load)
    monkeypatch.setattr(cli, "run_one_project", fake_run)
    settings = SimpleNamespace(results_dir=tmp_path / "results", codex_api_key="")
    config = SimpleNamespace(require_api_key=False)
    args = SimpleNamespace(
        manifest=manifest,
        output_dir=tmp_path / "patches",
        attempts=1,
        model=None,
        codex_timeout=1,
        command_timeout=1,
        jobs=0,
        inherit_codex_config=False,
    )

    returncode = cli.run_batch(settings, config, args)

    assert returncode == 0
    assert [item[0] for item in seen] == ["bug-1", "bug-2"]
    summary = json.loads((settings.results_dir / "batch_result.json").read_text())
    assert summary["project_count"] == 2
    assert summary["plausible_count"] == 2
