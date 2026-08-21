from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from src.utils.project_config import load_project_config, load_project_config_file


class WorkspaceError(RuntimeError):
    pass


# The partner-facing repair contract targets C/C++. Keeping the automatic
# patch allowlist deliberately narrow prevents an APR candidate from making its
# own oracle pass by changing a Makefile, CMake configuration, Dockerfile,
# custom test runner, or another executable validation artifact.  Projects
# that genuinely need a build/test-infrastructure repair require a separate,
# explicitly reviewed workflow rather than the automatic ``plausible`` path.
C_CPP_PRODUCTION_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".inl", ".inc",
}


def source_extensions_for_project(project) -> set[str]:
    config = (
        load_project_config_file(project.config_path)
        if project.config_path is not None
        else load_project_config(Path(project.path).resolve())
    )
    extensions = set(C_CPP_PRODUCTION_EXTENSIONS)
    extensions.update(config.repair.source_extensions)
    return extensions

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


def is_production_source_path(
    value: object,
    source_extensions: set[str] | frozenset[str] | None = None,
) -> bool:
    """Accept source files while rejecting tests and generated/build outputs."""
    relpath = normalize_relpath(value)
    extensions = set(source_extensions or C_CPP_PRODUCTION_EXTENSIONS)
    if not relpath or Path(relpath).suffix.lower() not in extensions:
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


def non_repairable_patch_paths(
    values: list[str] | tuple[str, ...],
    source_extensions: set[str] | frozenset[str] | None = None,
) -> list[str]:
    """Return paths that may change the oracle or are not configured source.

    This is an enforcement boundary, not a prompt hint.  A path must be a
    production source/header according to both the repository path policy and
    the extension allowlist before its patch can be classified plausible.
    """
    extensions = set(source_extensions or C_CPP_PRODUCTION_EXTENSIONS)
    blocked: list[str] = []
    for value in values:
        relpath = normalize_relpath(value)
        if (
            not relpath
            or Path(relpath).suffix.lower() not in extensions
            or not is_production_source_path(relpath, extensions)
        ):
            blocked.append(relpath or str(value))
    return sorted(dict.fromkeys(blocked))


