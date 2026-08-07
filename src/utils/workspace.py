from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
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


_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?P<context>.*)$"
)
_DIFF_FENCE = re.compile(r"^```(?:diff|patch|unified-diff)?$", re.IGNORECASE)


def normalize_unified_diff(value: object) -> tuple[str, list[str]]:
    """Normalize only deterministic unified-diff presentation metadata.

    The returned text never changes an added, removed, or context line.  It may
    remove standalone Markdown/``apply_patch`` wrappers, normalize line endings,
    and correct hunk line counts when every hunk body line is structurally valid.
    Missing file headers, missing hunk content, and path selection are left for
    the normal diff parser to reject rather than guessed here.
    """
    original = str(value or "")
    text = original.replace("\r\n", "\n").replace("\r", "\n")
    actions: list[str] = []
    if text != original:
        actions.append("line_endings_normalized")
    if text.startswith("\ufeff"):
        text = text[1:]
        actions.append("bom_removed")

    had_terminal_newline = text.endswith("\n")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    cleaned: list[str] = []
    for line in lines:
        stripped = line.strip()
        is_diff_body_line = line.startswith((" ", "+", "-"))
        if stripped in {"*** Begin Patch", "*** End Patch"} and not is_diff_body_line:
            if "patch_wrapper_removed" not in actions:
                actions.append("patch_wrapper_removed")
            continue
        if _DIFF_FENCE.fullmatch(stripped) and not is_diff_body_line:
            if "markdown_fence_removed" not in actions:
                actions.append("markdown_fence_removed")
            continue
        cleaned.append(line)
    before_outer_trim = len(cleaned)
    while cleaned and not cleaned[0].strip():
        cleaned.pop(0)
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    if len(cleaned) != before_outer_trim:
        actions.append("outer_blank_lines_removed")
    if cleaned and not had_terminal_newline:
        actions.append("final_newline_added")

    hunk_changed = _normalize_hunk_counts(cleaned)
    if hunk_changed:
        actions.append("hunk_counts_recomputed")
    if not cleaned:
        return "", actions
    return "\n".join(cleaned) + "\n", actions


def _normalize_hunk_counts(lines: list[str]) -> bool:
    changed = False
    index = 0
    while index < len(lines):
        match = _HUNK_HEADER.match(lines[index])
        if not match:
            index += 1
            continue
        old_count_declared = int(match.group("old_count") or 1)
        new_count_declared = int(match.group("new_count") or 1)
        old_count = 0
        new_count = 0
        body_valid = True
        body_seen = False
        cursor = index + 1
        while cursor < len(lines):
            line = lines[cursor]
            if line.startswith("@@ ") or line.startswith("diff --git "):
                break
            if (
                line.startswith("--- ")
                and cursor + 1 < len(lines)
                and lines[cursor + 1].startswith("+++ ")
                and old_count == old_count_declared
                and new_count == new_count_declared
            ):
                break
            if line == r"\ No newline at end of file":
                cursor += 1
                continue
            if not line or line[0] not in {" ", "+", "-"}:
                body_valid = False
                break
            body_seen = True
            if line[0] == "+":
                new_count += 1
            elif line[0] == "-":
                old_count += 1
            else:
                old_count += 1
                new_count += 1
            cursor += 1
        if body_valid and body_seen and (
            old_count != old_count_declared or new_count != new_count_declared
        ):
            old_count_text = (
                f",{old_count}"
                if match.group("old_count") is not None or old_count != 1
                else ""
            )
            new_count_text = (
                f",{new_count}"
                if match.group("new_count") is not None or new_count != 1
                else ""
            )
            lines[index] = (
                f"@@ -{match.group('old_start')}{old_count_text} "
                f"+{match.group('new_start')}{new_count_text} @@{match.group('context')}"
            )
            changed = True
        index = max(cursor, index + 1)
    return changed


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
            ".debugging-framework/venv/\n.debugging-framework/bundle/\n"
            ".debugging-framework/baseline-output.txt\n",
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

    def snapshot_sha256s(self, relpaths: list[str]) -> dict[str, str | None]:
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


