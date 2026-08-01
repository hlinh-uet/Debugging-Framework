from __future__ import annotations

import difflib
import hashlib
import os
import shutil
import subprocess
import uuid
from pathlib import Path, PurePosixPath

from src.utils.jsonio import safe_name


class WorkspaceError(RuntimeError):
    pass


SOURCE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
    ".inl",
    ".inc",
}


def normalize_relpath(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts:
        return ""
    return str(path)


def allowed_source_files(raw: dict) -> list[str]:
    values = raw.get("src_files") if isinstance(raw, dict) else None
    if not isinstance(values, list) or not values:
        files = raw.get("files", {}) if isinstance(raw, dict) else {}
        values = files.get("src") if isinstance(files, dict) else None
    if not isinstance(values, list) or not values:
        values = [raw.get("source_relpath")] if isinstance(raw, dict) else []
    normalized = [normalize_relpath(value) for value in values]
    return list(dict.fromkeys(value for value in normalized if value))


def repair_candidate_files(bug) -> list[str]:
    """Resolve source candidates from failing-test coverage, never GT fields."""
    raw = dict(getattr(bug, "raw", None) or {})
    repository = Path(str(raw.get("buggy_tree_dir") or ""))
    hints = []
    for test in getattr(bug, "tests", None) or []:
        if not isinstance(test, dict):
            continue
        if str(test.get("outcome") or "").strip().upper() not in {"FAIL", "FAILED"}:
            continue
        coverage = test.get("covered_functions")
        if not isinstance(coverage, list):
            coverage = test.get("covered_methods")
        for key in coverage if isinstance(coverage, list) else []:
            hint = _coverage_file_hint(key)
            if hint:
                hints.append(hint)
    hints = list(dict.fromkeys(hints))
    tracked = _tracked_source_files(repository)
    candidates = []
    for hint in hints:
        normalized_hint = hint.replace("\\", "/").lstrip("./")
        for relpath in tracked:
            if "/" in normalized_hint:
                matches = relpath == normalized_hint or relpath.endswith("/" + normalized_hint)
            else:
                matches = os.path.basename(relpath) == normalized_hint
            if matches:
                candidates.append(relpath)
    candidates = list(dict.fromkeys(candidates))
    # Some metadata lacks usable coverage. The loader's source target is then
    # the only actionable input available, so retain it as an explicit fallback.
    return candidates or allowed_source_files(raw)


def _coverage_file_hint(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    lower = text.lower()
    for extension in sorted(SOURCE_EXTENSIONS, key=len, reverse=True):
        marker = lower.find(extension)
        if marker < 0:
            continue
        end = marker + len(extension)
        if end < len(text) and text[end] == ":":
            return text[:end]
        if end == len(text):
            return text
    return ""


def _tracked_source_files(repository: Path) -> list[str]:
    if not repository.is_dir():
        return []
    completed = subprocess.run(
        ["git", "-C", str(repository), "ls-files", "-z"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        return []
    out = []
    for item in completed.stdout.split(b"\0"):
        if not item:
            continue
        relpath = normalize_relpath(item.decode("utf-8", errors="replace"))
        if relpath and Path(relpath).suffix.lower() in SOURCE_EXTENSIONS:
            out.append(relpath)
    return out


class BugWorkspace:
    """Disposable worktree containing the benchmark's buggy source overlay."""

    def __init__(
        self,
        bug,
        worktree_parent: Path,
        repair_candidates: list[str] | None = None,
    ):
        self.bug = bug
        self.raw = dict(getattr(bug, "raw", None) or {})
        self.owner_worktree = Path(str(self.raw.get("buggy_tree_dir") or ""))
        self.commit_after = str(self.raw.get("commit_after") or "").strip()
        self.commit_before = str(self.raw.get("commit_before") or "").strip()
        self.overlay_files = allowed_source_files(self.raw)
        selected_candidates = repair_candidates or repair_candidate_files(bug)
        self.allowed = list(
            dict.fromkeys(
                path for path in (normalize_relpath(item) for item in selected_candidates) if path
            )
        )
        self.parent = worktree_parent.resolve()
        self.path = self.parent / (
            f"{safe_name(getattr(bug, 'bug_id', 'bug'), 70)}-{uuid.uuid4().hex[:10]}"
        )
        self._baseline: dict[str, bytes] = {}
        self._baseline_cached: set[str] = set()
        self._owner_git_marker: bytes | None = None

    def __enter__(self) -> "BugWorkspace":
        if not self.owner_worktree.is_dir():
            raise WorkspaceError(f"Buggy worktree không tồn tại: {self.owner_worktree}")
        if not self.commit_after or not self.commit_before:
            raise WorkspaceError("Metadata thiếu commit_after/commit_before")
        if not self.overlay_files:
            raise WorkspaceError("Metadata không có buggy source overlay")
        if not self.allowed:
            raise WorkspaceError("Không resolve được production-source candidate hợp lệ")

        self.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            raise WorkspaceError(f"Worktree target đã tồn tại: {self.path}")
        self._run_git(
            self.owner_worktree,
            "worktree",
            "add",
            "--detach",
            "--force",
            str(self.path),
            self.commit_after,
            timeout=180,
        )
        try:
            self._run_git(
                self.path,
                "checkout",
                "--force",
                self.commit_before,
                "--",
                *self.overlay_files,
                timeout=120,
            )
            for relpath in self.allowed:
                source = self.path / relpath
                if not source.is_file():
                    raise WorkspaceError(f"Allowed source không tồn tại: {relpath}")
                self._baseline[relpath] = source.read_bytes()
            self._isolate_git_history()
            self._baseline_cached = set(self._git_paths("diff", "--cached", "--name-only"))
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        if self.path.exists():
            self._restore_owner_git_marker()
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.owner_worktree),
                    "worktree",
                    "remove",
                    "--force",
                    str(self.path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
                check=False,
            )
        if self.path.exists():
            resolved = self.path.resolve()
            if resolved.parent == self.parent and resolved.name.startswith(
                safe_name(getattr(self.bug, "bug_id", "bug"), 70) + "-"
            ):
                shutil.rmtree(resolved)

    def _isolate_git_history(self) -> None:
        """Replace the linked worktree metadata with a one-commit buggy repo.

        Defects4C's cached worktree has the fixed commit as HEAD and a staged
        buggy overlay. Exposing that git state would let an agent recover the
        accepted fix with `git diff --cached`. The Codex-facing repository is
        therefore reinitialized with only the buggy snapshot in its history.
        """
        marker = self.path / ".git"
        if not marker.is_file() or marker.is_symlink():
            raise WorkspaceError(f"Git worktree marker không hợp lệ: {marker}")
        self._owner_git_marker = marker.read_bytes()
        marker.unlink()
        try:
            self._run_git(self.path, "init", timeout=60)
            self._run_git(self.path, "config", "user.name", "Debugging Framework", timeout=30)
            self._run_git(
                self.path,
                "config",
                "user.email",
                "debugging-framework@example.invalid",
                timeout=30,
            )
            self._run_git(self.path, "add", "-A", timeout=120)
            self._run_git(
                self.path,
                "commit",
                "--no-gpg-sign",
                "-m",
                "Buggy benchmark snapshot",
                timeout=120,
            )
        except BaseException:
            self._restore_owner_git_marker()
            raise

    def _restore_owner_git_marker(self) -> None:
        if self._owner_git_marker is None or not self.path.exists():
            return
        marker = self.path / ".git"
        if marker.is_dir() and not marker.is_symlink():
            shutil.rmtree(marker)
        elif marker.exists() or marker.is_symlink():
            marker.unlink()
        marker.write_bytes(self._owner_git_marker)
        self._owner_git_marker = None

    def changed_source_files(self) -> list[str]:
        changed = []
        for relpath, baseline in self._baseline.items():
            candidate = self.path / relpath
            if not candidate.is_file() or candidate.read_bytes() != baseline:
                changed.append(relpath)
        return changed

    def unexpected_changes(self) -> list[str]:
        unstaged = set(self._git_paths("diff", "--name-only"))
        cached = set(self._git_paths("diff", "--cached", "--name-only"))
        untracked = set(self._git_paths("ls-files", "--others", "--exclude-standard"))
        newly_cached = cached - self._baseline_cached
        allowed = set(self.allowed)
        return sorted((unstaged | newly_cached | untracked) - allowed)

    def patched_text(self, relpath: str) -> str:
        return (self.path / relpath).read_text(encoding="utf-8", errors="replace")

    def apply_unified_diff(self, diff: str) -> None:
        """Apply Codex's diff; framework, not Codex, mutates this worktree."""
        text = str(diff or "")
        if not text.strip():
            raise WorkspaceError("codex_diff_empty")
        for check in (True, False):
            command = ["git", "-C", str(self.path), "apply", "--whitespace=nowarn"]
            if check:
                command.append("--check")
            completed = subprocess.run(
                [*command, "-"],
                input=text,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip().splitlines()
                raise WorkspaceError(
                    "codex_diff_apply_failed: "
                    f"{detail[-1] if detail else 'unknown'}"
                )

    def function_diff(self, relpath: str) -> str:
        before = self._baseline[relpath].decode("utf-8", errors="replace").splitlines(
            keepends=True
        )
        after = (self.path / relpath).read_text(
            encoding="utf-8", errors="replace"
        ).splitlines(keepends=True)
        return "".join(
            difflib.unified_diff(
                before,
                after,
                fromfile=f"a/{relpath}",
                tofile=f"b/{relpath}",
            )
        )

    def baseline_sha256(self, relpath: str) -> str:
        return hashlib.sha256(self._baseline[relpath]).hexdigest()

    def _git_paths(self, *arguments: str) -> list[str]:
        completed = subprocess.run(
            ["git", "-C", str(self.path), *arguments, "-z"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode(errors="replace").strip()
            raise WorkspaceError(f"git {' '.join(arguments)} lỗi: {detail}")
        return [
            item.decode("utf-8", errors="replace")
            for item in completed.stdout.split(b"\0")
            if item
        ]

    @staticmethod
    def _run_git(cwd: Path, *arguments: str, timeout: int) -> None:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip().splitlines()
            raise WorkspaceError(
                f"git {' '.join(arguments[:3])} lỗi: "
                f"{detail[-1] if detail else 'unknown'}"
            )
