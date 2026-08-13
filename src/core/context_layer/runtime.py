from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path


FRAMEWORK_ROOT = Path(__file__).resolve().parents[3]
SOURCE_VENDOR_ROOT = FRAMEWORK_ROOT / "third_party" / "codegraph"
INSTALLED_VENDOR_ROOT = (
    Path(sys.prefix).resolve() / "share" / "debugging-framework" / "codegraph"
)
CODEGRAPH_VENDOR_ROOT = (
    SOURCE_VENDOR_ROOT
    if (SOURCE_VENDOR_ROOT / "manifest.json").is_file()
    else INSTALLED_VENDOR_ROOT
)
CODEGRAPH_MANIFEST = CODEGRAPH_VENDOR_ROOT / "manifest.json"
CODEGRAPH_RUNTIME_ROOT = CODEGRAPH_VENDOR_ROOT / "runtime"

_TARGETS = {
    ("darwin", "arm64"): "darwin-arm64",
    ("darwin", "aarch64"): "darwin-arm64",
    ("darwin", "x86_64"): "darwin-x64",
    ("linux", "x86_64"): "linux-x64",
    ("linux", "amd64"): "linux-x64",
    ("linux", "aarch64"): "linux-arm64",
    ("linux", "arm64"): "linux-arm64",
    ("windows", "amd64"): "win32-x64",
    ("windows", "x86_64"): "win32-x64",
    ("windows", "arm64"): "win32-arm64",
    ("windows", "aarch64"): "win32-arm64",
}


@dataclass(frozen=True)
class CodeGraphRuntime:
    executable: Path | None
    target: str
    expected_version: str = ""
    error: str = ""

    @property
    def available(self) -> bool:
        return self.executable is not None and not self.error


def platform_target(
    system: str | None = None,
    machine: str | None = None,
) -> str:
    key = (
        str(system or platform.system()).strip().lower(),
        str(machine or platform.machine()).strip().lower(),
    )
    return _TARGETS.get(key, "")


def resolve_codegraph_runtime(configured_executable: str = "") -> CodeGraphRuntime:
    target = platform_target()
    manifest, manifest_error = _read_manifest()
    expected_version = str(manifest.get("version") or "")

    configured = str(configured_executable or "").strip()
    if configured:
        executable = _resolve_configured_executable(configured)
        if executable is None:
            return CodeGraphRuntime(
                executable=None,
                target=target,
                expected_version=expected_version,
                error=f"configured_codegraph_unavailable:{configured}",
            )
        return CodeGraphRuntime(
            executable=executable,
            target=target,
            expected_version=expected_version,
        )

    if manifest_error:
        return CodeGraphRuntime(
            executable=None,
            target=target,
            error=manifest_error,
        )
    if not target:
        return CodeGraphRuntime(
            executable=None,
            target="",
            expected_version=expected_version,
            error=(
                "codegraph_platform_unsupported:"
                f"{platform.system()}-{platform.machine()}"
            ),
        )

    bundle = (manifest.get("bundles") or {}).get(target)
    if not isinstance(bundle, dict):
        return CodeGraphRuntime(
            executable=None,
            target=target,
            expected_version=expected_version,
            error=f"codegraph_bundle_not_packaged:{target}",
        )
    relative = str(bundle.get("executable") or "").strip()
    if not relative:
        return CodeGraphRuntime(
            executable=None,
            target=target,
            expected_version=expected_version,
            error=f"codegraph_manifest_missing_executable:{target}",
        )
    executable = (CODEGRAPH_VENDOR_ROOT / relative).resolve()
    try:
        executable.relative_to(CODEGRAPH_VENDOR_ROOT.resolve())
    except ValueError:
        return CodeGraphRuntime(
            executable=None,
            target=target,
            expected_version=expected_version,
            error=f"codegraph_manifest_unsafe_executable:{target}",
        )
    if not executable.is_file():
        materialize_error = _materialize_bundle(bundle, target)
        if materialize_error:
            return CodeGraphRuntime(
                executable=None,
                target=target,
                expected_version=expected_version,
                error=materialize_error,
            )
    error = _executable_error(executable)
    if error:
        return CodeGraphRuntime(
            executable=None,
            target=target,
            expected_version=expected_version,
            error=error,
        )
    expected_sha256 = str(bundle.get("executable_sha256") or "").strip().lower()
    if expected_sha256:
        actual_sha256 = _sha256(executable)
        if actual_sha256 != expected_sha256:
            return CodeGraphRuntime(
                executable=None,
                target=target,
                expected_version=expected_version,
                error=(
                    f"codegraph_executable_digest_mismatch:{target}:"
                    f"{actual_sha256}"
                ),
            )
    return CodeGraphRuntime(
        executable=executable,
        target=target,
        expected_version=expected_version,
    )


