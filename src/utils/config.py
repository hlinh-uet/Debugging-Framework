from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    """Resolved paths shared by all commands."""

    unified_root: Path = PROJECT_ROOT.parent / "Unified-Debugging"
    results_dir: Path = PROJECT_ROOT / "experiments"
    codex_executable: str = "codex"

    @property
    def output_schema(self) -> Path:
        return Path(__file__).resolve().parents[1] / "schemas" / "codex_result.schema.json"

    def validated(self) -> "Settings":
        root = self.unified_root.expanduser().resolve()
        required = (
            root / "data_loaders" / "defects4c_loader.py",
            root / "data_loaders" / "sandbox_adapter.py",
            root / "evaluation" / "eval_fl.py",
            root / "evaluation" / "eval_apr.py",
        )
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise FileNotFoundError(
                "Unified-Debugging không đầy đủ; thiếu: " + ", ".join(missing)
            )
        schema = self.output_schema
        if not schema.is_file():
            raise FileNotFoundError(f"Thiếu Codex output schema: {schema}")
        return Settings(
            unified_root=root,
            results_dir=self.results_dir.expanduser().resolve(),
            codex_executable=self.codex_executable,
        )
