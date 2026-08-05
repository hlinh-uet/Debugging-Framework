from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from src.utils.jsonio import safe_name


class WorkspaceError(RuntimeError):
    pass


SOURCE_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".inl", ".inc",
    ".py", ".pyi", ".java", ".kt", ".kts", ".scala", ".go", ".rs", ".swift",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".rb", ".php", ".cs", ".fs",
    ".ex", ".exs", ".erl", ".hrl", ".lua", ".r", ".dart", ".sh", ".m", ".mm",
    ".vue", ".svelte", ".sol", ".clj", ".cljs", ".hs", ".lhs",
}

TEST_PATH_COMPONENTS = {
    "test", "tests", "testing", "testsuite", "testcases", "spec", "specs",
    "__tests__", "fixtures", "testdata", "unittest", "unittests", "utest", "utests",
}

GENERATED_PATH_COMPONENTS = {
    "build", "dist", "target", "node_modules", ".venv", "venv", "vendor",
    "generated", ".debugging-framework",
}


def normalize_relpath(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts:
        return ""
    return str(path)


def is_production_source_path(value: object) -> bool:
    """Accept source files while rejecting tests and generated/build outputs."""
    relpath = normalize_relpath(value)
    if not relpath or Path(relpath).suffix.lower() not in SOURCE_EXTENSIONS:
        return False
    parts = [part.lower() for part in PurePosixPath(relpath).parts]
    if any(part in TEST_PATH_COMPONENTS | GENERATED_PATH_COMPONENTS for part in parts[:-1]):
        return False
    stem = Path(parts[-1]).stem
    return not (
        stem.startswith(("test_", "spec_"))
        or stem.endswith(("_test", "_tests", "_spec"))
        or ".test." in parts[-1]
        or ".spec." in parts[-1]
    )


class ProjectWorkspace:
    """A disposable, history-free snapshot that the repair agent may modify."""

    def __init__(self, project, workspace_parent: Path):
        self.project = project
        self.owner = Path(project.path).resolve()
        self.parent = workspace_parent.resolve()
        try:
            self.parent.relative_to(self.owner)
        except ValueError:
            pass
        else:
            self.parent = (
                Path(tempfile.gettempdir()).resolve() / "debugging-framework-llm"
            )
        self.path = self.parent / f"{safe_name(project.project_id, 70)}-{uuid.uuid4().hex[:10]}"

    def __enter__(self) -> "ProjectWorkspace":
        if not self.owner.is_dir():
            raise WorkspaceError(f"Project không tồn tại: {self.owner}")
        self.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            raise WorkspaceError(f"Workspace target đã tồn tại: {self.path}")
        shutil.copytree(
            self.owner,
            self.path,
            symlinks=True,
            ignore=self._llm_ignored_paths,
        )
        try:
            self._initialize_snapshot_repository()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        if not self.path.exists():
            return
        resolved = self.path.resolve()
        if resolved.parent != self.parent or not resolved.name.startswith(
            safe_name(self.project.project_id, 70) + "-"
        ):
            raise WorkspaceError(f"Từ chối xoá workspace path không mong đợi: {resolved}")
        shutil.rmtree(resolved)

    def _initialize_snapshot_repository(self) -> None:
        self._run_git("init", timeout=60)
        self._run_git("config", "user.name", "Debugging Framework", timeout=30)
        self._run_git("config", "user.email", "debugging-framework@example.invalid", timeout=30)
        (self.path / ".git" / "info" / "exclude").write_text(
            "node_modules/\n.venv/\nvenv/\ntarget/\nbuild/\ndist/\n"
            ".debugging-framework/build/\n.debugging-framework/environment/\n"
            ".debugging-framework/venv/\n.debugging-framework/bundle/\n",
            encoding="utf-8",
        )
        files = [
            path.relative_to(self.path).as_posix()
            for path in self.path.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and ".git" not in path.relative_to(self.path).parts
            and not any(
                part in {"node_modules", ".venv", "venv", "target", "build", "dist"}
                for part in path.relative_to(self.path).parts[:-1]
            )
        ]
        if not any(is_production_source_path(path) for path in files):
            raise WorkspaceError("Project không có production source file được hỗ trợ")
        for offset in range(0, len(files), 500):
            self._run_git("add", "-f", "--", *files[offset : offset + 500], timeout=120)
        self._run_git("commit", "--no-gpg-sign", "-m", "Project snapshot", timeout=180)

    def _llm_ignored_paths(self, directory: str, names: list[str]) -> set[str]:
        current = Path(directory).resolve()
        try:
            relative = current.relative_to(self.owner)
        except ValueError:
            return set()
        ignored = {
            name for name in names
            if name in {
                "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
            }
        }
        if relative == Path("."):
            ignored.update(name for name in names if name in {".git", ".env", "build", "dist", "target"})
        elif ".git" in names:
            ignored.add(".git")
        if relative.as_posix() == ".debugging-framework":
            ignored.update({"build", "environment", "venv", "bundle", "cache"})
            ignored.update(
                name for name in names
                if Path(name).suffix.lower() in {".log", ".xml", ".status", ".msg"}
            )
        return ignored

    def changed_repository_files(self) -> list[str]:
        return sorted(set(self._git_paths("diff", "--name-only")))

    def unified_diff_paths(self, diff: str) -> list[str]:
        return self._unified_diff_paths(str(diff or ""))

    def baseline_sha256s(self, relpaths: list[str]) -> dict[str, str | None]:
        hashes: dict[str, str | None] = {}
        for value in relpaths:
            relpath = normalize_relpath(value)
            if not relpath:
                raise WorkspaceError(f"Patch path không an toàn: {value}")
            completed = subprocess.run(
                ["git", "-C", str(self.path), "show", f"HEAD:{relpath}"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60, check=False,
            )
            hashes[relpath] = (
                hashlib.sha256(completed.stdout).hexdigest()
                if completed.returncode == 0 else None
            )
        return hashes

    def _unified_diff_paths(self, text: str) -> list[str]:
        completed = subprocess.run(
            ["git", "-C", str(self.path), "apply", "--numstat", "-z", "-"],
            input=text.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=120, check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip().splitlines()
            raise WorkspaceError(
                "codex_diff_parse_failed: " + (detail[-1] if detail else "unknown")
            )
        paths = []
        for record in completed.stdout.split(b"\0"):
            if not record:
                continue
            fields = record.split(b"\t", 2)
            if len(fields) != 3:
                raise WorkspaceError("codex_diff_numstat_invalid")
            relpath = normalize_relpath(fields[2].decode("utf-8", errors="replace"))
            if not relpath:
                raise WorkspaceError("codex_diff_contains_unsafe_path")
            paths.append(relpath)
        return paths

    def _git_paths(self, *arguments: str) -> list[str]:
        completed = subprocess.run(
            ["git", "-C", str(self.path), *arguments, "-z"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode(errors="replace").strip()
            raise WorkspaceError(f"git {' '.join(arguments)} lỗi: {detail}")
        return [item.decode("utf-8", errors="replace") for item in completed.stdout.split(b"\0") if item]

    def _run_git(self, *arguments: str, timeout: int) -> None:
        completed = subprocess.run(
            ["git", "-C", str(self.path), *arguments],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip().splitlines()
            raise WorkspaceError(
                f"git {' '.join(arguments[:3])} lỗi: {detail[-1] if detail else 'unknown'}"
            )


class ValidationWorkspace:
    """A disposable project copy used for build/test without touching the input."""

    def __init__(self, project):
        self.project = project
        self.owner = Path(project.path).resolve()
        self.parent = Path(tempfile.gettempdir()).resolve() / "debugging-framework-validation"
        self.path = self.parent / f"{safe_name(project.project_id, 70)}-{uuid.uuid4().hex[:10]}"

    def __enter__(self) -> "ValidationWorkspace":
        if not self.owner.is_dir():
            raise WorkspaceError(f"Project không tồn tại: {self.owner}")
        self.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            raise WorkspaceError(f"Validation workspace đã tồn tại: {self.path}")
        shutil.copytree(
            self.owner,
            self.path,
            symlinks=True,
            ignore=self._ignored_paths,
        )
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        if not self.path.exists():
            return
        resolved = self.path.resolve()
        if resolved.parent != self.parent or not resolved.name.startswith(
            safe_name(self.project.project_id, 70) + "-"
        ):
            raise WorkspaceError(f"Từ chối xoá validation workspace: {resolved}")
        shutil.rmtree(resolved)

    def apply_unified_diff(self, diff: str, expected_paths: list[str] | None = None) -> list[str]:
        text = str(diff or "")
        if not text.strip():
            raise WorkspaceError("validation_diff_empty")
        parsed = subprocess.run(
            ["git", "-C", str(self.path), "apply", "--numstat", "-z", "-"],
            input=text.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=120, check=False,
        )
        if parsed.returncode != 0:
            detail = parsed.stderr.decode("utf-8", errors="replace").strip().splitlines()
            raise WorkspaceError(
                "validation_diff_parse_failed:" + (detail[-1] if detail else "unknown")
            )
        paths = []
        for record in parsed.stdout.split(b"\0"):
            if not record:
                continue
            fields = record.split(b"\t", 2)
            relpath = normalize_relpath(
                fields[2].decode("utf-8", errors="replace") if len(fields) == 3 else ""
            )
            if not relpath or PurePosixPath(relpath).parts[0] == ".git":
                raise WorkspaceError("validation_diff_contains_unsafe_path")
            candidate = self.path / relpath
            if candidate.is_symlink():
                raise WorkspaceError(f"validation_diff_targets_symlink:{relpath}")
            try:
                candidate.resolve(strict=False).relative_to(self.path)
            except ValueError as exc:
                raise WorkspaceError(f"validation_diff_path_escape:{relpath}") from exc
            paths.append(relpath)
        if not paths:
            raise WorkspaceError("validation_diff_changed_path_count:0")
        if expected_paths is not None and paths != expected_paths:
            raise WorkspaceError(
                "validation_diff_path_mismatch:"
                + ",".join(expected_paths)
                + ":"
                + ",".join(paths)
            )
        for check in (True, False):
            command = ["git", "-C", str(self.path), "apply", "--whitespace=nowarn"]
            if check:
                command.append("--check")
            completed = subprocess.run(
                [*command, "-"], input=text, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", timeout=120, check=False,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip().splitlines()
                raise WorkspaceError(
                    "validation_diff_apply_failed:"
                    + (detail[-1] if detail else "unknown")
                )
        return paths

    def _ignored_paths(self, directory: str, names: list[str]) -> set[str]:
        current = Path(directory).resolve()
        try:
            relative = current.relative_to(self.owner)
        except ValueError:
            return set()
        ignored = {
            name for name in names
            if name in {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
        }
        # A real .git directory is copied so version-derived builds keep working,
        # but a worktree/submodule .git pointer is excluded because it can write
        # back into the input repository's administrative directory.
        if ".git" in names and not (relative == Path(".") and (self.owner / ".git").is_dir()):
            ignored.add(".git")
        if relative == Path("."):
            ignored.update(name for name in names if name in {"build", "dist", "target"})
            ignored.update(
                child.name
                for child in self.owner.iterdir()
                if child.is_dir() and (child / "CMakeCache.txt").is_file()
            )
        if relative.as_posix() == ".debugging-framework":
            ignored.update({"build", "environment", "venv", "bundle", "cache"})
            ignored.update(
                name for name in names
                if Path(name).suffix.lower() in {".log", ".xml", ".status", ".msg"}
            )
        return ignored
