from __future__ import annotations

"""Defects4C dataset adapter.

The normal framework API remains project-path based.  This adapter adds the
dataset-oriented convenience used by Unified-Debugging: ``libyang``/``fmt``
resolve to a materialized buggy checkout, its Defects4C container and (when
metadata is available) the tests that failed for that version.
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from src.loaders.project import Project
from src.utils.jsonio import safe_name


@dataclass(frozen=True)
class Defects4CRecipe:
    alias: str
    project_name: str
    recipe_dir: Path
    container: str


@dataclass(frozen=True)
class Defects4CSelection:
    recipe: Defects4CRecipe
    project: Project
    bug_id: str = ""
    commit_after: str = ""
    failing_tests: tuple[str, ...] = ()
    metadata_file: Path | None = None


class Defects4CProjectResolver:
    """Resolve dataset aliases to the existing Defects4C materialization."""

    def __init__(self, root: Path | None = None):
        configured = str(os.environ.get("DEBUGGING_DEFECTS4C_ROOT") or "").strip()
        if root is None and configured:
            root = Path(configured)
        if root is None:
            # Installed/source checkout layout: Unified_Debugging/{framework,defects4c}.
            root = Path(__file__).resolve().parents[3] / "defects4c"
        self.root = root.expanduser().resolve()

    def resolve(
        self,
        alias: str,
        *,
        project_path: Path | None = None,
        bug_id: str = "",
    ) -> Defects4CSelection:
        recipe = self.recipe(alias)
        record: dict = {}
        if project_path is not None:
            path = project_path.expanduser().resolve()
            if not path.is_dir():
                raise FileNotFoundError(f"Defects4C project không tồn tại: {path}")
            record = self._record_for_path(recipe, path, bug_id)
        else:
            record = self._select_record(recipe, bug_id)
            path = Path(str(record.get("project_path") or "")).expanduser().resolve()
            if not path.is_dir():
                raise FileNotFoundError(
                    f"Materialized project không tồn tại: {path}. "
                    f"Chạy prepare_project.py cho recipe {recipe.project_name}."
                )
        commit_after = str(record.get("commit_after") or "").strip()
        selected_bug = str(record.get("bug_id") or bug_id or "").strip()
        metadata_file, failing_tests = self._metadata_tests(
            recipe, selected_bug, commit_after, path
        )
        return Defects4CSelection(
            recipe=recipe,
            project=Project(path=path, project_id=path.name),
            bug_id=selected_bug,
            commit_after=commit_after,
            failing_tests=failing_tests,
            metadata_file=metadata_file,
        )

    def resolve_all(
        self,
        alias: str,
        *,
        bug_id: str = "",
    ) -> list[Defects4CSelection]:
        """Resolve every materialized version for a recipe alias."""
        recipe = self.recipe(alias)
        records = self._materialized_records(recipe)
        if not records:
            records = self._glob_records(recipe, bug_id)
        selector = str(bug_id or "").strip()
        selections: list[Defects4CSelection] = []
        for record in records:
            record_bug = str(record.get("bug_id") or "").strip()
            commit = str(record.get("commit_after") or "").strip()
            if selector and not (
                record_bug == selector
                or commit == selector
                or (len(selector) >= 7 and commit.startswith(selector))
            ):
                continue
            path = self._record_project_path(record)
            if path is None:
                continue
            metadata_file, failing_tests = self._metadata_tests(
                recipe, record_bug or selector, commit, path
            )
            selections.append(
                Defects4CSelection(
                    recipe=recipe,
                    project=Project(path=path, project_id=path.name),
                    bug_id=record_bug or selector,
                    commit_after=commit,
                    failing_tests=failing_tests,
                    metadata_file=metadata_file,
                )
            )
        if not selections:
            raise ValueError(
                f"Không tìm thấy materialized bug nào cho Defects4C {recipe.project_name}"
                + (f" với selector {selector!r}" if selector else "")
            )
        return selections

    def recipe(self, alias: str) -> Defects4CRecipe:
        requested = self._normalize_alias(alias)
        if not requested:
            raise ValueError("Defects4C alias không được để trống")
        candidates: list[Defects4CRecipe] = []
        for group in ("projects_v1", "projects"):
            recipes_root = self.root / "defectsc_tpl" / group
            if not recipes_root.is_dir():
                continue
            for recipe_dir in sorted(recipes_root.iterdir()):
                project_json = recipe_dir / "project.json"
                if not project_json.is_file():
                    continue
                try:
                    raw = json.loads(project_json.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
                project_name = str(raw.get("repo_name") or recipe_dir.name).strip()
                aliases = {
                    self._normalize_alias(recipe_dir.name),
                    self._normalize_alias(project_name),
                    self._normalize_alias(project_name.rsplit("___", 1)[-1]),
                }
                if requested in aliases:
                    candidates.append(
                        Defects4CRecipe(
                            alias=requested,
                            project_name=project_name,
                            recipe_dir=recipe_dir.resolve(),
                            container=self._container_name(project_name, recipe_dir),
                        )
                    )
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            known = ", ".join(self.available_aliases())
            raise ValueError(
                f"Không tìm thấy Defects4C recipe {alias!r} dưới {self.root}. "
                f"Alias có sẵn: {known or 'không có'}"
            )
        raise ValueError(f"Defects4C alias không duy nhất: {alias}")

    def available_aliases(self) -> list[str]:
        values: set[str] = set()
        for group in ("projects_v1", "projects"):
            recipes_root = self.root / "defectsc_tpl" / group
            if not recipes_root.is_dir():
                continue
            for recipe_dir in recipes_root.iterdir():
                project_json = recipe_dir / "project.json"
                if not project_json.is_file():
                    continue
                try:
                    raw = json.loads(project_json.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
                name = str(raw.get("repo_name") or recipe_dir.name)
                values.add(self._normalize_alias(name.rsplit("___", 1)[-1]))
        return sorted(value for value in values if value)

    def _select_record(self, recipe: Defects4CRecipe, bug_id: str) -> dict:
        records = self._materialized_records(recipe)
        if not records:
            records = self._glob_records(recipe, bug_id)
        if not records:
            raise FileNotFoundError(
                f"Chưa có materialized project cho {recipe.project_name}. "
                f"Chạy: python prepare_project.py {recipe.project_name} --bug <id>"
            )
        selector = str(bug_id or "").strip()
        if selector:
            matches = [
                item for item in records
                if str(item.get("bug_id") or "").strip() == selector
                or str(item.get("commit_after") or "").strip() == selector
                or str(item.get("commit_after") or "").strip().startswith(selector)
                and len(selector) >= 7
            ]
        else:
            matches = records
        normalized_matches = []
        for item in matches:
            path = self._record_project_path(item)
            if path is not None:
                normalized_matches.append({**item, "project_path": str(path)})
        matches = normalized_matches
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(
                f"Không tìm thấy bug {selector!r} cho Defects4C {recipe.project_name}."
            )
        options = ", ".join(
            f"{item.get('bug_id') or '?'}:{Path(str(item.get('project_path'))).name}"
            for item in matches
        )
        raise ValueError(
            f"Bug selector {selector!r} không duy nhất cho {recipe.project_name}; "
            f"chọn bug id/commit cụ thể trong: {options}"
        )

    def _record_project_path(self, record: dict) -> Path | None:
        raw = str(record.get("project_path") or "").strip()
        if not raw:
            return None
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.root / path
        if path.is_dir():
            return path.resolve()
        # prepare_project writes absolute paths.  If the Defects4C checkout was
        # moved, recover using the materialized directory name under this root.
        fallback = self.root / "data" / path.name
        return fallback.resolve() if fallback.is_dir() else None

    def _materialized_records(self, recipe: Defects4CRecipe) -> list[dict]:
        data_root = self.root / "data"
        candidates = [data_root / f"{safe_name(recipe.project_name)}__materialized.json"]
        if data_root.is_dir():
            candidates.extend(sorted(data_root.glob("*__materialized.json")))
        for manifest in candidates:
            if not manifest.is_file():
                continue
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if str(payload.get("project_name") or "").strip() != recipe.project_name:
                continue
            records = payload.get("projects")
            if isinstance(records, list):
                return [item for item in records if isinstance(item, dict)]
        return []

    def _glob_records(self, recipe: Defects4CRecipe, bug_id: str) -> list[dict]:
        data_root = self.root / "data"
        if not data_root.is_dir():
            return []
        pattern = f"{safe_name(recipe.project_name)}__*"
        records = []
        for path in sorted(data_root.glob(pattern)):
            if not path.is_dir():
                continue
            records.append({"bug_id": bug_id, "project_path": str(path)})
        return records

    @staticmethod
    def _record_for_path(recipe: Defects4CRecipe, path: Path, bug_id: str) -> dict:
        for item in Defects4CProjectResolver._materialized_records_static(recipe):
            if Path(str(item.get("project_path") or "")).expanduser().resolve() == path:
                return item
        return {"bug_id": bug_id, "project_path": str(path)}

    @staticmethod
    def _materialized_records_static(recipe: Defects4CRecipe) -> list[dict]:
        # Kept separate so resolving an explicit path does not require a resolver
        # to rescan recipes; the caller has already validated that path.
        manifest = recipe.recipe_dir.parent.parent.parent / "data" / (
            f"{safe_name(recipe.project_name)}__materialized.json"
        )
        if not manifest.is_file():
            return []
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return []
        return [item for item in payload.get("projects", []) if isinstance(item, dict)]

    def _metadata_tests(
        self,
        recipe: Defects4CRecipe,
        bug_id: str,
        commit_after: str,
        project_path: Path,
    ) -> tuple[Path | None, tuple[str, ...]]:
        tail = self._normalize_alias(recipe.project_name.rsplit("___", 1)[-1])
        metadata_root = self.root / "out_tmp_dirs" / "unified_debugging" / tail / "metadata"
        if not metadata_root.is_dir():
            return None, ()
        candidates: list[tuple[Path, dict]] = []
        for metadata_file in sorted(metadata_root.glob("*_meta.json")):
            try:
                payload = json.loads(metadata_file.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if str(payload.get("project") or "").strip() != recipe.project_name:
                continue
            if commit_after and str(payload.get("commit_after") or "").startswith(commit_after[:12]):
                candidates.insert(0, (metadata_file, payload))
            elif bug_id and str(payload.get("bug_id") or "").strip() == bug_id:
                candidates.append((metadata_file, payload))
        if not candidates:
            return None, ()
        metadata_file, payload = candidates[0]
        values: list[str] = []
        for item in payload.get("tests", []) or []:
            if not isinstance(item, dict) or not self._is_failure(item.get("outcome")):
                continue
            fixed = item.get("outcome_fixed")
            if fixed is not None and self._is_failure(fixed):
                continue
            test_id = str(item.get("test_id") or "").strip()
            if not test_id:
                continue
            # Rendered Defects4C recipes select a CTest binary.  Metadata may
            # include a case suffix; the binary is the portable selector.
            selector = test_id.split("::", 1)[0]
            if selector not in values:
                values.append(selector)
        recipe_filter = self._rendered_test_filter(project_path)
        if recipe_filter:
            try:
                filtered = [value for value in values if re.search(recipe_filter, value)]
            except re.error:
                filtered = []
            if filtered:
                values = filtered
        return metadata_file, tuple(values)

    @staticmethod
    def _is_failure(value: object) -> bool:
        return str(value or "").strip().upper() in {
            "FAIL", "FAILED", "ERROR", "CRASH", "TIMEOUT",
        }

    @staticmethod
    def _rendered_test_filter(project_path: Path) -> str:
        script = project_path / ".debugging-framework" / "recipe_test_impl.sh"
        if not script.is_file():
            return ""
        try:
            text = script.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        match = re.search(r"(?m)^\s*filter_item\s*=\s*(['\"])(.*?)\1", text)
        return str(match.group(2) or "").strip() if match else ""

    @staticmethod
    def _normalize_alias(value: str) -> str:
        text = str(value or "").strip().lower()
        if text.startswith("defects4c-"):
            text = text[len("defects4c-") :]
        return text

    @staticmethod
    def _container_name(project_name: str, recipe_dir: Path) -> str:
        candidates = [
            recipe_dir.parents[2] / f"Dockerfile.{recipe_dir.name.split('___', 1)[-1]}",
            recipe_dir.parents[2] / f"Dockerfile.{recipe_dir.name}",
        ]
        pattern = re.compile(r"--name\s+([A-Za-z0-9_.-]+)")
        for dockerfile in candidates:
            if not dockerfile.is_file():
                continue
            try:
                match = pattern.search(dockerfile.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if match:
                return match.group(1)
        tail = project_name.rsplit("___", 1)[-1].lower()
        return "my_defects4c_" + safe_name(tail, 80).strip("._-")
