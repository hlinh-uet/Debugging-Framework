"""Public package facade for Debugging-Framework.

The implementation currently lives in the historical ``src`` package.  This
facade gives installed users a stable, non-generic import path while the
internals are migrated incrementally.
"""

from src.core.pipeline import DebuggingPipeline, PipelineOptions
from src.loaders.project import Project, ProjectLoader

__all__ = [
    "DebuggingPipeline",
    "PipelineOptions",
    "Project",
    "ProjectLoader",
]

