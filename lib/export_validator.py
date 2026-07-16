"""Structural validation of an export bundle on disk.

Checks that an export directory (a ``manifest.json`` plus one file per object
type) is internally consistent and readable, *without* contacting AWX, the
cluster, or the CLI.  It depends only on :mod:`lib.export_format` (supported
version constants), :mod:`lib.awx_objects` (the registry), and
:class:`~lib.canonical.CanonicalObject`.

Design principles:

* **Field validation problems are collected, never raised.**  A single call to
  :meth:`ExportValidator.validate` gathers as many errors and warnings as it
  can find; it does not stop at the first problem.
* **Exceptions are reserved for genuine I/O failures** (permission denied,
  other OS errors) and unexpected programming errors — never for normal
  validation findings.  A missing file is a validation *error*, not an
  exception.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .awx_objects import OBJECT_TYPES, ObjectType
from .canonical import CanonicalObject
from .export_format import FORMAT_VERSION, SCHEMA_VERSION

_MANIFEST_FILENAME: str = "manifest.json"
_KIND_MANIFEST: str = "manifest"
_KIND_TYPE_FILE: str = "type_file"


class ExportValidationError(RuntimeError):
    """Raised only for I/O failures or unexpected errors during validation.

    Normal validation findings are reported via :class:`ValidationResult`, not
    by raising this exception.
    """


@dataclass
class ValidationResult:
    """Outcome of validating an export bundle.

    Attributes:
        valid: ``True`` when no errors were found.
        warnings: Non-fatal findings.
        errors: Fatal findings that make the bundle invalid.
        object_count: Total number of objects across all valid type files.
        object_types: Object-type keys found in the bundle.
        schema_version: Schema version read from the manifest (0 if unknown).
        format_version: Format version read from the manifest (0 if unknown).
    """

    valid: bool = True
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    object_count: int = 0
    object_types: list[str] = field(default_factory=list)
    schema_version: int = 0
    format_version: int = 0


class ExportValidator:
    """Validates the structure of an export bundle directory."""

    def __init__(
        self, object_types: Mapping[str, ObjectType] | None = None
    ) -> None:
        """Initialise the validator.

        Args:
            object_types: Object-type registry to validate against.  Defaults
                to :data:`~lib.awx_objects.OBJECT_TYPES`.
        """
        self._object_types: Mapping[str, ObjectType] = (
            object_types if object_types is not None else OBJECT_TYPES
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate(self, path: str | Path) -> ValidationResult:
        """Validate the export bundle at *path*.

        Args:
            path: Path to the export bundle directory.

        Returns:
            A :class:`ValidationResult` collecting every error and warning
            found in one pass.

        Raises:
            ExportValidationError: On genuine I/O failures (e.g. permission
                denied) — never for ordinary validation findings.
        """
        result = ValidationResult()
        bundle = Path(path)

        manifest_path = bundle / _MANIFEST_FILENAME
        if not manifest_path.is_file():
            result.errors.append(f"{_MANIFEST_FILENAME} is missing")
            return self._finalize(result)

        manifest, load_error = self._load_json(manifest_path)
        if load_error is not None:
            result.errors.append(f"{_MANIFEST_FILENAME}: {load_error}")
            return self._finalize(result)
        assert manifest is not None  # load_error is None → manifest is set

        self._validate_manifest_head(manifest, result)

        object_types = manifest.get("object_types")
        if not isinstance(object_types, dict):
            result.errors.append(
                f"{_MANIFEST_FILENAME}: 'object_types' must be an object"
            )
            return self._finalize(result)

        seen: set[str] = set()
        for declared_key, info in object_types.items():
            self._validate_type_entry(
                bundle, declared_key, info, seen, result
            )

        return self._finalize(result)

    # ------------------------------------------------------------------
    # Manifest-level checks
    # ------------------------------------------------------------------

    def _validate_manifest_head(
        self, manifest: Mapping[str, Any], result: ValidationResult
    ) -> None:
        """Validate the manifest's kind and version fields."""
        kind = manifest.get("kind")
        if kind != _KIND_MANIFEST:
            result.errors.append(
                f"{_MANIFEST_FILENAME}: kind is {kind!r}, "
                f"expected {_KIND_MANIFEST!r}"
            )

        format_version = manifest.get("format_version")
        schema_version = manifest.get("schema_version")
        result.format_version = self._as_int(format_version)
        result.schema_version = self._as_int(schema_version)

        self._check_version(
            format_version, FORMAT_VERSION, "format_version", result
        )
        self._check_version(
            schema_version, SCHEMA_VERSION, "schema_version", result
        )

    def _check_version(
        self,
        value: Any,
        supported_max: int,
        label: str,
        result: ValidationResult,
    ) -> None:
        """Record an error when *value* is not a supported version integer."""
        if isinstance(value, bool) or not isinstance(value, int):
            result.errors.append(
                f"{_MANIFEST_FILENAME}: {label} must be an integer, "
                f"got {value!r}"
            )
            return
        if value < 1 or value > supported_max:
            result.errors.append(
                f"{_MANIFEST_FILENAME}: unsupported {label} {value} "
                f"(supported: 1..{supported_max})"
            )

    # ------------------------------------------------------------------
    # Type-file checks
    # ------------------------------------------------------------------

    def _validate_type_entry(
        self,
        bundle: Path,
        declared_key: str,
        info: Any,
        seen: set[str],
        result: ValidationResult,
    ) -> None:
        """Validate a single manifest ``object_types`` entry and its file."""
        if not isinstance(info, dict):
            result.errors.append(
                f"manifest object_types[{declared_key!r}] must be an object"
            )
            return

        obj_type = self._object_types.get(declared_key)
        if obj_type is None:
            result.errors.append(f"unknown object_type {declared_key!r}")

        expected_file = (
            obj_type.filename if obj_type is not None else f"{declared_key}.json"
        )
        declared_file = info.get("file")
        if declared_file != expected_file:
            result.errors.append(
                f"object_type {declared_key!r}: manifest file "
                f"{declared_file!r} does not match registry filename "
                f"{expected_file!r}"
            )

        file_name = (
            declared_file if isinstance(declared_file, str) else expected_file
        )
        type_path = bundle / file_name
        if not type_path.is_file():
            result.errors.append(
                f"type file {file_name!r} for {declared_key!r} is missing"
            )
            return

        data, load_error = self._load_json(type_path)
        if load_error is not None:
            result.errors.append(f"{file_name}: {load_error}")
            return
        assert data is not None

        kind = data.get("kind")
        if kind != _KIND_TYPE_FILE:
            result.errors.append(
                f"{file_name}: kind is {kind!r}, expected {_KIND_TYPE_FILE!r}"
            )

        file_type = data.get("object_type")
        if file_type != declared_key:
            result.errors.append(
                f"{file_name}: object_type {file_type!r} does not match "
                f"manifest key {declared_key!r}"
            )

        dup_key = file_type if isinstance(file_type, str) else declared_key
        if dup_key in seen:
            result.errors.append(f"duplicate object_type {dup_key!r}")
        else:
            seen.add(dup_key)

        objects = data.get("objects")
        if not isinstance(objects, list):
            result.errors.append(f"{file_name}: 'objects' must be a list")
            return

        count = data.get("count")
        if count != len(objects):
            result.errors.append(
                f"{file_name}: count {count!r} does not match the number of "
                f"objects ({len(objects)})"
            )

        result.object_count += len(objects)
        if declared_key not in result.object_types:
            result.object_types.append(declared_key)

        if len(objects) == 0:
            result.warnings.append(
                f"object_type {declared_key!r} contains no objects"
            )

        if obj_type is not None:
            self._check_objects(obj_type, objects, file_name, result)

    def _check_objects(
        self,
        obj_type: ObjectType,
        objects: Sequence[Any],
        file_name: str,
        result: ValidationResult,
    ) -> None:
        """Warn about unknown fields and missing natural keys per object."""
        allowed = set(obj_type.fields)
        for index, raw in enumerate(objects):
            if not isinstance(raw, dict):
                result.errors.append(
                    f"{file_name}: object #{index} must be an object"
                )
                continue

            unknown = sorted(set(raw) - allowed)
            if unknown:
                result.warnings.append(
                    f"{file_name}: object #{index} has unknown field(s): "
                    f"{unknown}"
                )

            canonical = CanonicalObject(type=obj_type.key, fields=raw)
            try:
                canonical.identity(obj_type.natural_key)
            except KeyError:
                result.warnings.append(
                    f"{file_name}: object #{index} is missing natural-key "
                    f"field(s) {list(obj_type.natural_key)}"
                )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_json(
        self, path: Path
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Read and parse a JSON object from *path*.

        Returns:
            ``(data, None)`` on success, or ``(None, message)`` for a
            validation problem (invalid JSON, not an object, or — defensively —
            a file that vanished after the existence check).

        Raises:
            ExportValidationError: On a genuine I/O failure such as a
                permission error.
        """
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None, "file not found"
        except PermissionError as exc:
            raise ExportValidationError(
                f"Permission denied reading '{path}': {exc}"
            ) from exc
        except OSError as exc:
            raise ExportValidationError(
                f"I/O error reading '{path}': {exc}"
            ) from exc

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, f"invalid JSON: {exc}"

        if not isinstance(data, dict):
            return None, (
                f"expected a JSON object, got {type(data).__name__}"
            )
        return data, None

    @staticmethod
    def _as_int(value: Any) -> int:
        """Return *value* as an int when it is a non-bool int, else 0."""
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return value

    @staticmethod
    def _finalize(result: ValidationResult) -> ValidationResult:
        """Set :attr:`ValidationResult.valid` from the error list and return."""
        result.valid = not result.errors
        return result