class ProjectWorkspace:
    """Operate on the supplied project through a recoverable Git baseline.

    The source tree is never copied. Existing repositories must be completely
    clean. A non-Git source export is accepted only when its project contract
    explicitly marks it disposable and authorizes creating temporary Git
    metadata. Every attempt starts from the same baseline commit and ``close``
    restores the caller's original branch/HEAD or removes framework-created
    Git metadata.
    """

    def __init__(self, project, workspace_parent: Path):
        self.project = project
        self.owner = Path(project.path).resolve()
        self.parent = workspace_parent.resolve()
        try:
            self.parent.relative_to(self.owner)
        except ValueError:
            pass
        else:
            raise WorkspaceError("Workspace metadata must be outside the input project")
        self.path = self.owner
        self.snapshot_commit = ""
        self.original_head = ""
        self.original_branch = ""
        self.framework_created_git = False
        self._entered = False
        self._restored = False
        self._lock_handle = None
        self._exclude_path: Path | None = None
        self._exclude_existed = False
        self._exclude_content = b""
        # ``workspace_parent`` itself is transient and is intentionally removed
        # between runs. Keep recovery state one level above it so an interrupted
        # in-place repair can be recovered before the next attempt starts.
        self._recovery_path = self.parent.parent / "workspace-recovery.json"
        config = (
            load_project_config_file(project.config_path)
            if project.config_path is not None
            else load_project_config(self.owner)
        )
        self.workspace_config = config.workspace
        self.source_extensions = source_extensions_for_project(project)

    def __enter__(self) -> "ProjectWorkspace":
        self.reserve()
        self._entered = True
        try:
            # ``git rev-parse`` walks through parent directories. A disposable
            # benchmark input may therefore appear to belong to the outer
            # defects4c checkout even though it has no repository of its own.
            # Only an exact root match represents this project's repository.
            if self._git_root() != self.owner:
                if not (
                    self.workspace_config.disposable
                    and self.workspace_config.initialize_git_if_missing
                ):
                    raise WorkspaceError(
                        "Project is not a Git repository; only disposable source may "
                        "create a baseline with workspace.disposable=true and "
                        "workspace.initialize_git_if_missing=true"
                    )
                self._initialize_disposable_repository()
            else:
                self._prepare_existing_repository()
            # Persist the baseline immediately after detaching. Rewrite it after
            # installing excludes so their exact original bytes are recoverable.
            self._write_recovery_metadata()
            self._install_framework_excludes()
            self._write_recovery_metadata()
            self._assert_production_sources()
        except BaseException:
            self.close()
            raise
        return self

    def reserve(self) -> None:
        """Lock the project and recover a previously interrupted run.

        A pipeline reserves before touching its result directory, preventing a
        concurrent invocation from deleting artifacts owned by the active run.
        ``__enter__`` reuses the reservation and keeps it through restoration.
        """
        if self._lock_handle is not None:
            return
        if not self.owner.is_dir():
            raise WorkspaceError(f"Project does not exist: {self.owner}")
        self.parent.mkdir(parents=True, exist_ok=True)
        self._acquire_lock()
        try:
            self._recover_interrupted_run()
        except BaseException:
            self._release_lock()
            raise

    def release_reservation(self) -> None:
        """Release a reservation that has not entered an in-place repair."""
        if not self._entered:
            self._release_lock()

    def preflight(self) -> dict[str, object]:
        """Check the in-place contract without changing the supplied project."""
        if not self.owner.is_dir():
            raise WorkspaceError(f"Project does not exist: {self.owner}")
        root = self._git_root()
        if root != self.owner:
            if not (
                self.workspace_config.disposable
                and self.workspace_config.initialize_git_if_missing
            ):
                raise WorkspaceError(
                    "Project is not a Git repository and disposable baseline creation "
                    "has not been authorized: "
                    "disposable baseline"
                )
            return {
                "mode": "in_place",
                "git": "initialize-temporary",
                "disposable": True,
            }
        status = self._git_text(
            "status", "--porcelain=v1", "--untracked-files=all", timeout=60
        )
        if status.strip():
            raise WorkspaceError(
                "Git project must be clean before repair (tracked and untracked); "
                "commit/stash the changes or use a separate clone"
            )
        ignored = self._git_text(
            "status", "--porcelain=v1", "--ignored=matching", "--untracked-files=all",
            timeout=60,
        )
        ignored_entries = [
            line
            for line in ignored.splitlines()
            if line.startswith("!! ") and line[3:].rstrip("/") != ".debugging-framework"
        ]
        if ignored_entries and not self.workspace_config.disposable:
            preview = ", ".join(line[3:] for line in ignored_entries[:5])
            raise WorkspaceError(
                "Git project contains ignored files, so in-place recovery cannot be guaranteed; "
                f"use a clean clone (for example: {preview})"
            )
        head = self._git_text(
            "rev-parse", "--verify", "HEAD", timeout=30, allow_failure=True
        ).strip()
        if not head:
            raise WorkspaceError("Git project has no baseline commit")
        return {
            "mode": "in_place",
            "git": "existing",
            "disposable": self.workspace_config.disposable,
            "head": head,
        }

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        if not self._entered:
            return
        restore_error: Exception | None = None
        try:
            if self.snapshot_commit and (self.owner / ".git").exists():
                self._restore_baseline(remove_generated=True)
                if not self.framework_created_git:
                    if self.original_branch:
                        self._run_git("checkout", "-f", self.original_branch, timeout=120)
                    elif self.original_head:
                        self._run_git(
                            "checkout", "--detach", "-f", self.original_head, timeout=120
                        )
                    if self.original_head:
                        self._run_git("reset", "--hard", self.original_head, timeout=120)
                self._restored = True
            self._restore_framework_excludes()
            if self.framework_created_git:
                if (self.owner / ".git").is_dir():
                    shutil.rmtree(self.owner / ".git")
                if not self.snapshot_commit:
                    # Initialization failed before a baseline commit existed;
                    # only framework-owned Git metadata could have changed.
                    self._restored = True
            if self._restored and self._recovery_path.exists():
                self._recovery_path.unlink()
        except Exception as exc:  # keep recovery.json and temporary Git metadata
            restore_error = exc
        finally:
            self._release_lock()
            self._entered = False
        if restore_error is not None:
            raise WorkspaceError(f"Could not restore the input project: {restore_error}") from restore_error

    @property
    def restored(self) -> bool:
        return self._restored

    def _initialize_disposable_repository(self) -> None:
        self.framework_created_git = True
        # This phase can be expensive for a very large export. Record ownership
        # before creating Git metadata so interruption during add/commit is safe.
        self._write_recovery_metadata()
        self._run_git("init", timeout=60)
        self._run_git("config", "user.name", "Debugging Framework", timeout=30)
        self._run_git("config", "user.email", "debugging-framework@example.invalid", timeout=30)
        files = [
            path.relative_to(self.path).as_posix()
            for path in self.path.rglob("*")
            if (path.is_file() or path.is_symlink())
            and ".git" not in path.relative_to(self.path).parts
            and not any(
                part in {".codegraph", ".debugging-framework"}
                for part in path.relative_to(self.path).parts
            )
            and path.relative_to(self.path).as_posix() != "codegraph.json"
        ]
        for offset in range(0, len(files), 500):
            self._run_git("add", "-f", "--", *files[offset : offset + 500], timeout=120)
        self._run_git("commit", "--no-gpg-sign", "-m", "Project snapshot", timeout=180)
        self.snapshot_commit = self._git_text("rev-parse", "HEAD", timeout=30).strip()
        if not self.snapshot_commit:
            raise WorkspaceError("Could not determine the project snapshot commit")
        self._run_git("checkout", "--detach", "-f", self.snapshot_commit, timeout=120)

    def _prepare_existing_repository(self) -> None:
        contract = self.preflight()
        self.original_head = str(contract["head"])
        self.original_branch = self._git_text(
            "symbolic-ref", "--quiet", "--short", "HEAD", timeout=30, allow_failure=True
        ).strip()
        self.snapshot_commit = self.original_head
        # Preserve the caller's branch before detaching; even a process kill
        # immediately after checkout can then restore the exact original state.
        self._write_recovery_metadata()
        self._run_git("checkout", "--detach", "-f", self.snapshot_commit, timeout=120)

    def _assert_production_sources(self) -> None:
        files = self._git_paths("ls-tree", "-r", "--name-only", self.snapshot_commit)
        if not any(is_production_source_path(path, self.source_extensions) for path in files):
            raise WorkspaceError("Project has no supported production source files")

    def _git_root(self) -> Path | None:
        output = self._git_text(
            "rev-parse", "--show-toplevel", timeout=30, allow_failure=True
        ).strip()
        return Path(output).resolve() if output else None

    def _install_framework_excludes(self) -> None:
        raw_path = self._git_text("rev-parse", "--git-path", "info/exclude", timeout=30).strip()
        exclude_path = Path(raw_path)
        if not exclude_path.is_absolute():
            exclude_path = self.owner / exclude_path
        self._exclude_path = exclude_path.resolve()
        self._exclude_existed = self._exclude_path.exists()
        self._exclude_content = self._exclude_path.read_bytes() if self._exclude_existed else b""
        self._exclude_path.parent.mkdir(parents=True, exist_ok=True)
        current = self._exclude_content.decode("utf-8", errors="replace")
        suffix = "\n# Debugging Framework runtime artifacts\n.codegraph/\ncodegraph.json\n.debugging-framework/\n"
        self._exclude_path.write_text(current.rstrip("\n") + suffix, encoding="utf-8")

    def _restore_framework_excludes(self) -> None:
        if self._exclude_path is None:
            return
        if self._exclude_existed:
            self._exclude_path.write_bytes(self._exclude_content)
        elif self._exclude_path.exists():
            self._exclude_path.unlink()
        self._exclude_path = None

    def _write_recovery_metadata(self) -> None:
        value = {
            "schema_version": 1,
            "phase": "active" if self.snapshot_commit else "initializing",
            "project": str(self.owner),
            "snapshot_commit": self.snapshot_commit,
            "original_head": self.original_head,
            "original_branch": self.original_branch,
            "framework_created_git": self.framework_created_git,
            "exclude_recorded": self._exclude_path is not None,
            "exclude_existed": self._exclude_existed,
            "exclude_content_base64": base64.b64encode(
                self._exclude_content
            ).decode("ascii"),
        }
        temporary = self._recovery_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self._recovery_path)

    def _recover_interrupted_run(self) -> None:
        """Restore an interrupted in-place run before creating a new baseline."""
        if not self._recovery_path.is_file():
            return
        try:
            value = json.loads(self._recovery_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkspaceError(
                f"Could not read recovery metadata: {self._recovery_path}"
            ) from exc
        if str(value.get("project") or "") != str(self.owner):
            raise WorkspaceError(
                "Recovery metadata does not belong to the current project; source will not be changed automatically"
            )

        framework_created = bool(value.get("framework_created_git", False))
        git_entry = self.owner / ".git"
        if not git_entry.exists():
            # A temporary repository is removed only after its source has already
            # been reset. A crash between that removal and unlinking metadata is
            # therefore safe to finish here.
            if framework_created:
                self._recovery_path.unlink()
                return
            raise WorkspaceError(
                "The project Git repository disappeared while recovery metadata remains; "
                "manual inspection is required"
            )

        self.snapshot_commit = str(value.get("snapshot_commit") or "").strip()
        self.original_head = str(value.get("original_head") or "").strip()
        self.original_branch = str(value.get("original_branch") or "").strip()
        self.framework_created_git = framework_created
        if not self.snapshot_commit:
            if framework_created:
                # No source edit happens before the temporary baseline commit.
                # Remove a partial init/index/object database and retry cleanly.
                if git_entry.is_dir():
                    shutil.rmtree(git_entry)
                self._recovery_path.unlink()
                self.framework_created_git = False
                return
            raise WorkspaceError("Recovery metadata is missing snapshot_commit")
        recovered_commit = self._git_text(
            "rev-parse", "--verify", f"{self.snapshot_commit}^{{commit}}",
            timeout=30, allow_failure=True,
        ).strip()
        if not recovered_commit:
            raise WorkspaceError("No baseline commit remains to restore the project automatically")

        if bool(value.get("exclude_recorded", False)):
            raw_path = self._git_text(
                "rev-parse", "--git-path", "info/exclude", timeout=30
            ).strip()
            exclude_path = Path(raw_path)
            if not exclude_path.is_absolute():
                exclude_path = self.owner / exclude_path
            self._exclude_path = exclude_path.resolve()
            self._exclude_existed = bool(value.get("exclude_existed", False))
            encoded = str(value.get("exclude_content_base64") or "")
            try:
                self._exclude_content = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise WorkspaceError("Recovery metadata contains an invalid exclude backup") from exc

        try:
            self._restore_baseline(remove_generated=True)
            if not framework_created:
                if self.original_branch:
                    self._run_git("checkout", "-f", self.original_branch, timeout=120)
                elif self.original_head:
                    self._run_git(
                        "checkout", "--detach", "-f", self.original_head, timeout=120
                    )
                if self.original_head:
                    self._run_git("reset", "--hard", self.original_head, timeout=120)
            self._restore_framework_excludes()
            if framework_created and git_entry.is_dir():
                shutil.rmtree(git_entry)
            self._recovery_path.unlink()
        except Exception as exc:
            raise WorkspaceError(
                f"Could not automatically recover the interrupted repair: {exc}"
            ) from exc
        finally:
            # A successful recovery is followed by a completely fresh baseline.
            # On failure these values are also retained in recovery.json itself.
            self.snapshot_commit = ""
            self.original_head = ""
            self.original_branch = ""
            self.framework_created_git = False
            self._exclude_path = None
            self._exclude_existed = False
            self._exclude_content = b""
            self._restored = False

    def _acquire_lock(self) -> None:
        lock_root = Path(tempfile.gettempdir()).resolve() / "debugging-framework-locks"
        lock_root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(str(self.owner).encode("utf-8")).hexdigest()[:24]
        lock_path = lock_root / f"{digest}.lock"
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise WorkspaceError(f"Project is already being used by another repair: {self.owner}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\nproject={self.owner}\n")
        handle.flush()
        self._lock_handle = handle

    def _release_lock(self) -> None:
        if self._lock_handle is None:
            return
        try:
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._lock_handle.close()
            self._lock_handle = None

    def reset_to_snapshot(self) -> None:
        """Restore the supplied project to the repair run's baseline.

        CodeGraph's repository index is retained because it is read-only navigation
        metadata and can be reused across attempts. All tracked edits, intent-to-add
        entries, build output, and other untracked files are removed.
        """
        if not self.snapshot_commit:
            raise WorkspaceError("Project snapshot has not been initialized")
        self._run_git("checkout", "--detach", "-f", self.snapshot_commit, timeout=120)
        self._run_git("reset", "--hard", self.snapshot_commit, timeout=120)
        self._run_git(
            "clean", "-ffdx", "-e", ".codegraph/", "-e", "codegraph.json",
            "-e", ".debugging-framework/",
            timeout=180,
        )
        self._clean_framework_runtime()

    def _restore_baseline(self, *, remove_generated: bool) -> None:
        self._run_git("checkout", "--detach", "-f", self.snapshot_commit, timeout=120)
        self._run_git("reset", "--hard", self.snapshot_commit, timeout=120)
        arguments = ["clean", "-ffdx", "-e", ".debugging-framework/"]
        if not remove_generated:
            arguments.extend(["-e", ".codegraph/", "-e", "codegraph.json"])
        self._run_git(*arguments, timeout=180)
        self._clean_framework_runtime()

    def _clean_framework_runtime(self) -> None:
        root = self.owner / ".debugging-framework"
        for name in ("build", "environment", "venv", "bundle", "cache"):
            path = root / name
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                path.unlink()
        baseline = root / "baseline-output.txt"
        if baseline.exists() or baseline.is_symlink():
            baseline.unlink()

    def canonical_diff(self) -> str:
        """Return the exact Git diff represented by the workspace filesystem.

        ``git diff`` normally omits untracked files. Marking them intent-to-add makes
        newly created source files visible without staging their contents or changing
        the snapshot commit.
        """
        untracked = self._git_paths("ls-files", "--others", "--exclude-standard")
        for offset in range(0, len(untracked), 500):
            self._run_git(
                "add", "--intent-to-add", "--", *untracked[offset : offset + 500],
                timeout=120,
            )
        completed = subprocess.run(
            [
                "git", "-C", str(self.path), "diff", "--no-ext-diff", "--binary",
                "--full-index", "--no-renames", self.snapshot_commit, "--",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", timeout=120, check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip().splitlines()
            raise WorkspaceError(
                "workspace_diff_failed:"
                + (detail[-1] if detail else "unknown")
            )
        return completed.stdout

    def changed_repository_files(self) -> list[str]:
        diff = self.canonical_diff()
        return sorted(set(self.unified_diff_paths(diff))) if diff.strip() else []

    def unified_diff_paths(self, diff: str) -> list[str]:
        return self._unified_diff_paths(str(diff or ""))

    def snapshot_sha256s(self, relpaths: list[str]) -> dict[str, str | None]:
        hashes: dict[str, str | None] = {}
        for value in relpaths:
            relpath = normalize_relpath(value)
            if not relpath:
                raise WorkspaceError(f"Patch path is unsafe: {value}")
            completed = subprocess.run(
                [
                    "git", "-C", str(self.path), "cat-file", "--filters",
                    f"{self.snapshot_commit}:{relpath}",
                ],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60, check=False,
            )
            # Hash the checkout representation, not the raw blob. Attributes
            # such as PHP's ``ident`` filter legitimately transform bytes when
            # reset/checkout writes the baseline back to the filesystem.
            hashes[relpath] = (
                hashlib.sha256(completed.stdout).hexdigest()
                if completed.returncode == 0 else None
            )
        return hashes

    def apply_unified_diff(self, diff: str, expected_paths: list[str] | None = None) -> list[str]:
        return _apply_unified_diff(self.path, diff, expected_paths)

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
            raise WorkspaceError(f"git {' '.join(arguments)} failed: {detail}")
        return [item.decode("utf-8", errors="replace") for item in completed.stdout.split(b"\0") if item]

    def _run_git(self, *arguments: str, timeout: int) -> None:
        self._git_text(*arguments, timeout=timeout)

    def _git_text(self, *arguments: str, timeout: int, allow_failure: bool = False) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.path), *arguments],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
        if completed.returncode != 0 and not allow_failure:
            detail = (completed.stderr or completed.stdout).strip().splitlines()
            raise WorkspaceError(
                f"git {' '.join(arguments[:3])} failed: {detail[-1] if detail else 'unknown'}"
            )
        return completed.stdout


def _apply_unified_diff(
    workspace_path: Path,
    diff: str,
    expected_paths: list[str] | None = None,
) -> list[str]:
    text = str(diff or "")
    if not text.strip():
        raise WorkspaceError("validation_diff_empty")
    parsed = subprocess.run(
        ["git", "-C", str(workspace_path), "apply", "--numstat", "-z", "-"],
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
        candidate = workspace_path / relpath
        if candidate.is_symlink():
            raise WorkspaceError(f"validation_diff_targets_symlink:{relpath}")
        try:
            candidate.resolve(strict=False).relative_to(workspace_path)
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
        command = ["git", "-C", str(workspace_path), "apply", "--whitespace=nowarn"]
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
