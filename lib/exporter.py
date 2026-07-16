"""Export orchestration: canonical objects → stable per-type files.

The :class:`Exporter` drives an :class:`~lib.awx_client.AwxClient` and the
export format.  It works exclusively with
:class:`~lib.canonical.CanonicalObject` and
:class:`~lib.awx_objects.ObjectType`: it never sees AWX JSON, never invokes a
CLI, and does not consult the global registry — the object types it handles are
supplied to it.  It writes exactly one file per object type plus a
``manifest.json`` describing the bundle.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .awx_client import AwxClient
from .awx_objects import ObjectType
from .canonical import CanonicalObject
from .export_format import ExportFormatError, write_manifest, write_type_file

_MANIFEST_FILENAME: str = "manifest.json"


class ExportError(RuntimeError):
    """Raised when an export bundle cannot be written."""


@dataclass
class ExportSummary:
    """Result of an export operation.

    Attributes:
        directory: Directory the bundle was written to.
        counts: Mapping of object-type key to the number of objects written.
    """

    directory: Path
    counts: dict[str, int] = field(default_factory=dict)


class Exporter:
    """Exports canonical objects to a stable per-type file bundle."""

    def __init__(
        self,
        client: AwxClient,
        object_types: Sequence[ObjectType],
        *,
        tool_version: str,
        awx_version: str,
        exported_at: str | None = None,
    ) -> None:
        """Initialise the exporter.

        Args:
            client: The AWX client used to fetch canonical objects.
            object_types: Object types this exporter handles (used by
                :meth:`export_all`).
            tool_version: awx-migration tool version recorded in the files.
            awx_version: Source AWX version recorded in the files.
            exported_at: ISO-8601 timestamp for the export.  Defaults to the
                current UTC time.
        """
        self._client = client
        self._object_types = list(object_types)
        self._tool_version = tool_version
        self._awx_version = awx_version
        self._exported_at = (
            exported_at or datetime.now(tz=timezone.utc).isoformat()
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def export_all(
        self, output_dir: str | Path, *, organization: str | None = None
    ) -> ExportSummary:
        """Export every configured object type into *output_dir*.

        Args:
            output_dir: Destination bundle directory.
            organization: Restrict organization-scoped types to this
                organization, or ``None`` for all.

        Returns:
            An :class:`ExportSummary`.
        """
        entries = [
            (obj_type, self._client.export(obj_type.key, organization=organization))
            for obj_type in self._object_types
        ]
        return self._write_bundle(output_dir, entries, organization)

    def export_type(
        self,
        output_dir: str | Path,
        object_type: ObjectType,
        *,
        organization: str | None = None,
    ) -> ExportSummary:
        """Export all objects of a single *object_type* into *output_dir*.

        Args:
            output_dir: Destination bundle directory.
            object_type: The object type to export.
            organization: Organization filter, or ``None`` for all.

        Returns:
            An :class:`ExportSummary`.
        """
        objects = self._client.export(
            object_type.key, organization=organization
        )
        return self._write_bundle(output_dir, [(object_type, objects)], organization)

    def export_object(
        self,
        output_dir: str | Path,
        object_type: ObjectType,
        name: str,
        *,
        organization: str | None = None,
    ) -> ExportSummary:
        """Export the single object of *object_type* named *name*.

        The full type is fetched and then narrowed to objects whose ``name``
        field matches; combine with *organization* to disambiguate identical
        names across organizations.

        Args:
            output_dir: Destination bundle directory.
            object_type: The object type to export from.
            name: Name of the object to export.
            organization: Organization filter, or ``None`` for all.

        Returns:
            An :class:`ExportSummary` (count 0 when no object matched).
        """
        objects = self._client.export(
            object_type.key, organization=organization
        )
        matched = [obj for obj in objects if obj.fields.get("name") == name]
        return self._write_bundle(output_dir, [(object_type, matched)], organization)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _write_bundle(
        self,
        output_dir: str | Path,
        entries: Sequence[tuple[ObjectType, Sequence[CanonicalObject]]],
        organization: str | None,
    ) -> ExportSummary:
        """Write per-type files and the manifest for *entries*."""
        out = Path(output_dir)
        counts: dict[str, int] = {}
        manifest_types: dict[str, dict[str, object]] = {}
        try:
            for object_type, objects in entries:
                write_type_file(
                    out / object_type.filename,
                    object_type.key,
                    objects,
                    tool_version=self._tool_version,
                    awx_version=self._awx_version,
                    exported_at=self._exported_at,
                    organization=organization,
                )
                counts[object_type.key] = len(objects)
                manifest_types[object_type.key] = {
                    "count": len(objects),
                    "file": object_type.filename,
                }
            write_manifest(
                out / _MANIFEST_FILENAME,
                tool_version=self._tool_version,
                awx_version=self._awx_version,
                exported_at=self._exported_at,
                object_types=manifest_types,
                organization=organization,
            )
        except ExportFormatError as exc:
            raise ExportError(
                f"Failed to write export bundle to '{out}': {exc}"
            ) from exc
        return ExportSummary(directory=out, counts=counts)
