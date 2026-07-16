"""Unit tests for lib.export_validator — export bundle validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.canonical import CanonicalObject
from lib.export_format import (
    FORMAT_VERSION,
    SCHEMA_VERSION,
    write_manifest,
    write_type_file,
)
from lib.export_validator import (
    ExportValidationError,
    ExportValidator,
    ValidationResult,
)

_META = {
    "tool_version": "0.1.0",
    "awx_version": "24.6.1",
    "exported_at": "2026-07-16T14:30:22Z",
}


def _write_valid_bundle(tmp_path: Path) -> None:
    """Create a well-formed two-type export bundle."""
    write_type_file(
        tmp_path / "organizations.json",
        "organizations",
        [CanonicalObject("organizations", {"name": "Default"})],
        **_META,
    )
    write_type_file(
        tmp_path / "job_templates.json",
        "job_templates",
        [
            CanonicalObject(
                "job_templates", {"name": "Deploy", "organization": "Default"}
            )
        ],
        **_META,
    )
    write_manifest(
        tmp_path / "manifest.json",
        object_types={
            "organizations": {"count": 1, "file": "organizations.json"},
            "job_templates": {"count": 1, "file": "job_templates.json"},
        },
        **_META,
    )


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


# -- happy path -------------------------------------------------------


def test_valid_bundle(tmp_path: Path) -> None:
    _write_valid_bundle(tmp_path)
    result = ExportValidator().validate(tmp_path)

    assert result.valid is True
    assert result.errors == []
    assert result.object_count == 2
    assert sorted(result.object_types) == ["job_templates", "organizations"]
    assert result.format_version == FORMAT_VERSION
    assert result.schema_version == SCHEMA_VERSION


# -- manifest problems ------------------------------------------------


def test_missing_manifest(tmp_path: Path) -> None:
    result = ExportValidator().validate(tmp_path)
    assert result.valid is False
    assert any("manifest.json" in e for e in result.errors)


def test_invalid_json_manifest(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text("{ not json", encoding="utf-8")
    result = ExportValidator().validate(tmp_path)
    assert result.valid is False
    assert any("invalid JSON" in e for e in result.errors)


def test_wrong_manifest_kind(tmp_path: Path) -> None:
    _write_valid_bundle(tmp_path)
    manifest = _read(tmp_path / "manifest.json")
    manifest["kind"] = "bogus"
    _write(tmp_path / "manifest.json", manifest)

    result = ExportValidator().validate(tmp_path)
    assert result.valid is False
    assert any("kind" in e for e in result.errors)


def test_unknown_format_version(tmp_path: Path) -> None:
    _write_valid_bundle(tmp_path)
    manifest = _read(tmp_path / "manifest.json")
    manifest["format_version"] = 99
    _write(tmp_path / "manifest.json", manifest)

    result = ExportValidator().validate(tmp_path)
    assert result.valid is False
    assert result.format_version == 99
    assert any("format_version" in e for e in result.errors)


def test_unknown_schema_version(tmp_path: Path) -> None:
    _write_valid_bundle(tmp_path)
    manifest = _read(tmp_path / "manifest.json")
    manifest["schema_version"] = 99
    _write(tmp_path / "manifest.json", manifest)

    result = ExportValidator().validate(tmp_path)
    assert result.valid is False
    assert result.schema_version == 99
    assert any("schema_version" in e for e in result.errors)


# -- type-file problems -----------------------------------------------


def test_missing_type_file(tmp_path: Path) -> None:
    _write_valid_bundle(tmp_path)
    (tmp_path / "job_templates.json").unlink()

    result = ExportValidator().validate(tmp_path)
    assert result.valid is False
    assert any("missing" in e and "job_templates" in e for e in result.errors)


def test_wrong_count(tmp_path: Path) -> None:
    _write_valid_bundle(tmp_path)
    doc = _read(tmp_path / "job_templates.json")
    doc["count"] = 5  # actual objects: 1
    _write(tmp_path / "job_templates.json", doc)

    result = ExportValidator().validate(tmp_path)
    assert result.valid is False
    assert any("count" in e for e in result.errors)


def test_wrong_type_file_kind(tmp_path: Path) -> None:
    _write_valid_bundle(tmp_path)
    doc = _read(tmp_path / "organizations.json")
    doc["kind"] = "manifest"
    _write(tmp_path / "organizations.json", doc)

    result = ExportValidator().validate(tmp_path)
    assert result.valid is False
    assert any("kind" in e for e in result.errors)


def test_unknown_object_type(tmp_path: Path) -> None:
    write_type_file(
        tmp_path / "widgets.json",
        "widgets",
        [CanonicalObject("widgets", {"name": "W"})],
        **_META,
    )
    write_manifest(
        tmp_path / "manifest.json",
        object_types={"widgets": {"count": 1, "file": "widgets.json"}},
        **_META,
    )

    result = ExportValidator().validate(tmp_path)
    assert result.valid is False
    assert any("unknown object_type" in e for e in result.errors)


def test_duplicate_object_type(tmp_path: Path) -> None:
    # organizations.json is correct; job_templates.json (wrongly) also claims
    # to be an organizations type file → the object_type appears twice.
    write_type_file(
        tmp_path / "organizations.json",
        "organizations",
        [CanonicalObject("organizations", {"name": "Default"})],
        **_META,
    )
    write_type_file(
        tmp_path / "job_templates.json",
        "organizations",  # collision on purpose
        [CanonicalObject("organizations", {"name": "Other"})],
        **_META,
    )
    write_manifest(
        tmp_path / "manifest.json",
        object_types={
            "organizations": {"count": 1, "file": "organizations.json"},
            "job_templates": {"count": 1, "file": "job_templates.json"},
        },
        **_META,
    )

    result = ExportValidator().validate(tmp_path)
    assert result.valid is False
    assert any("duplicate object_type" in e for e in result.errors)


# -- warnings (no errors) ---------------------------------------------


def test_empty_type_produces_warning_not_error(tmp_path: Path) -> None:
    write_type_file(
        tmp_path / "organizations.json", "organizations", [], **_META
    )
    write_manifest(
        tmp_path / "manifest.json",
        object_types={
            "organizations": {"count": 0, "file": "organizations.json"}
        },
        **_META,
    )

    result = ExportValidator().validate(tmp_path)
    assert result.valid is True
    assert result.errors == []
    assert any("no objects" in w for w in result.warnings)


def test_unknown_field_produces_warning_not_error(tmp_path: Path) -> None:
    _write_valid_bundle(tmp_path)
    doc = _read(tmp_path / "job_templates.json")
    doc["objects"][0]["totally_unknown_field"] = "x"
    _write(tmp_path / "job_templates.json", doc)

    result = ExportValidator().validate(tmp_path)
    assert result.valid is True  # unknown fields are warnings only
    assert any("unknown field" in w for w in result.warnings)


# -- multiple errors & non-abort --------------------------------------


def test_multiple_errors_collected(tmp_path: Path) -> None:
    _write_valid_bundle(tmp_path)
    # Problem 1: manifest kind wrong.
    manifest = _read(tmp_path / "manifest.json")
    manifest["kind"] = "wrong"
    _write(tmp_path / "manifest.json", manifest)
    # Problem 2: organizations count wrong.
    org = _read(tmp_path / "organizations.json")
    org["count"] = 99
    _write(tmp_path / "organizations.json", org)
    # Problem 3: job_templates kind wrong.
    jt = _read(tmp_path / "job_templates.json")
    jt["kind"] = "nope"
    _write(tmp_path / "job_templates.json", jt)

    result = ExportValidator().validate(tmp_path)
    assert result.valid is False
    assert len(result.errors) >= 3


def test_does_not_abort_on_first_error(tmp_path: Path) -> None:
    """A problem in the first type file must not skip the second one."""
    _write_valid_bundle(tmp_path)
    for name in ("organizations.json", "job_templates.json"):
        doc = _read(tmp_path / name)
        doc["count"] = 123  # both wrong
        _write(tmp_path / name, doc)

    result = ExportValidator().validate(tmp_path)
    assert result.valid is False
    # Both type files were validated → both count errors present.
    assert any("organizations.json" in e for e in result.errors)
    assert any("job_templates.json" in e for e in result.errors)


# -- exceptions reserved for I/O --------------------------------------


def test_permission_error_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_valid_bundle(tmp_path)

    def deny(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_text", deny)
    with pytest.raises(ExportValidationError):
        ExportValidator().validate(tmp_path)


# -- dataclass --------------------------------------------------------


def test_validation_result_lists_are_independent() -> None:
    a = ValidationResult()
    b = ValidationResult()
    a.errors.append("x")
    a.warnings.append("y")
    assert b.errors == []
    assert b.warnings == []