def _read_manifest() -> tuple[dict, str]:
    try:
        raw = json.loads(CODEGRAPH_MANIFEST.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, f"codegraph_manifest_missing:{CODEGRAPH_MANIFEST}"
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {}, f"codegraph_manifest_invalid:{type(exc).__name__}:{exc}"
    if not isinstance(raw, dict) or not str(raw.get("version") or "").strip():
        return {}, "codegraph_manifest_invalid:missing_version"
    if not isinstance(raw.get("bundles"), dict):
        return {}, "codegraph_manifest_invalid:missing_bundles"
    return raw, ""


def _resolve_configured_executable(value: str) -> Path | None:
    candidate = Path(value).expanduser()
    if candidate.is_absolute() or candidate.parent != Path("."):
        candidate = candidate.resolve()
        return candidate if not _executable_error(candidate) else None
    resolved = shutil.which(value)
    if not resolved:
        return None
    candidate = Path(resolved).resolve()
    return candidate if not _executable_error(candidate) else None


def _materialize_bundle(bundle: dict, target: str) -> str:
    """Extract a pinned bundled archive once, without network access."""
    archive_relative = str(bundle.get("archive") or "").strip()
    archive_sha256 = str(bundle.get("archive_sha256") or "").strip().lower()
    bundle_root = str(bundle.get("bundle_root") or "").strip()
    if not archive_relative or not archive_sha256 or not bundle_root:
        return f"codegraph_manifest_missing_archive_metadata:{target}"
    archive = (CODEGRAPH_VENDOR_ROOT / archive_relative).resolve()
    try:
        archive.relative_to(CODEGRAPH_VENDOR_ROOT.resolve())
    except ValueError:
        return f"codegraph_manifest_unsafe_archive:{target}"
    if not archive.is_file():
        return f"codegraph_archive_missing:{target}:{archive}"
    try:
        actual_sha256 = _sha256(archive)
    except OSError as exc:
        return f"codegraph_archive_unreadable:{target}:{type(exc).__name__}:{exc}"
    if actual_sha256 != archive_sha256:
        return (
            f"codegraph_archive_digest_mismatch:{target}:"
            f"expected={archive_sha256}:actual={actual_sha256}"
        )

    destination = (CODEGRAPH_RUNTIME_ROOT / bundle_root).resolve()
    try:
        destination.relative_to(CODEGRAPH_RUNTIME_ROOT.resolve())
    except ValueError:
        return f"codegraph_manifest_unsafe_bundle_root:{target}"
    if destination.is_dir():
        return ""
    CODEGRAPH_RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(
        prefix=f".{bundle_root}-",
        dir=CODEGRAPH_RUNTIME_ROOT,
    )).resolve()
    try:
        with tarfile.open(archive, mode="r:gz") as handle:
            members = handle.getmembers()
            for member in members:
                member_path = Path(member.name)
                if (
                    member_path.is_absolute()
                    or ".." in member_path.parts
                    or member.issym()
                    or member.islnk()
                    or member.isdev()
                ):
                    return f"codegraph_archive_unsafe_member:{target}:{member.name}"
                resolved_member = (temporary / member.name).resolve()
                try:
                    resolved_member.relative_to(temporary)
                except ValueError:
                    return f"codegraph_archive_unsafe_member:{target}:{member.name}"
            if sys.version_info >= (3, 12):
                handle.extractall(temporary, members=members, filter="data")
            else:
                # Extraction filters were added after Python 3.10. Every member
                # has already passed the equivalent path/type checks above.
                handle.extractall(temporary, members=members)
        extracted = temporary / bundle_root
        if not extracted.is_dir():
            return f"codegraph_archive_root_missing:{target}:{bundle_root}"
        if destination.exists():
            return ""
        os.replace(extracted, destination)
    except (OSError, tarfile.TarError) as exc:
        if destination.is_dir():
            return ""
        return f"codegraph_archive_extract_failed:{target}:{type(exc).__name__}:{exc}"
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return ""


def _executable_error(path: Path) -> str:
    if not path.is_file():
        return f"codegraph_executable_missing:{path}"
    if os.name != "nt" and not os.access(path, os.X_OK):
        return f"codegraph_executable_not_executable:{path}"
    return ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
