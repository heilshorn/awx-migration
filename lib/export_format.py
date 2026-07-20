"""Stable, versioned on-disk format for AWX object exports.

This module owns the export **file format** and nothing else.  It serialises and
deserialises :class:`~lib.canonical.CanonicalObject` instances to and from
disk, wrapped in a small versioned envelope, and delegates version upgrades to
:mod:`lib.migrations`.  It contains **no AWX logic**: it never talks to AWX, a
cluster, or a subprocess, and it does not know the object-type registry.

Two independent version numbers are tracked:

``FORMAT_VERSION``
    Version of the file envelope (which top-level keys exist, how objects are
    laid out).  Governs the migration chain in :mod:`lib.migrations`.

``SCHEMA_VERSION``
    Version of the canonical object schema (which whitelisted fields a type
    carries).  May evolve independently of the envelope.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .canonical import CanonicalObject
from .migrations import MigrationError, migrate_document

#: Version of the file envelope.  See module docstring.
FORMAT_VERSION: int = 1

#: Version of the canonical object schema.  See module docstring.
SCHEMA_VERSION: int = 1

_KIND_TYPE_FILE: str = "type_file"
_KIND_MANIFEST: str = "manifest"


class ExportFormatError(RuntimeError):
    """Raised on any export-format read/write or validation failure."""


@dataclass(frozen=True)
class TypeFile:
    """In-memory view of a parsed per-type export file.

    Attributes:
        object_type: Registry key of the contained objects.
        objects: The parsed canonical objects.
        format_version: Envelope version the file was read at (post-migration).
        schema_version: Canonical schema version recorded in the file.
        metadata: Remaining envelope fields (tool_version, awx_version,
            organization, exported_at, count).
    """

    object_type: str
    objects: list[CanonicalObject]
    format_version: int
    schema_version: int
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Low-level JSON I/O (kept private so error handling is uniform)
# ---------------------------------------------------------------------------


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Write *data* to *path* as sorted, indented UTF-8 JSON.

    Sorting keys keeps exports stable and diff-friendly across runs.

    Raises:
        ExportFormatError: If the file cannot be written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True, ensure_ascii=False)
            fh.write("\n")
    except OSError as exc:
        raise ExportFormatError(f"Cannot write '{path}': {exc}") from exc


def _read_json(path: Path) -> dict[str, Any]:
    """Read and parse a JSON object from *path*.

    Raises:
        ExportFormatError: If the file cannot be read, is not valid JSON, or
            does not contain a JSON object.
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            parsed = json.load(fh)
    except OSError as exc:
        raise ExportFormatError(f"Cannot read '{path}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ExportFormatError(f"Invalid JSON in '{path}': {exc}") from exc
    if not isinstance(parsed, dict):
        raise ExportFormatError(
            f"Export file '{path}' must contain a JSON object, got "
            f"{type(parsed).__name__}"
        )
    return parsed


# ---------------------------------------------------------------------------
# Per-type files
# ---------------------------------------------------------------------------


def write_type_file(
    path: str | Path,
    object_type: str,
    objects: Sequence[CanonicalObject],
    *,
    tool_version: str,
    awx_version: str,
    exported_at: str,
    organization: str | None = None,
) -> None:
    """Write *objects* of a single type to *path* in the stable format.

    Only the objects' :attr:`~lib.canonical.CanonicalObject.fields` are stored;
    the type is recorded once at the envelope level and reattached on read.

    Args:
        path: Destination file path.  Parent directories are created.
        object_type: Registry key all *objects* must share.
        objects: Canonical objects to serialise.
        tool_version: awx-migration tool version string.
        awx_version: Source AWX version string.
        exported_at: ISO-8601 timestamp of the export.
        organization: Organisation filter in effect, or ``None`` for all.

    Raises:
        ExportFormatError: If any object's type differs from *object_type*, or
            the file cannot be written.
    """
    payload: list[dict[str, Any]] = []
    natural_keys: list[dict[str, Any] | None] = []
    for obj in objects:
        if obj.type != object_type:
            raise ExportFormatError(
                f"Object of type {obj.type!r} does not belong in a "
                f"{object_type!r} type file"
            )
        payload.append(obj.fields)
        natural_keys.append(obj.natural_key)

    document: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "kind": _KIND_TYPE_FILE,
        "object_type": object_type,
        "tool_version": tool_version,
        "awx_version": awx_version,
        "organization": organization,
        "exported_at": exported_at,
        "count": len(payload),
        "objects": payload,
    }
    # Identity metadata is kept in a parallel array, separate from the business
    # fields, and only when at least one object carries it.
    if any(nk is not None for nk in natural_keys):
        document["natural_keys"] = natural_keys
    _write_json(Path(path), document)


