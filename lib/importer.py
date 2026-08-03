"""Import orchestration: a validated bundle → AwxClient import calls.

The :class:`Importer` drives the import of an export bundle.  It works
exclusively with :class:`~lib.awx_client.AwxClient`,
:class:`~lib.export_validator.ExportValidator`, the export format
(:mod:`lib.export_format`), :class:`~lib.awx_objects.ObjectType`, and
:class:`~lib.canonical.CanonicalObject`.  It never sees AWX dictionaries, never
invokes a CLI, and never talks to Kubernetes — all AWX translation happens
inside the client.

Note: ``ImportError`` here is this module's own domain exception and
intentionally shadows the built-in of the same name within this module; it is
unrelated to Python's import machinery.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .awx_client import AwxClient
from .awx_objects import OBJECT_TYPES, ObjectType, import_order
from .canonical import CanonicalObject
from .export_format import ExportFormatError, read_manifest, read_type_file
from .export_validator import ExportValidator

_MANIFEST_FILENAME: str = "manifest.json"
_DEFAULT_ON_CONFLICT: str = "update"

#: Registry key of the reference-only credentials type.  A ``RelatedRef`` whose
#: target is this type is subject to the pre-flight existence check.
_CREDENTIALS_TYPE: str = "credentials"


def _cred_identity(ref: object) -> tuple[object, object, object]:
    """Return ``(name, credential_type, organization)`` for a credential ref.

    Normalises the reduced dict form (``credential_type`` nested as
    ``{"name", "kind"}``) and a flattened form (plain name) alike, so the same
    identity is produced regardless of how a reference was stored.
    """
    if not isinstance(ref, Mapping):
        return (None, None, None)
    name = ref.get("name")
    ct = ref.get("credential_type")
    if isinstance(ct, Mapping):
        ct = ct.get("name")
    org = ref.get("organization")
    if isinstance(org, Mapping):
        org = org.get("name")
    return (name, ct, org)


def _cred_label(ref: object) -> str:
    """Return a human-readable label for a credential reference."""
    name, ct, org = _cred_identity(ref)
    label = f"{name!r}"
    if ct:
        label += f" ({ct})"
    if org:
        label += f" in org {org!r}"
    return label


class ImportError(RuntimeError):  # noqa: A001 - intentional domain name
    """Raised when an import cannot proceed (e.g. an invalid bundle)."""


@dataclass
class ImportSummary:
    """Aggregated outcome of importing a bundle.

    Attributes:
        created: Labels of objects created across all types.
        updated: Labels of objects updated.
        skipped: Labels of objects skipped.
        warnings: Non-fatal messages (validator warnings + per-type warnings).
        errors: Non-fatal per-object import errors collected from the client.
        object_count: Total number of objects handed to the client.
        imported_types: Object-type keys that were imported, in import order.
        referenced_credentials: Labels of credentials referenced by imported
            objects — the credentials that must already exist in the target.
        missing_credentials: Labels of referenced credentials that were absent
            from the target and therefore not re-attached.
    """

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    object_count: int = 0
    imported_types: list[str] = field(default_factory=list)
    referenced_credentials: list[str] = field(default_factory=list)
    missing_credentials: list[str] = field(default_factory=list)


def _select_objects(
    objects: Sequence[CanonicalObject], name: str | None
) -> list[CanonicalObject]:
    """Return *objects*, optionally narrowed to those named *name*."""
    if name is None:
        return list(objects)
    return [obj for obj in objects if obj.fields.get("name") == name]


class Importer:
    """Imports an export bundle by driving an :class:`AwxClient`."""

    def __init__(
        self,
        client: AwxClient,
        *,
        validator: ExportValidator | None = None,
        object_types: Mapping[str, ObjectType] | None = None,
    ) -> None:
        """Initialise the importer.

        Args:
            client: The AWX client that performs the actual import.
            validator: Bundle validator.  Defaults to an
                :class:`ExportValidator` bound to *object_types*.
            object_types: Object-type registry.  Defaults to
                :data:`~lib.awx_objects.OBJECT_TYPES`.
        """
        self._client = client
        self._object_types: Mapping[str, ObjectType] = (
            object_types if object_types is not None else OBJECT_TYPES
        )
        self._validator = (
            validator
            if validator is not None
            else ExportValidator(self._object_types)
        )

    def import_path(
        self,
        path: str | Path,
        *,
        types: Sequence[str] | None = None,
        name: str | None = None,
        on_conflict: str = _DEFAULT_ON_CONFLICT,
    ) -> ImportSummary:
        """Validate and import the export bundle at *path*.

        Args:
            path: Export bundle directory.
            types: Restrict the import to these object-type keys.  ``None``
                imports every type present in the bundle.
            name: Import only the object with this name (requires the selection
                to resolve to exactly one type).
            on_conflict: Conflict policy forwarded to the client
                (``"update"``, ``"skip"``, ``"fail"``).

        Returns:
            An :class:`ImportSummary` aggregating every client result.

        Raises:
            ImportError: If the bundle is invalid, unreadable, or ``name`` is
                combined with anything other than exactly one type.
        """
        bundle = Path(path)

        # 1. Validate first — nothing is imported from an invalid bundle.
        result = self._validator.validate(bundle)
        if not result.valid:
            raise ImportError(
                f"Export bundle at '{bundle}' is invalid: "
                + "; ".join(result.errors)
            )

        summary = ImportSummary()
        summary.warnings.extend(result.warnings)

        try:
            manifest = read_manifest(bundle / _MANIFEST_FILENAME)
        except ExportFormatError as exc:
            raise ImportError(
                f"Cannot read manifest in '{bundle}': {exc}"
            ) from exc

        declared = list((manifest.get("object_types") or {}).keys())
        selected_keys = self._select_keys(declared, types, summary)

        # 3. Order by dependencies.
        selected_types = [
            self._object_types[key]
            for key in selected_keys
            if key in self._object_types
        ]
        ordered = import_order(selected_types)

        if name is not None and len(ordered) != 1:
            raise ImportError(
                "--name requires the selection to resolve to exactly one type"
            )

        # 4-6. Read, filter, import each type in order.
        for obj_type in ordered:
            objects = self._read_objects(bundle, obj_type)
            selected = _select_objects(objects, name)
            if name is not None and not selected:
                summary.warnings.append(
                    f"no object named {name!r} in type {obj_type.key!r}"
                )
            selected = self._resolve_credential_refs(
                obj_type, selected, summary
            )
            outcome = self._client.import_objects(
                obj_type.key, selected, on_conflict=on_conflict
            )
            self._merge(summary, outcome, obj_type.key, len(selected))

        return summary

    def _resolve_credential_refs(
        self,
        obj_type: ObjectType,
        objects: Sequence[CanonicalObject],
        summary: ImportSummary,
    ) -> list[CanonicalObject]:
        """Record referenced credentials and drop any absent from the target.

        For every ``RelatedRef`` targeting credentials, the referenced
        credentials are recorded on the summary (they must pre-exist in the
        target).  A pre-flight query then determines which are missing; those
        references are stripped from the objects and reported as warnings, so
        the object still imports without a hard awxkit failure.
        """
        cred_fields = [
            rref.canonical_field
            for rref in obj_type.related_refs
            if rref.target_type == _CREDENTIALS_TYPE
        ]
        if not cred_fields:
            return list(objects)

        all_refs: list[Mapping[str, object]] = []
        seen: set[tuple[object, object, object]] = set()
        for obj in objects:
            for cred_field in cred_fields:
                for ref in obj.fields.get(cred_field) or []:
                    ident = _cred_identity(ref)
                    if ident not in seen:
                        seen.add(ident)
                        all_refs.append(ref)
                        label = _cred_label(ref)
                        if label not in summary.referenced_credentials:
                            summary.referenced_credentials.append(label)
        if not all_refs:
            return list(objects)

        missing_ids = {
            _cred_identity(ref)
            for ref in self._client.missing_credentials(all_refs)
        }
        if not missing_ids:
            return list(objects)

        result: list[CanonicalObject] = []
        for obj in objects:
            new_fields = dict(obj.fields)
            changed = False
            for cred_field in cred_fields:
                refs = new_fields.get(cred_field)
                if not isinstance(refs, list):
                    continue
                kept = []
                for ref in refs:
                    if _cred_identity(ref) in missing_ids:
                        changed = True
                        label = _cred_label(ref)
                        if label not in summary.missing_credentials:
                            summary.missing_credentials.append(label)
                        summary.warnings.append(
                            f"object {obj.fields.get('name')!r} references "
                            f"credential {label} which is not present in the "
                            "target; attach it manually or import the "
                            "credential first"
                        )
                    else:
                        kept.append(ref)
                if len(kept) != len(refs):
                    new_fields[cred_field] = kept
            result.append(
                CanonicalObject(
                    type=obj.type,
                    fields=new_fields,
                    natural_key=obj.natural_key,
                )
                if changed
                else obj
            )
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _select_keys(
        self,
        declared: list[str],
        types: Sequence[str] | None,
        summary: ImportSummary,
    ) -> list[str]:
        """Return the declared keys narrowed by *types*, warning on misses."""
        if types is None:
            return declared
        wanted = set(types)
        for requested in types:
            if requested not in declared:
                summary.warnings.append(
                    f"requested type {requested!r} is not present in the "
                    "bundle"
                )
        return [key for key in declared if key in wanted]

    def _read_objects(
        self, bundle: Path, obj_type: ObjectType
    ) -> list[CanonicalObject]:
        """Read the canonical objects of *obj_type* from the bundle."""
        try:
            type_file = read_type_file(bundle / obj_type.filename)
        except ExportFormatError as exc:
            raise ImportError(
                f"Cannot read '{obj_type.filename}': {exc}"
            ) from exc
        return type_file.objects

    @staticmethod
    def _merge(
        summary: ImportSummary,
        outcome,  # type: ignore[no-untyped-def]  # duck-typed ImportResult
        type_key: str,
        count: int,
    ) -> None:
        """Merge a single client result into the running *summary*."""
        summary.created.extend(outcome.created)
        summary.updated.extend(outcome.updated)
        summary.skipped.extend(outcome.skipped)
        summary.warnings.extend(outcome.warnings)
        summary.errors.extend(outcome.errors)
        summary.object_count += count
        if type_key not in summary.imported_types:
            summary.imported_types.append(type_key)
