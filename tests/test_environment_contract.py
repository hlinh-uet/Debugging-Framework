from __future__ import annotations

import json
import subprocess

import pytest

import debugging_framework
from src.cli import (
    _prepare_repair_file_inputs,
    _validate_repair_config_contract,
    apply_effective_options,
    build_parser,
    main,
)
from src.core.pipeline import DebuggingPipeline, PipelineOptions
from src.environments.oci import OCIEnvironment
from src.environments.spec import EnvironmentResolver
from src.utils.config import FrameworkConfig, Settings
from src.utils.project_config import ProjectConfig, load_project_config
from src.validation.project import BuildDetector


def test_public_package_imports() -> None:
    assert debugging_framework.DebuggingPipeline
    assert debugging_framework.BuildPlan


def test_environment_mode_is_required() -> None:
    with pytest.raises(ValueError, match="không fallback"):
        Settings().validated()


@pytest.mark.parametrize("legacy_mode", ["auto", "current", "local", "oci", "container"])
def test_legacy_environment_modes_are_rejected(legacy_mode: str) -> None:
    with pytest.raises(ValueError, match="host hoặc image"):
        Settings(environment_backend=legacy_mode).validated()


def test_image_requires_name_and_host_rejects_name() -> None:
    with pytest.raises(ValueError, match="bắt buộc"):
        Settings(environment_backend="image").validated()
    with pytest.raises(ValueError, match="chỉ được dùng"):
        Settings(environment_backend="host", environment_image="unused:latest").validated()


