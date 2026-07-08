"""General-purpose helpers for awx-migration."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class MigrationError(RuntimeError):
    """Raised when a non-recoverable migration step fails."""


def timestamp() -> str:
    """Return the current UTC time as a sortable string.

    Returns:
        Timestamp in ``YYYYMMDD-HHMMSS`` format, e.g. ``"20240315-143022"``.
    """
    return datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")


def sha256(filename: str | Path) -> str:
    """Compute the SHA-256 digest of a file without loading it fully into RAM.

    Args:
        filename: Path to the file.

    Returns:
        Lowercase hexadecimal digest string.

    Raises:
        MigrationError: If the file cannot be read.
    """
    path = Path(filename)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
    except OSError as exc:
        raise MigrationError(f"Cannot read file for hashing '{path}': {exc}") from exc
    return digest.hexdigest()


def read_json(filename: str | Path) -> Any:
    """Read and parse a JSON file.

    Args:
        filename: Path to the JSON file.

    Returns:
        Parsed Python object.

    Raises:
        MigrationError: If the file cannot be read or is not valid JSON.
    """
    path = Path(filename)
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except OSError as exc:
        raise MigrationError(f"Cannot read JSON file '{path}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MigrationError(
            f"Invalid JSON in '{path}': {exc}"
        ) from exc


def write_json(filename: str | Path, data: Any) -> None:
    """Serialise *data* to a JSON file with readable indentation.

    Args:
        filename: Destination path. Parent directories are created if absent.
        data: JSON-serialisable Python object.

    Raises:
        MigrationError: If the file cannot be written.
    """
    path = Path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    except OSError as exc:
        raise MigrationError(
            f"Cannot write JSON file '{path}': {exc}"
        ) from exc


def mkdir(path: str | Path) -> Path:
    """Create a directory tree, including all missing parents.

    Succeeds silently when the directory already exists.

    Args:
        path: Directory path to create.

    Returns:
        Resolved :class:`~pathlib.Path` of the created directory.

    Raises:
        MigrationError: If the directory cannot be created.
    """
    target = Path(path)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MigrationError(
            f"Cannot create directory '{target}': {exc}"
        ) from exc
    return target


def human_size(num_bytes: int) -> str:
    """Format a byte count as a human-readable string.

    Args:
        num_bytes: Size in bytes.

    Returns:
        Human-readable string such as ``"1.2 MB"`` or ``"5.4 GB"``.
        Values below 1 KB are returned as ``"<n> B"``.
    """
    if num_bytes < 0:
        raise MigrationError(f"human_size: negative byte count: {num_bytes}")
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    value = float(num_bytes)
    for unit in units[:-1]:
        if value < 1024.0:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} {units[-1]}"


def which(program: str) -> str | None:
    """Return the absolute path of *program* if it exists in PATH.

    Args:
        program: Executable name to look up.

    Returns:
        Full path string, or ``None`` if the program is not found.
    """
    return shutil.which(program)


def check_program(program: str) -> str:
    """Assert that *program* is available in PATH.

    Args:
        program: Executable name to check.

    Returns:
        Full path string when the program is found.

    Raises:
        MigrationError: If the program is not found in PATH.
    """
    path = shutil.which(program)
    if path is None:
        raise MigrationError(
            f"Required program '{program}' not found in PATH"
        )
    return path
