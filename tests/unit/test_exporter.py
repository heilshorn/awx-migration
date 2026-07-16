"""Unit tests for lib.exporter — export orchestration (mocked client)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from lib.awx_client import AwxClient
from lib.awx_objects import OBJECT_TYPES, ObjectType
from lib.canonical import CanonicalObject
from lib.export_format import read_manifest, read_type_file
from lib.exporter import Exporter, ExportSummary

_META = {
    "tool_version": "0.2.0",
    "awx_version": "24.6.1",
    "exported_at": "2026-07-16T14:30:22Z",
}


class FakeClient(AwxClient):
    """AwxClient stand-in returning canned canonical objects per type."""

    def __init__(self, data: dict[str, list[CanonicalObject]]) -> None:
        self._data = data
        self.export_calls: list[tuple[str, str | None]] = []

    def list_organizations(self) -> list[str]:
        return []

    def export(
        self, object_type: str, *, organization: str | None = None
    ) -> list[CanonicalObject]:
        self.export_calls.append((object_type, organization))
        return list(self._data.get(object_type, []))

    def import_objects(
        self,
        object_type: str,
        objects: Sequence[CanonicalObject],
        *,
        on_conflict: str,
    ):  # pragma: no cover - not used in export tests
        raise NotImplementedError

    def exists(self, object_type: str, identity: tuple) -> bool:
        raise NotImplementedError  # pragma: no cover - not used here


def _exporter(client: FakeClient, object_types: Sequence[ObjectType]) -> Exporter:
    return Exporter(client, object_types, **_META)


def _orgs() -> list[CanonicalObject]:
    return [
        CanonicalObject("organizations", {"name": "Default"}),
        CanonicalObject("organizations", {"name": "Other"}),
    ]


def _job_templates() -> list[CanonicalObject]:
    return [
        CanonicalObject(
            "job_templates", {"name": "Deploy", "organization": "Default"}
        ),
        CanonicalObject(
            "job_templates", {"name": "Backup", "organization": "Default"}
        ),
    ]


# -- export_all -------------------------------------------------------


def test_export_all_writes_a_file_per_type_and_manifest(
    tmp_path: Path,
) -> None:
    client = FakeClient(
        {"organizations": _orgs(), "job_templates": _job_templates()}
    )
    types = [OBJECT_TYPES["organizations"], OBJECT_TYPES["job_templates"]]
    summary = _exporter(client, types).export_all(tmp_path)

    assert (tmp_path / "organizations.json").is_file()
    assert (tmp_path / "job_templates.json").is_file()
    assert (tmp_path / "manifest.json").is_file()
    assert summary == ExportSummary(
        directory=tmp_path, counts={"organizations": 2, "job_templates": 2}
    )


def test_export_all_roundtrips_objects(tmp_path: Path) -> None:
    orgs = _orgs()
    client = FakeClient({"organizations": orgs})
    _exporter(client, [OBJECT_TYPES["organizations"]]).export_all(tmp_path)

    result = read_type_file(tmp_path / "organizations.json")
    assert result.object_type == "organizations"
    assert result.objects == orgs


def test_manifest_lists_types_and_marks_no_secrets(tmp_path: Path) -> None:
    client = FakeClient(
        {"organizations": _orgs(), "job_templates": _job_templates()}
    )
    types = [OBJECT_TYPES["organizations"], OBJECT_TYPES["job_templates"]]
    _exporter(client, types).export_all(tmp_path, organization="Default")

    manifest = read_manifest(tmp_path / "manifest.json")
    assert manifest["contains_secrets"] is False
    assert manifest["organization"] == "Default"
    assert manifest["object_types"] == {
        "organizations": {"count": 2, "file": "organizations.json"},
        "job_templates": {"count": 2, "file": "job_templates.json"},
    }


def test_organization_is_forwarded_to_client(tmp_path: Path) -> None:
    client = FakeClient(
        {"organizations": _orgs(), "job_templates": _job_templates()}
    )
    types = [OBJECT_TYPES["organizations"], OBJECT_TYPES["job_templates"]]
    _exporter(client, types).export_all(tmp_path, organization="Default")

    assert ("organizations", "Default") in client.export_calls
    assert ("job_templates", "Default") in client.export_calls


# -- export_type ------------------------------------------------------


def test_export_type_writes_single_type(tmp_path: Path) -> None:
    client = FakeClient({"job_templates": _job_templates()})
    summary = _exporter(client, []).export_type(
        tmp_path, OBJECT_TYPES["job_templates"]
    )

    assert (tmp_path / "job_templates.json").is_file()
    assert not (tmp_path / "organizations.json").exists()
    assert summary.counts == {"job_templates": 2}
    manifest = read_manifest(tmp_path / "manifest.json")
    assert set(manifest["object_types"]) == {"job_templates"}


# -- export_object ----------------------------------------------------


def test_export_object_filters_by_name(tmp_path: Path) -> None:
    client = FakeClient({"job_templates": _job_templates()})
    summary = _exporter(client, []).export_object(
        tmp_path, OBJECT_TYPES["job_templates"], "Deploy"
    )

    assert summary.counts == {"job_templates": 1}
    result = read_type_file(tmp_path / "job_templates.json")
    assert [o.fields["name"] for o in result.objects] == ["Deploy"]


def test_export_object_missing_name_writes_empty(tmp_path: Path) -> None:
    client = FakeClient({"job_templates": _job_templates()})
    summary = _exporter(client, []).export_object(
        tmp_path, OBJECT_TYPES["job_templates"], "Nonexistent"
    )
    assert summary.counts == {"job_templates": 0}
    assert read_type_file(tmp_path / "job_templates.json").objects == []


# -- filenames --------------------------------------------------------


def test_filenames_match_registry(tmp_path: Path) -> None:
    client = FakeClient({k: [] for k in OBJECT_TYPES})
    types = list(OBJECT_TYPES.values())
    _exporter(client, types).export_all(tmp_path)
    for obj_type in types:
        assert (tmp_path / obj_type.filename).is_file()


# -- extensibility: a brand-new registry type ------------------------


def test_dummy_type_from_registry_flows_through(tmp_path: Path) -> None:
    widgets = ObjectType(
        key="widgets",
        cli_flag="--widgets",
        filename="widgets.json",
        natural_key=("name",),
        org_scoped=False,
        fields=("name", "color"),
    )
    objects = [CanonicalObject("widgets", {"name": "W1", "color": "red"})]
    client = FakeClient({"widgets": objects})

    summary = _exporter(client, [widgets]).export_all(tmp_path)

    assert summary.counts == {"widgets": 1}
    result = read_type_file(tmp_path / "widgets.json")
    assert result.objects == objects
