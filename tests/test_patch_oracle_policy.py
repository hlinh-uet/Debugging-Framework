from pathlib import Path

from src.loaders.project import Project
from src.utils.workspace import non_repairable_patch_paths
from src.validation.project import ProjectValidator


def test_patch_allowlist_accepts_only_production_cpp_paths() -> None:
    assert non_repairable_patch_paths(["src/parser.cpp", "include/parser.hpp"]) == []
    assert non_repairable_patch_paths(
        [
            "Makefile",
            "CMakeLists.txt",
            "Dockerfile",
            ".debugging-framework.json",
            "tests/parser_test.cpp",
            "scripts/run_tests.sh",
        ]
    ) == [
        ".debugging-framework.json",
        "CMakeLists.txt",
        "Dockerfile",
        "Makefile",
        "scripts/run_tests.sh",
        "tests/parser_test.cpp",
    ]


def test_validator_rejects_patch_that_replaces_test_execution(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "main.c").write_text("int main(void) { return 1; }\n", encoding="utf-8")
    (project_root / "Makefile").write_text(
        "test:\n\t./test_binary\n",
        encoding="utf-8",
    )
    diff = (
        "diff --git a/Makefile b/Makefile\n"
        "--- a/Makefile\n"
        "+++ b/Makefile\n"
        "@@ -1,2 +1,2 @@\n"
        " test:\n"
        "-\t./test_binary\n"
        "+\t@echo '1 tests passed'\n"
    )

    result = ProjectValidator(environment_backend="host").validate_diff(
        project=Project(path=project_root, project_id="project"),
        diff=diff,
        patch_paths=["Makefile"],
        artifact_dir=tmp_path / "artifacts",
        failing_tests=("test_bug",),
    )

    assert result["status"] == "invalid"
    assert result["test_oracle_modified"] is True
    assert result["blocked_patch_paths"] == ["Makefile"]
    assert result["validation_executed"] is False
