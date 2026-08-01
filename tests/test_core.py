from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.codex_runner import CodexRunResult, CodexRunner
from src.utils.jsonio import atomic_write_json, load_json, safe_name
from src.core.pipeline import (
    DebuggingPipeline,
    PipelineOptions,
    candidate_sort_key,
    localization_key,
    select_localization,
)
from src.core.prompts import build_codex_prompt
from src.utils.workspace import (
    BugWorkspace,
    allowed_source_files,
    normalize_relpath,
    repair_candidate_files,
)


def test_prompt_excludes_ground_truth_value_and_contains_failure_context():
    prompt = build_codex_prompt(
        bug_id="A.2",
        dataset="fmt",
        tests=[
            {
                "test_id": "format-test::one",
                "outcome": "FAIL",
                "expected_output": "expected",
                "actual_output": "actual",
                "fail_reason": "mismatch",
                "covered_functions": ["format.h:writer::write"],
            },
            {"test_id": "format-test::two", "outcome": "PASS"},
        ],
        allowed_source_files=["include/fmt/format.h"],
        attempt=1,
        previous_attempt=None,
    )
    assert "secret_ground_truth_symbol" not in prompt
    assert "format-test::one" in prompt
    assert '"expected_output": "expected"' in prompt
    assert "include/fmt/format.h" in prompt


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("src/main.c", "src/main.c"),
        ("src\\main.c", "src/main.c"),
        ("../main.c", ""),
        ("/tmp/main.c", ""),
        ("", ""),
    ],
)
def test_normalize_relpath(value, expected):
    assert normalize_relpath(value) == expected


def test_allowed_source_files_fallback_and_deduplication():
    assert allowed_source_files(
        {"src_files": ["src/a.c", "src/a.c", "../escape.c", "src/b.c"]}
    ) == ["src/a.c", "src/b.c"]
    assert allowed_source_files({"source_relpath": "lib/one.cc"}) == ["lib/one.cc"]


