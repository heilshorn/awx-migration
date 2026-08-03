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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .awx_client import AwxClient
from .awx_objects import ObjectType
from .canonical import CanonicalObject
from .export_format import ExportFormatError, write_manifest, write_type_file

_MANIFEST_FILENAME: str = "manifest.json"

#: Registry key of the reference-only credentials type.
_CREDENTIALS_TYPE: str = "credentials"


class ExportError(RuntimeError):
    """Raised when an export bundle cannot be written."""


def _cred_label(ref: object) -> str:
    """Return a human-readable label for a credential reference."""
    if not isinstance(ref, Mapping):
        return repr(ref)
    name = ref.get("name")
    ct = ref.get("credential_type")
    if isinstance(ct, Mapping):
        ct = ct.get("name")
    org = ref.get("organization")
    if isinstance(org, Mapping):
        org = org.get("name")
    label = f"{name!r}"
    if ct:
        label += f" ({ct})"
    if org:
        label += f" in org {org!r}"
    return label


@dataclass
class ExportSummary:
    """Result of an export operation.

    Attributes:
        directory: Directory the bundle was written to.
        counts: Mapping of object-type key to the number of objects written.
        referenced_credentials: Labels of credentials referenced by exported
            objects — the credentials that must already exist in any target
            these objects are later imported into.
    """

    directory: Path
    counts: dict[str, int] = field(default_factory=dict)
    referenced_credentials: list[str] = field(default_factory=list)


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
        referenced_credentials: list[str] = []
        try:
            for object_type, objects in entries:
                self._collect_referenced_credentials(
                    object_type, objects, referenced_credentials
                )
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
        return ExportSummary(
            directory=out,
            counts=counts,
            referenced_credentials=referenced_credentials,
        )

    @staticmethod
    def _collect_referenced_credentials(
        object_type: ObjectType,
        objects: Sequence[CanonicalObject],
        into: list[str],
    ) -> None:
        """Append the unique credential-reference labels of *objects* to *into*.

        Credentials are surfaced as a dependency: they are never exported as
        objects, so they must already exist wherever these objects are
        imported.
        """
        cred_fields = [
            rref.canonical_field
            for rref in object_type.related_refs
            if rref.target_type == _CREDENTIALS_TYPE
        ]
        if not cred_fields:
            return
        for obj in objects:
            for cred_field in cred_fields:
                for ref in obj.fields.get(cred_field) or []:
                    label = _cred_label(ref)
                    if label not in into:
                        into.append(label)