def test_project_config_requires_new_environment_schema(tmp_path) -> None:
    config = tmp_path / ".debugging-framework.json"
    config.write_text(json.dumps({"environment": {"backend": "oci"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="field không được hỗ trợ"):
        load_project_config(tmp_path)

    config.write_text(json.dumps({"environment": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="environment.mode là bắt buộc"):
        load_project_config(tmp_path)


def test_resolver_records_prepared_environment_without_provisioning(tmp_path) -> None:
    (tmp_path / "CMakeLists.txt").write_text("project(example)\n", encoding="utf-8")
    resolver = EnvironmentResolver()

    host = resolver.resolve(tmp_path, "cmake", backend="host")
    assert host.backend == "host"
    assert host.base_image == ""

    image = resolver.resolve(
        tmp_path, "cmake", backend="image", image="partner/project-tests:prepared"
    )
    assert image.base_image == "partner/project-tests:prepared"


def test_auto_detector_is_scoped_to_cpp_build_systems(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='python-only'\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="Không tự nhận diện"):
        BuildDetector().detect(tmp_path)

    (tmp_path / "CMakeLists.txt").write_text("project(example CXX)\n", encoding="utf-8")
    assert BuildDetector().detect(tmp_path).system == "cmake"


def test_cli_exposes_only_host_or_image() -> None:
    parser = build_parser()
    args = parser.parse_args(["inspect", ".", "--environment", "host"])
    assert args.environment_backend == "host"
    with pytest.raises(SystemExit):
        parser.parse_args(["inspect", ".", "--environment", "oci"])
    with pytest.raises(SystemExit):
        parser.parse_args(["inspect", ".", "--environment-backend", "host"])


def test_explicit_host_does_not_inherit_image_from_lower_priority_config(tmp_path) -> None:
    args = build_parser().parse_args(["inspect", str(tmp_path), "--environment", "host"])
    config = FrameworkConfig(
        environment_backend="image",
        environment_image="global/image:prepared",
    )
    apply_effective_options(
        args,
        config,
        ProjectConfig(path=tmp_path / ".debugging-framework.json", raw={}),
    )
    assert args.environment_backend == "host"
    assert args.environment_image == ""


def test_image_mode_only_inspects_prebuilt_image(tmp_path, monkeypatch) -> None:
    spec = EnvironmentResolver().resolve(
        tmp_path, "cmake", backend="image", image="partner/tests:prepared"
    )
    commands: list[list[str]] = []

    monkeypatch.setattr("src.environments.oci.shutil.which", lambda value: f"/bin/{value}")

    def fake_run(command, **kwargs):
        del kwargs
        commands.append(list(command))
        if command[1] == "info":
            return subprocess.CompletedProcess(command, 0, stdout="")
        return subprocess.CompletedProcess(command, 0, stdout="sha256:image-id\n")

    monkeypatch.setattr("src.environments.oci.subprocess.run", fake_run)
    provision = OCIEnvironment(runtime="docker").provision(spec, tmp_path / "artifacts")

    assert provision.image_digest == "sha256:image-id"
    assert commands == [
        ["docker", "info"],
        ["docker", "image", "inspect", "--format", "{{.Id}}", "partner/tests:prepared"],
    ]
    assert not any("build" in command or "pull" in command for command in commands)


def test_init_writes_explicit_host_contract(tmp_path) -> None:
    assert main(["init", str(tmp_path), "--environment", "host"]) == 0
    raw = json.loads((tmp_path / ".debugging-framework.json").read_text(encoding="utf-8"))
    assert raw["schema_version"] == 5
    assert raw["environment"] == {"mode": "host"}
    assert "failure_output" not in raw["repair"]


def test_repair_two_file_contract_infers_project_root(tmp_path) -> None:
    config = tmp_path / ".debugging-framework.json"
    failure = tmp_path / "failure_output.log"
    config.write_text("{}\n", encoding="utf-8")
    failure.write_text("actual failure\n", encoding="utf-8")
    args = build_parser().parse_args([
        "repair",
        "--config", str(config),
        "--failure-output", str(failure),
    ])

    _prepare_repair_file_inputs(args)

    assert args.project == tmp_path.resolve()
    assert args.project_config == config.resolve()
    assert args.failing_output == failure.resolve()


def test_repair_two_file_contract_rejects_missing_input(tmp_path) -> None:
    failure = tmp_path / "failure_output.log"
    failure.write_text("actual failure\n", encoding="utf-8")
    args = build_parser().parse_args([
        "repair", "--failure-output", str(failure)
    ])
    with pytest.raises(ValueError, match="repair cần --config"):
        _prepare_repair_file_inputs(args)


def test_two_file_inputs_are_archived_with_digests(tmp_path) -> None:
    config = tmp_path / ".debugging-framework.json"
    failure = tmp_path / "failure_output.log"
    config.write_text('{"schema_version": 5}\n', encoding="utf-8")
    failure.write_text("actual failure\n", encoding="utf-8")
    results = tmp_path / "results"

    archived = DebuggingPipeline._archive_file_inputs(
        results,
        PipelineOptions(request_config_path=config, failure_output_path=failure),
    )

    assert (results / archived["config"]["artifact"]).read_text(encoding="utf-8") == (
        config.read_text(encoding="utf-8")
    )
    assert (results / archived["failure_output"]["artifact"]).read_text(
        encoding="utf-8"
    ) == failure.read_text(encoding="utf-8")
    assert len(archived["config"]["sha256"]) == 64
    assert len(archived["failure_output"]["sha256"]) == 64


def test_pipeline_rejects_incomplete_two_file_request() -> None:
    with pytest.raises(ValueError, match="actual failing output"):
        DebuggingPipeline._validate_request(PipelineOptions(failing_tests=("cpp_test",)))


def test_main_accepts_only_two_file_paths_for_repair(tmp_path, monkeypatch) -> None:
    config = tmp_path / ".debugging-framework.json"
    failure = tmp_path / "failure_output.log"
    config.write_text(json.dumps({
        "schema_version": 5,
        "repair": {"failing_tests": ["cpp_test_case"]},
        "environment": {"mode": "host"},
    }), encoding="utf-8")
    failure.write_text("cpp_test_case: FAILED\n", encoding="utf-8")
    observed: dict[str, object] = {}

    def fake_repair(settings, project, args, output_patch, failing_tests, **kwargs):
        del settings, output_patch
        observed["project"] = project.path
        observed["config"] = args.project_config
        observed["failure"] = args.failing_output
        observed["tests"] = tuple(failing_tests)
        observed["output"] = kwargs["external_baseline_output"]
        return {"status": "invalid", "validation_error": "test-double"}

    monkeypatch.setattr("src.cli.repair_project", fake_repair)

    assert main([
        "repair",
        "--config", str(config),
        "--failure-output", str(failure),
    ]) == 1
    assert observed == {
        "project": tmp_path.resolve(),
        "config": config.resolve(),
        "failure": failure.resolve(),
        "tests": ("cpp_test_case",),
        "output": "cpp_test_case: FAILED\n",
    }


def test_main_returns_structured_error_when_two_file_contract_is_incomplete(
    tmp_path, capsys
) -> None:
    failure = tmp_path / "failure_output.log"
    failure.write_text("FAILED\n", encoding="utf-8")

    assert main(["repair", "--failure-output", str(failure)]) == 2
    error = json.loads(capsys.readouterr().err)
    assert error["status"] == "error"
    assert error["stage"] == "preflight"
    assert "--config" in error["error"]


def test_repair_config_is_authoritative_for_tests_and_environment(tmp_path) -> None:
    config = tmp_path / ".debugging-framework.json"
    config.write_text(json.dumps({
        "schema_version": 5,
        "repair": {"failing_tests": ["case"]},
        "environment": {"mode": "host"},
    }), encoding="utf-8")
    project_config = load_project_config(tmp_path)
    args = build_parser().parse_args([
        "--environment", "host",
        "repair",
        "--config", str(config),
        "--failure-output", str(tmp_path / "failure.log"),
    ])
    with pytest.raises(ValueError, match="không dùng CLI override"):
        _validate_repair_config_contract(args, project_config)
