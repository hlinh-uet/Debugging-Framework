from __future__ import annotations

from typing import Iterable, Optional

from src.utils.unified_runtime import UnifiedRuntime


class Defects4CLoader:
    """Read normalized bug records through Unified-Debugging's loader factory."""

    def __init__(self, runtime: UnifiedRuntime):
        self.runtime = runtime

    def load_bugs(self, dataset: str, bug_ids: Optional[Iterable[str]] = None):
        self.runtime.ensure_imports()
        loader = self.runtime.get_loader(dataset)
        requested = [
            str(item).strip() for item in (bug_ids or []) if str(item).strip()
        ]
        if not requested:
            return loader.load_all()

        records = []
        missing = []
        for bug_id in requested:
            record = loader.load_one(bug_id)
            if record is None:
                missing.append(bug_id)
            else:
                records.append(record)
        if missing:
            raise ValueError(
                f"Không tìm thấy bug trong dataset '{dataset}': {', '.join(missing)}"
            )
        return records