def read_type_file(path: str | Path) -> TypeFile:
    """Read a per-type export file, migrating it to the current format.

    Args:
        path: Path to the type file.

    Returns:
        A :class:`TypeFile` with reconstructed canonical objects.

    Raises:
        ExportFormatError: If the file is unreadable, malformed, of an
            unsupported version, or not a type file.
    """
    raw = _read_json(Path(path))
    try:
        doc = migrate_document(raw, _KIND_TYPE_FILE, target=FORMAT_VERSION)
    except MigrationError as exc:
        raise ExportFormatError(f"Cannot read '{path}': {exc}") from exc

    kind = doc.get("kind")
    if kind != _KIND_TYPE_FILE:
        raise ExportFormatError(
            f"Expected a {_KIND_TYPE_FILE!r} document in '{path}', got "
            f"{kind!r}"
        )

    object_type = doc.get("object_type")
    if not isinstance(object_type, str) or not object_type:
        raise ExportFormatError(
            f"Type file '{path}' is missing a valid 'object_type'"
        )

    raw_objects = doc.get("objects")
    if not isinstance(raw_objects, list):
        raise ExportFormatError(
            f"Type file '{path}' is missing an 'objects' list"
        )

    raw_natural_keys = doc.get("natural_keys")
    natural_keys = (
        raw_natural_keys if isinstance(raw_natural_keys, list) else []
    )

    objects: list[CanonicalObject] = []
    for index, entry in enumerate(raw_objects):
        if not isinstance(entry, dict):
            raise ExportFormatError(
                f"Type file '{path}' contains a non-object entry: "
                f"{type(entry).__name__}"
            )
        natural_key = (
            natural_keys[index]
            if index < len(natural_keys)
            and isinstance(natural_keys[index], dict)
            else None
        )
        objects.append(
            CanonicalObject(
                type=object_type, fields=entry, natural_key=natural_key
            )
        )

    metadata = {
        k: v
        for k, v in doc.items()
        if k
        not in {
            "format_version",
            "schema_version",
            "kind",
            "object_type",
            "objects",
            "natural_keys",
        }
    }
    return TypeFile(
        object_type=object_type,
        objects=objects,
        format_version=int(doc.get("format_version", FORMAT_VERSION)),
        schema_version=int(doc.get("schema_version", SCHEMA_VERSION)),
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def write_manifest(
    path: str | Path,
    *,
    tool_version: str,
    awx_version: str,
    exported_at: str,
    object_types: Mapping[str, Mapping[str, Any]],
    organization: str | None = None,
    contains_secrets: bool = False,
) -> None:
    """Write the export bundle manifest to *path*.

    Args:
        path: Destination file path.  Parent directories are created.
        tool_version: awx-migration tool version string.
        awx_version: Source AWX version string.
        exported_at: ISO-8601 timestamp of the export.
        object_types: Per-type summary, e.g.
            ``{"job_templates": {"count": 18, "file": "job_templates.json"}}``.
        organization: Organisation filter in effect, or ``None`` for all.
        contains_secrets: Always ``False`` for exports; present so the mode is
            machine-detectable.

    Raises:
        ExportFormatError: If the file cannot be written.
    """
    document: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "kind": _KIND_MANIFEST,
        "tool_version": tool_version,
        "awx_version": awx_version,
        "organization": organization,
        "exported_at": exported_at,
        "contains_secrets": contains_secrets,
        "object_types": {k: dict(v) for k, v in object_types.items()},
    }
    _write_json(Path(path), document)


def read_manifest(path: str | Path) -> dict[str, Any]:
    """Read an export bundle manifest, migrating it to the current format.

    Args:
        path: Path to the manifest file.

    Returns:
        The parsed manifest document.

    Raises:
        ExportFormatError: If the file is unreadable, malformed, of an
            unsupported version, or not a manifest.
    """
    raw = _read_json(Path(path))
    try:
        doc = migrate_document(raw, _KIND_MANIFEST, target=FORMAT_VERSION)
    except MigrationError as exc:
        raise ExportFormatError(f"Cannot read '{path}': {exc}") from exc

    kind = doc.get("kind")
    if kind != _KIND_MANIFEST:
        raise ExportFormatError(
            f"Expected a {_KIND_MANIFEST!r} document in '{path}', got "
            f"{kind!r}"
        )
    return doc
