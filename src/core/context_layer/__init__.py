"""Optional repository-navigation backends for the Codex repair worker.

The context layer is deliberately outside the fault-localization and repair
contract.  It may make read-only repository navigation cheaper, but it never
selects repair locations, changes Codex's response schema, or validates a patch.
"""

from src.core.context_layer.codegraph import CodeGraphBackend, CodeGraphError
from src.core.context_layer.models import CodeGraphPreparation

__all__ = ["CodeGraphBackend", "CodeGraphError", "CodeGraphPreparation"]
