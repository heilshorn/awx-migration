"""Unit tests for lib.importer — import orchestration (mocked client)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from lib.awx_client import AwxClient, ImportResult
from lib.awx_objects import OBJECT_TYPES, ObjectType
from lib.canonical import CanonicalObject
from lib.export_format import write_manifest, write_type_file
from lib.export_validator import ValidationResult
from lib.importer import ImportError, Importer, ImportSummary

_META = {
    "tool_version": "0.1.0",
    "awx_version": "24.6.1",
    "exported_at": "2026-07-16T14:30:22Z",
}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeAwxClient(AwxClient):
    """AwxClient stand-in recording import calls and returning canned results."""

    def __init__(
        self,
        results: dict[str, ImportResult] | None = None,
        events: list[str] | None = None,
    ) -> None:
        self._results = results or {}
        self._events = events
        self.import_calls: list[tuple[str, list[CanonicalObject], str]] = []

    def list_organizations(self) -> list[str]:  # pragma: no cover
        return []

    def export(self, object_type, *, organization=None):  # pragma: no cover
        raise NotImplementedError

    def import_objects(
        self,
        object_type: str,
        objects: Sequence[CanonicalObject],
        *,
        on_conflict: str,
    ) -> ImportResult:
        if self._events is not None:
            self._events.append(f"import:{object_type}")
        self.import_calls.append((object_type, list(objects), on_conflict))
        return self._results.get(object_type, ImportResult())

    def exists(self, object_type, identity):  # pragma: no cover
        raise NotImplementedError


class FakeValidator:
    """ExportValidator stand-in returning a fixed result and recording calls."""

    def __init__(self, result: ValidationResult, events: list[str] | None = None):
        self._result = result
        self._events = events
        self.calls: list[Path] = []

    def validate(self, path) -> ValidationResult:  # type: ignore[no-untyped-def]
        if self._events is not None:
            self._events.append("validate")
        self.calls.append(Path(path))
        return self._result


# ---------------------------------------------------------------------------
# Bundle helpers
# ---------------------------------------------------------------------------


def _write_bundle(
    tmp_path: Path,
    data: Mapping[str, list[CanonicalObject]],
    registry: Mapping[str, ObjectType] = OBJECT_TYPES,
) -> None:
    manifest_types: dict[str, dict[str, object]] = {}
    for key, objs in data.items():
        obj_type = registry[key]
        write_type_file(tmp_path / obj_type.filename, key, objs, **_META)
        manifest_types[key] = {"count": len(objs), "file": obj_type.filename}
    write_manifest(tmp_path / "manifest.json", object_types=manifest_types, **_META)


def _org(name: str) -> CanonicalObject:
    return CanonicalObject("organizations", {"name": name})


def _jt(name: str) -> CanonicalObject:
    return CanonicalObject(
        "job_templates", {"name": name, "organization": "Default"}
    )


# ---------------------------------------------------------------------------
# Validator gate
# ---------------------------------------------------------------------------


def test_validator_is_invoked_first(tmp_path: Path) -> None:
    _write_bundle(tmp_path, {"organizations": [_org("Default")]})
    events: list[str] = []
    validator = FakeValidator(ValidationResult(valid=True), events=events)
    client = FakeAwxClient(events=events)

    Importer(client, validator=validator).import_path(tmp_path)

    assert validator.calls == [tmp_path]
    assert events[0] == "validate"
    assert "import:organizations" in events


def test_invalid_bundle_raises_and_skips_import(tmp_path: Path) -> None:
    _write_bundle(tmp_path, {"organizations": [_org("Default")]})
    validator = FakeValidator(
        ValidationResult(valid=False, errors=["boom"])
    )
    client = FakeAwxClient()

    with pytest.raises(ImportError):
        Importer(client, validator=validator).import_path(tmp_path)
    assert client.import_calls == []  # import gated by validation


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_import_follows_dependency_order(tmp_path: Path) -> None:
    _write_bundle(
        tmp_path,
        {"job_templates": [_jt("Deploy")], "organizations": [_org("Default")]},
    )
    client = FakeAwxClient()
    Importer(client).import_path(tmp_path)

    imported = [call[0] for call in client.import_calls]
    assert imported.index("organizations") < imported.index("job_templates")


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_type_filter_limits_imported_types(tmp_path: Path) -> None:
    _write_bundle(
        tmp_path,
        {
            "organizations": [_org("Default")],
            "projects": [
                CanonicalObject(
                    "projects", {"name": "Infra", "organization": "Default"}
                )
            ],
            "job_templates": [_jt("Deploy")],
        },
    )
    client = FakeAwxClient()
    summary = Importer(client).import_path(
        tmp_path, types=["organizations"]
    )

    assert [call[0] for call in client.import_calls] == ["organizations"]
    assert summary.imported_types == ["organizations"]


def test_name_filter_selects_single_object(tmp_path: Path) -> None:
    _write_bundle(tmp_path, {"job_templates": [_jt("Deploy"), _jt("Backup")]})
    client = FakeAwxClient()
    Importer(client).import_path(
        tmp_path, types=["job_templates"], name="Deploy"
    )

    _, objects, _ = client.import_calls[0]
    assert [o.fields["name"] for o in objects] == ["Deploy"]


def test_name_requires_single_type(tmp_path: Path) -> None:
    _write_bundle(
        tmp_path,
        {"organizations": [_org("Default")], "job_templates": [_jt("Deploy")]},
    )
    with pytest.raises(ImportError):
        Importer(FakeAwxClient()).import_path(tmp_path, name="Deploy")


def test_on_conflict_is_forwarded(tmp_path: Path) -> None:
    _write_bundle(tmp_path, {"organizations": [_org("Default")]})
    client = FakeAwxClient()
    Importer(client).import_path(tmp_path, on_conflict="skip")
    assert client.import_calls[0][2] == "skip"


# ---------------------------------------------------------------------------
# Result aggregation
# ---------------------------------------------------------------------------


def test_merges_multiple_import_results(tmp_path: Path) -> None:
    _write_bundle(
        tmp_path,
        {"organizations": [_org("Default")], "job_templates": [_jt("Deploy")]},
    )
    client = FakeAwxClient(
        results={
            "organizations": ImportResult(created=["Default"]),
            "job_templates": ImportResult(
                updated=["Deploy"], skipped=["Old"], errors=["boom"]
            ),
        }
    )
    summary = Importer(client).import_path(tmp_path)

    assert summary.created == ["Default"]
    assert summary.updated == ["Deploy"]
    assert summary.skipped == ["Old"]
    assert summary.errors == ["boom"]
    assert summary.object_count == 2
    assert sorted(summary.imported_types) == ["job_templates", "organizations"]


def test_errors_are_collected_not_raised(tmp_path: Path) -> None:
    _write_bundle(tmp_path, {"organizations": [_org("Default")]})
    client = FakeAwxClient(
        results={"organizations": ImportResult(errors=["nope"])}
    )
    summary = Importer(client).import_path(tmp_path)
    assert summary.errors == ["nope"]


def test_warnings_from_validator_and_client_are_merged(
    tmp_path: Path,
) -> None:
    # Empty organizations type → real validator emits a "no objects" warning.
    _write_bundle(
        tmp_path,
        {"organizations": [], "job_templates": [_jt("Deploy")]},
    )
    client = FakeAwxClient(
        results={"job_templates": ImportResult(warnings=["client-warn"])}
    )
    summary = Importer(client).import_path(tmp_path)

    assert any("no objects" in w for w in summary.warnings)  # validator
    assert "client-warn" in summary.warnings  # client


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_bundle_imports_nothing(tmp_path: Path) -> None:
    write_manifest(tmp_path / "manifest.json", object_types={}, **_META)
    client = FakeAwxClient()
    summary = Importer(client).import_path(tmp_path)

    assert client.import_calls == []
    assert summary.object_count == 0
    assert summary.imported_types == []
    assert isinstance(summary, ImportSummary)


def test_dummy_type_from_registry(tmp_path: Path) -> None:
    widgets = ObjectType(
        key="widgets",
        cli_flag="--widgets",
        filename="widgets.json",
        natural_key=("name",),
        org_scoped=False,
        fields=("name", "color"),
    )
    registry = {"widgets": widgets}
    objects = [CanonicalObject("widgets", {"name": "W1", "color": "red"})]
    _write_bundle(tmp_path, {"widgets": objects}, registry=registry)

    client = FakeAwxClient()
    summary = Importer(client, object_types=registry).import_path(tmp_path)

    assert client.import_calls[0][0] == "widgets"
    assert summary.imported_types == ["widgets"]
    assert client.import_calls[0][1] == objects