def test_repair_candidates_come_from_failing_coverage(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    (repo / "src").mkdir()
    (repo / "src" / "covered.c").write_text("void covered(void) {}\n")
    (repo / "src" / "ground_truth.c").write_text("void target(void) {}\n")
    _git(repo, "add", "src/covered.c", "src/ground_truth.c")
    bug = SimpleNamespace(
        tests=[
            {
                "test_id": "neg1",
                "outcome": "FAIL",
                "covered_functions": ["covered.c:covered"],
            }
        ],
        raw={
            "buggy_tree_dir": str(repo),
            "src_files": ["src/ground_truth.c"],
        },
    )
    assert repair_candidate_files(bug) == ["src/covered.c"]


def test_localization_key_preserves_cpp_scope():
    assert (
        localization_key("include/fmt/format.h", "basic_writer::write")
        == "format.h:basic_writer::write"
    )


def test_select_localization_prefers_exact_path():
    payload = {
        "fault_localization": [
            {"path": "other/format.h", "function": "wrong", "score": 0.99},
            {
                "path": "include/fmt/format.h",
                "function": "basic_writer::write",
                "score": 0.8,
            },
        ]
    }
    selected = select_localization(payload, "include/fmt/format.h")
    assert selected["function"] == "basic_writer::write"


def test_candidate_sorting_prioritizes_semantic_status():
    plausible = {"status": "plausible", "post_failed_tests": [], "attempt": 2}
    cleanfix = {"status": "cleanfix", "post_failed_tests": ["one"], "attempt": 1}
    assert candidate_sort_key(plausible) < candidate_sort_key(cleanfix)


def test_patch_policy_rejects_extra_file():
    payload = {"repair": {"path": "src/a.c"}}
    error = DebuggingPipeline._patch_policy_error(
        payload, ["src/a.c"], ["README.md"], ["src/a.c"]
    )
    assert error == "codex_modified_unexpected_files:README.md"


def test_atomic_json_roundtrip(tmp_path: Path):
    target = tmp_path / "nested" / "result.json"
    atomic_write_json(target, {"text": "Tiếng Việt", "value": 2})
    assert load_json(target, {}) == {"text": "Tiếng Việt", "value": 2}
    assert not list(target.parent.glob("*.tmp"))


def test_codex_output_schema_is_packaged_and_strict():
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "schemas"
        / "codex_result.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"summary", "fault_localization", "repair"}
    assert "diff" in schema["properties"]["repair"]["required"]


def test_safe_name_removes_path_separators():
    assert safe_name("CVE/../../bad name") == "CVE_.._.._bad_name"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_bug_workspace_materializes_buggy_overlay_and_detects_patch(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Tests")
    _git(repo, "config", "user.email", "tests@example.invalid")
    source = repo / "src" / "value.c"
    source.parent.mkdir()
    source.write_text("int value(void) { return 0; }\n", encoding="utf-8")
    _git(repo, "add", "src/value.c")
    _git(repo, "commit", "-m", "buggy")
    commit_before = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    source.write_text("int value(void) { return 1; }\n", encoding="utf-8")
    _git(repo, "commit", "-am", "fixed")
    commit_after = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    bug = SimpleNamespace(
        bug_id="demo",
        raw={
            "buggy_tree_dir": str(repo),
            "commit_after": commit_after,
            "commit_before": commit_before,
            "src_files": ["src/value.c"],
        },
    )
    worktree_path = None
    with BugWorkspace(bug, tmp_path / "worktrees") as workspace:
        worktree_path = workspace.path
        candidate = workspace.path / "src" / "value.c"
        assert "return 0" in candidate.read_text(encoding="utf-8")
        assert subprocess.check_output(
            ["git", "-C", str(workspace.path), "rev-list", "--count", "HEAD"],
            text=True,
        ).strip() == "1"
        assert "Buggy benchmark snapshot" in subprocess.check_output(
            ["git", "-C", str(workspace.path), "log", "-1", "--format=%s"],
            text=True,
        )
        workspace.apply_unified_diff(
            "--- a/src/value.c\n"
            "+++ b/src/value.c\n"
            "@@ -1 +1 @@\n"
            "-int value(void) { return 0; }\n"
            "+int value(void) { return 2; }\n"
        )
        assert workspace.changed_source_files() == ["src/value.c"]
        assert workspace.unexpected_changes() == []
        assert "return 2" in workspace.function_diff("src/value.c")
    assert worktree_path is not None and not worktree_path.exists()


def test_codex_runner_reads_structured_response(tmp_path: Path):
    fake = tmp_path / "fake-codex"
    fake.write_text(
        """#!/usr/bin/env python3
import json
import pathlib
import sys

args = sys.argv[1:]
output = pathlib.Path(args[args.index('--output-last-message') + 1])
payload = {
    'summary': 'fixed',
    'fault_localization': [
        {'path': 'src.c', 'function': 'fixed', 'score': 0.9, 'reason': 'test'}
    ],
    'repair': {
        'path': 'src.c',
        'description': 'return correct value',
        'diff': '--- a/src.c\\n+++ b/src.c\\n@@ -1 +1 @@\\n-int fixed(void) { return 0; }\\n+int fixed(void) { return 1; }\\n',
    },
}
output.write_text(json.dumps(payload))
print(json.dumps({'type': 'turn.completed'}))
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "src.c").write_text("int fixed(void) { return 0; }\n")
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    runner = CodexRunner(
        executable=str(fake), schema_path=schema, timeout_seconds=10
    )
    result = runner.run(
        workspace=workspace,
        prompt="repair",
        artifact_dir=tmp_path / "artifacts",
    )
    assert result.ok
    assert result.payload["repair"]["path"] == "src.c"
    assert result.payload["repair"]["diff"].startswith("--- a/src.c")
    assert "return 0" in (workspace / "src.c").read_text(encoding="utf-8")
    assert result.command[result.command.index("--sandbox") + 1] == "read-only"
    assert "turn.completed" in (tmp_path / "artifacts" / "events.jsonl").read_text()


def test_pipeline_writes_evaluator_compatible_results(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Tests")
    _git(repo, "config", "user.email", "tests@example.invalid")
    source = repo / "src" / "value.c"
    source.parent.mkdir()
    source.write_text("int value(void) { return 0; }\n", encoding="utf-8")
    _git(repo, "add", "src/value.c")
    _git(repo, "commit", "-m", "buggy")
    commit_before = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    source.write_text("int value(void) { return 1; }\n", encoding="utf-8")
    _git(repo, "commit", "-am", "fixed")
    commit_after = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    bug = SimpleNamespace(
        bug_id="demo",
        tests=[{"test_id": "neg1", "outcome": "FAIL"}],
        ground_truth=["value.c:value"],
        raw={
            "buggy_tree_dir": str(repo),
            "commit_after": commit_after,
            "commit_before": commit_before,
            "src_files": ["src/value.c"],
        },
    )

    class FakeLoader:
        def load_bugs(self, dataset, bug_ids):
            return [bug]

    class FakeValidator:
        def initial_snapshot(self, tests, exclude_fixed_fail_tests):
            return {
                "comparison_failed": ["neg1"],
                "full_failed": ["neg1"],
                "fields": {
                    "init_passed_tests": [],
                    "init_failed_tests": ["neg1"],
                    "full_init_passed_tests": [],
                    "full_init_failed_tests": ["neg1"],
                },
            }

        def invalid_snapshot(self, initial, error, exclude_fixed_fail_tests):
            return {**initial["fields"], "status": "invalid", "real_status": "invalid"}

        def extract_function(self, text, function, source_path):
            return text.strip()

        def validate(self, **kwargs):
            return {
                **kwargs["initial"]["fields"],
                "status": "plausible",
                "real_status": "plausible",
                "validation_error": "",
                "post_passed_tests": ["neg1"],
                "post_failed_tests": [],
                "full_post_passed_tests": ["neg1"],
                "full_post_failed_tests": [],
                "fixed_fail_excluded_tests": [],
                "validation_details": {},
            }

    class FakeEvaluator:
        def evaluate(self, dataset, results_dir):
            return ""

    class FakeRunner:
        def __init__(self, **kwargs):
            pass

        def run(self, *, workspace, prompt, artifact_dir):
            return CodexRunResult(
                ok=True,
                returncode=0,
                payload={
                    "summary": "fix return value",
                    "fault_localization": [
                        {
                            "path": "src/value.c",
                            "function": "value",
                            "score": 0.95,
                            "reason": "failing assertion",
                        }
                    ],
                    "repair": {
                        "path": "src/value.c",
                        "description": "return expected value",
                        "diff": (
                            "--- a/src/value.c\n"
                            "+++ b/src/value.c\n"
                            "@@ -1 +1 @@\n"
                            "-int value(void) { return 0; }\n"
                            "+int value(void) { return 1; }\n"
                        ),
                    },
                },
            )

    results_dir = tmp_path / "results"
    settings = SimpleNamespace(
        results_dir=results_dir,
        unified_root=tmp_path / "Unified-Debugging",
        codex_executable="fake",
        output_schema=tmp_path / "schema.json",
    )
    pipeline = DebuggingPipeline(
        settings=settings,
        loader=FakeLoader(),
        validator=FakeValidator(),
        evaluator=FakeEvaluator(),
        runner_factory=FakeRunner,
    )
    pipeline.run(
        PipelineOptions(dataset="demo", attempts=1, evaluate_after_run=False),
        bug_ids=["demo"],
    )
    fl = load_json(results_dir / "fault_localization_results.json", {})
    apr = load_json(results_dir / "apr_results.json", {})
    assert fl["demo"]["scores"] == {"value.c:value": 0.95}
    assert fl["demo"]["ground_truth_used"] is False
    assert fl["demo"]["codex_response"]["repair"]["path"] == "src/value.c"
    assert fl["demo"]["codex_response_artifact"] == (
        "codex_artifacts/demo/attempt_01/response.json"
    )
    assert apr["demo"]["status"] == "plausible"
    assert apr["demo"]["patched_function"] == "int value(void) { return 1; }"
    assert (results_dir / apr["demo"]["patched_file_artifact"]).is_file()
    assert apr["demo"]["codex_response"]["repair"]["diff"].startswith(
        "--- a/src/value.c"
    )
    assert apr["demo"]["codex_response_artifact"] == (
        "codex_artifacts/demo/attempt_01/response.json"
    )
    assert (
        results_dir / apr["demo"]["codex_response_artifact"]
    ).is_file()
    assert fl["demo"]["codex_responses"][0]["response"]["summary"] == (
        "fix return value"
    )
