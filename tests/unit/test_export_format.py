"""Unit tests for lib.export_format — the stable on-disk export format."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.canonical import CanonicalObject
from lib.export_format import (
    FORMAT_VERSION,
    SCHEMA_VERSION,
    ExportFormatError,
    read_manifest,
    read_type_file,
    write_manifest,
    write_type_file,
)

_META = {
    "tool_version": "0.2.0",
    "awx_version": "24.6.1",
    "exported_at": "2026-07-16T14:30:22Z",
}


def _sample_objects() -> list[CanonicalObject]:
    return [
        CanonicalObject(
            "job_templates",
            {"name": "Deploy", "organization": "Default", "project": "Infra"},
        ),
        CanonicalObject(
            "job_templates",
            {"name": "Backup", "organization": "Default", "project": "Infra"},
        ),
    ]


def test_type_file_roundtrip(tmp_path: Path) -> None:
    """CanonicalObject → write → read → CanonicalObject must be identical."""
    path = tmp_path / "job_templates.json"
    objects = _sample_objects()

    write_type_file(
        path, "job_templates", objects, organization="Default", **_META
    )
    result = read_type_file(path)

    assert result.object_type == "job_templates"
    assert result.format_version == FORMAT_VERSION
    assert result.schema_version == SCHEMA_VERSION
    assert result.metadata["count"] == 2
    assert result.metadata["organization"] == "Default"
    assert result.objects == objects


def test_type_file_persists_natural_key_as_separate_metadata(
    tmp_path: Path,
) -> None:
    """natural_key round-trips in a parallel array, separate from fields."""
    path = tmp_path / "inventories.json"
    objects = [
        CanonicalObject(
            "inventories",
            {"name": "Demo", "organization": "Default"},
            natural_key={"name": "Demo", "organization": "Default"},
        )
    ]
    write_type_file(path, "inventories", objects, **_META)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["natural_keys"] == [{"name": "Demo", "organization": "Default"}]
    # Identity metadata is kept out of the business-field objects.
    assert "natural_key" not in raw["objects"][0]

    result = read_type_file(path)
    assert result.objects == objects
    assert result.objects[0].natural_key == {
        "name": "Demo",
        "organization": "Default",
    }


def test_type_file_omits_natural_keys_when_absent(tmp_path: Path) -> None:
    """No parallel array is written when no object carries identity metadata."""
    path = tmp_path / "organizations.json"
    write_type_file(
        path,
        "organizations",
        [CanonicalObject("organizations", {"name": "Default"})],
        **_META,
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "natural_keys" not in raw


def test_type_file_envelope_fields(tmp_path: Path) -> None:
    """The written envelope carries both version numbers and the kind."""
    path = tmp_path / "organizations.json"
    write_type_file(
        path,
        "organizations",
        [CanonicalObject("organizations", {"name": "Default"})],
        **_META,
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["format_version"] == FORMAT_VERSION
    assert raw["schema_version"] == SCHEMA_VERSION
    assert raw["kind"] == "type_file"
    assert raw["object_type"] == "organizations"
    assert raw["count"] == 1


def test_write_type_file_rejects_type_mismatch(tmp_path: Path) -> None:
    """Objects whose type differs from the file's type are rejected."""
    path = tmp_path / "projects.json"
    with pytest.raises(ExportFormatError):
        write_type_file(
            path,
            "projects",
            [CanonicalObject("job_templates", {"name": "X"})],
            **_META,
        )


def test_read_type_file_rejects_future_version(tmp_path: Path) -> None:
    """A newer format_version than supported is refused, not silently read."""
    path = tmp_path / "future.json"
    path.write_text(
        json.dumps(
            {
                "format_version": FORMAT_VERSION + 99,
                "schema_version": SCHEMA_VERSION,
                "kind": "type_file",
                "object_type": "organizations",
                "objects": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ExportFormatError):
        read_type_file(path)


def test_read_type_file_rejects_wrong_kind(tmp_path: Path) -> None:
    """A manifest document is not accepted where a type file is expected."""
    path = tmp_path / "manifest.json"
    write_manifest(path, object_types={}, **_META)
    with pytest.raises(ExportFormatError):
        read_type_file(path)


def test_read_type_file_missing_file(tmp_path: Path) -> None:
    """A missing file raises ExportFormatError, not OSError."""
    with pytest.raises(ExportFormatError):
        read_type_file(tmp_path / "does-not-exist.json")


def test_read_type_file_invalid_json(tmp_path: Path) -> None:
    """Malformed JSON raises ExportFormatError."""
    path = tmp_path / "bad.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ExportFormatError):
        read_type_file(path)


def test_manifest_roundtrip(tmp_path: Path) -> None:
    """Manifest write → read preserves metadata and marks no secrets."""
    path = tmp_path / "manifest.json"
    object_types = {
        "job_templates": {"count": 18, "file": "job_templates.json"},
        "organizations": {"count": 2, "file": "organizations.json"},
    }
    write_manifest(
        path, object_types=object_types, organization="Default", **_META
    )
    manifest = read_manifest(path)

    assert manifest["kind"] == "manifest"
    assert manifest["contains_secrets"] is False
    assert manifest["organization"] == "Default"
    assert manifest["object_types"] == object_types
    assert manifest["format_version"] == FORMAT_VERSION
    assert manifest["schema_version"] == SCHEMA_VERSION


def test_read_manifest_rejects_wrong_kind(tmp_path: Path) -> None:
    """A type file is not accepted where a manifest is expected."""
    path = tmp_path / "job_templates.json"
    write_type_file(path, "job_templates", [], **_META)
    with pytest.raises(ExportFormatError):
        read_manifest(path)
