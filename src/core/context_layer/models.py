from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class CodeGraphPreparation:
    """Result of preparing optional CodeGraph navigation for one workspace."""

    mode: str
    status: str
    ready: bool = False
    target: str = ""
    version: str = ""
    executable: Path | None = None
    elapsed_seconds: float = 0.0
    error: str = ""
    init_log_artifact: str = ""
    status_log_artifact: str = ""

    @property
    def tool_directories(self) -> tuple[Path, ...]:
        if not self.ready or self.executable is None:
            return ()
        return (self.executable.parent,)

    def prompt_context(self) -> str:
        if not self.ready:
            return ""
        return (
            "A read-only CodeGraph navigation index has been prepared for this "
            "recoverable project baseline. Start structural repository investigation with "
            "`codegraph explore \"<question about the failure, symbols, or call flow>\"`; "
            "use `codegraph node`, `codegraph callers`, or `codegraph callees` for "
            "narrow follow-up queries. CodeGraph is navigational evidence, not fault "
            "ground truth: production source and the supplied failure output remain "
            "authoritative, and you must fall back to normal search/read tools when "
            "the graph is incomplete. If you query after editing source, first run "
            "`codegraph sync .`. Do not initialize, install, upgrade, or reconfigure "
            "CodeGraph."
        )

    def to_dict(self) -> dict:
        value = asdict(self)
        value["executable"] = str(self.executable) if self.executable else ""
        return value
