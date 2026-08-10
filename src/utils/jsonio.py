from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
    )


def atomic_write_text(path: Path, value: str) -> None:
    """Atomically replace ``path`` with UTF-8 text on the same filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def atomic_write_bytes(path: Path, value: bytes) -> None:
    """Atomically replace ``path`` with bytes on the same filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def safe_name(value: object, limit: int = 100) -> str:
    text = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in str(value or "").strip()
    ).strip("._-")
    return (text or "unknown")[:limit]