class CurrentProjectWorkspace(ProjectWorkspace):
    """Use the supplied project directory directly without making a copy.

    This mode is intentional: Codex edits the user's current checkout and those
    edits remain there after the run.  A private snapshot of the initial project
    is captured before Codex starts and is used as the clean validation source;
    the live checkout is never temporarily reverted during validation.
    """

    in_place = True

    def __init__(self, project):
        super().__init__(project, Path(project.path).resolve().parent)
        self.path = self.owner
        self.parent = self.owner.parent
        self._context_file: Path | None = None
        self._context_original: bytes | None = None
        self._context_existed = False
        self._context_dir: Path | None = None
        self._context_dir_existed = False
        self._initial_diff = b""
        self._initial_files: dict[str, bytes] = {}
        self._captured = False
        self._validation_base = None

    def __enter__(self) -> "CurrentProjectWorkspace":
        if not self.owner.is_dir():
            raise WorkspaceError(f"Project không tồn tại: {self.owner}")
        if self._captured:
            return self
        self._initial_files = self._file_state()
        if (self.owner / ".git").exists():
            self._initial_diff = self._git_diff_binary()
        self._validation_base = ValidationWorkspace(self.project)
        try:
            self._validation_base.__enter__()
        except BaseException:
            self._validation_base = None
            raise
        self._captured = True
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        context_file = self._context_file
        if context_file is not None:
            if self._context_existed:
                context_file.parent.mkdir(parents=True, exist_ok=True)
                context_file.write_bytes(self._context_original or b"")
            elif context_file.exists() or context_file.is_symlink():
                context_file.unlink()
        context_dir = self._context_dir
        if (
            context_dir is not None
            and not self._context_dir_existed
            and context_dir.is_dir()
            and not any(context_dir.iterdir())
        ):
            context_dir.rmdir()
        self._context_file = None
        self._context_dir = None
        if self._validation_base is not None:
            self._validation_base.close()
            self._validation_base = None
        self._captured = False

    def write_baseline_context(self, text: str) -> None:
        context_dir = self.owner / ".debugging-framework"
        if context_dir.exists() and context_dir.is_symlink():
            raise WorkspaceError(".debugging-framework là symlink; không ghi baseline context")
        if context_dir.exists() and not context_dir.is_dir():
            raise WorkspaceError(".debugging-framework không phải thư mục")
        context_dir_existed = context_dir.exists()
        context_dir.mkdir(parents=True, exist_ok=True)
        context_file = context_dir / "baseline-output.txt"
        if context_file.is_symlink():
            raise WorkspaceError("baseline-output.txt là symlink; không ghi đè")
        if context_file.exists() and not context_file.is_file():
            raise WorkspaceError("baseline-output.txt không phải file")
        if self._context_file is None:
            self._context_file = context_file
            self._context_dir = context_dir
            self._context_existed = context_file.exists()
            self._context_dir_existed = context_dir_existed
            if self._context_existed:
                self._context_original = context_file.read_bytes()
        context_file.write_text(str(text or ""), encoding="utf-8", errors="replace")

    def changed_repository_files(self) -> list[str]:
        if (self.owner / ".git").exists():
            changed = set(super().changed_repository_files())
            current = self._file_state()
            changed.update(
                path for path in set(current) | set(self._initial_files)
                if current.get(path) != self._initial_files.get(path)
            )
            return sorted(changed)
        current = self._file_state()
        return sorted(
            path for path in set(current) | set(self._initial_files)
            if current.get(path) != self._initial_files.get(path)
        )

    def unified_diff_paths(self, diff: str) -> list[str]:
        text = str(diff or "")
        if (self.owner / ".git").exists():
            return self._unified_diff_paths(text)
        # ProjectLoader supports non-git projects. Keep path extraction safe in
        # direct mode even when there is no repository for `git apply` to use.
        paths: list[str] = []
        for line in text.splitlines():
            if not line.startswith("diff --git "):
                if line.startswith("+++ b/"):
                    relpath = normalize_relpath(line[6:])
                    if relpath and relpath not in paths:
                        paths.append(relpath)
                continue
            fields = line.split()
            if len(fields) != 4:
                raise WorkspaceError("codex_diff_parse_failed:invalid diff header")
            relpath = normalize_relpath(fields[3][2:] if fields[3].startswith("b/") else "")
            if not relpath:
                raise WorkspaceError("codex_diff_contains_unsafe_path")
            if relpath not in paths:
                paths.append(relpath)
        if not paths:
            raise WorkspaceError("codex_diff_parse_failed:unified diff has no files")
        return paths

    def snapshot_sha256s(self, relpaths: list[str]) -> dict[str, str | None]:
        # Codex is allowed to edit the current checkout; comparing its files to
        # a pre-Codex hash would reject the intended in-place workflow.
        return {}

    @contextmanager
    def clean_source_for_validation(self):
        """Yield the pre-Codex snapshot without mutating the live checkout."""
        if self._validation_base is None or not self._validation_base.path.is_dir():
            raise WorkspaceError("initial_validation_snapshot_unavailable")
        yield self._validation_base.path

    def _git_diff_binary(self) -> bytes:
        completed = subprocess.run(
            ["git", "-C", str(self.owner), "diff", "--binary", "--no-ext-diff"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise WorkspaceError(f"git diff lỗi: {detail or 'unknown'}")
        return completed.stdout

    def _file_state(self) -> dict[str, bytes]:
        state: dict[str, bytes] = {}
        for path in self.owner.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(self.owner)
            parts = {part.lower() for part in relative.parts}
            if ".git" in parts or parts & GENERATED_PATH_COMPONENTS:
                continue
            try:
                state[relative.as_posix()] = path.read_bytes()
            except OSError:
                continue
        return state

    def _restore_file_state(
        self, target: dict[str, bytes], current: dict[str, bytes]
    ) -> None:
        changed = set(target) | set(current)
        for relative in changed:
            if target.get(relative) == current.get(relative):
                continue
            path = self.owner / relative
            if relative in target:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(target[relative])
            elif path.exists() or path.is_symlink():
                path.unlink()

    def _apply_diff(self, diff: bytes, *, reverse: bool = False) -> None:
        command = ["git", "-C", str(self.owner), "apply"]
        if reverse:
            command.append("--reverse")
        completed = subprocess.run(
            command,
            input=diff,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise WorkspaceError(f"git apply lỗi: {detail or 'unknown'}")


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
