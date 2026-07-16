"""Unit tests for lib.awx_objects — the object-type registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.awx_objects import OBJECT_TYPES, ObjectType, Relation, import_order
from lib.canonical import CanonicalObject
from lib.export_format import read_type_file, write_type_file

_META = {
    "tool_version": "0.2.0",
    "awx_version": "24.6.1",
    "exported_at": "2026-07-16T14:30:22Z",
}


def test_registry_contains_phase_2_1_types() -> None:
    """The Phase 2.1 example set is present."""
    assert set(OBJECT_TYPES) >= {
        "organizations",
        "projects",
        "inventories",
        "job_templates",
    }


def test_registry_keys_are_consistent() -> None:
    """Each entry's key matches its dict key and its filename."""
    for key, obj_type in OBJECT_TYPES.items():
        assert obj_type.key == key
        assert obj_type.filename == f"{key}.json"


def test_natural_key_and_org_scope_invariant() -> None:
    """org_scoped is true exactly when the natural key includes organization."""
    for obj_type in OBJECT_TYPES.values():
        assert obj_type.natural_key, f"{obj_type.key} has an empty natural key"
        assert obj_type.org_scoped == ("organization" in obj_type.natural_key)


def test_dependencies_reference_known_types() -> None:
    """Every depends_on / relation target refers to a registered type."""
    for obj_type in OBJECT_TYPES.values():
        for dep in obj_type.depends_on:
            assert dep in OBJECT_TYPES
        for rel in obj_type.relations:
            assert rel.target_type in OBJECT_TYPES


def test_reserved_hooks_default_to_none() -> None:
    """The optional hooks are not wired up in this phase."""
    for obj_type in OBJECT_TYPES.values():
        assert obj_type.validator is None
        assert obj_type.exporter is None
        assert obj_type.importer is None
        assert obj_type.post_export is None
        assert obj_type.post_import is None


def test_import_order_places_dependencies_first() -> None:
    """Dependencies are ordered before dependents, regardless of input order."""
    selected = [
        OBJECT_TYPES["job_templates"],
        OBJECT_TYPES["inventories"],
        OBJECT_TYPES["projects"],
        OBJECT_TYPES["organizations"],
    ]
    ordered = [ot.key for ot in import_order(selected)]

    assert ordered[0] == "organizations"
    assert ordered[-1] == "job_templates"
    assert ordered.index("organizations") < ordered.index("projects")
    assert ordered.index("organizations") < ordered.index("inventories")
    assert ordered.index("projects") < ordered.index("job_templates")
    assert ordered.index("inventories") < ordered.index("job_templates")


def test_import_order_ignores_absent_dependencies() -> None:
    """A subset can be ordered even when its dependencies are not selected."""
    ordered = import_order([OBJECT_TYPES["job_templates"]])
    assert [ot.key for ot in ordered] == ["job_templates"]


def test_import_order_detects_cycles() -> None:
    """A dependency cycle raises ValueError."""
    a = ObjectType(
        key="a",
        cli_flag="--a",
        filename="a.json",
        natural_key=("name",),
        org_scoped=False,
        fields=("name",),
        depends_on=("b",),
    )
    b = ObjectType(
        key="b",
        cli_flag="--b",
        filename="b.json",
        natural_key=("name",),
        org_scoped=False,
        fields=("name",),
        depends_on=("a",),
    )
    with pytest.raises(ValueError):
        import_order([a, b])


def test_identity_uses_natural_key() -> None:
    """CanonicalObject.identity extracts values in natural-key order."""
    jt = OBJECT_TYPES["job_templates"]
    obj = CanonicalObject(
        "job_templates", {"name": "Deploy", "organization": "Default"}
    )
    assert obj.identity(jt.natural_key) == ("Deploy", "Default")


def test_identity_missing_field_raises() -> None:
    """A missing natural-key field is reported clearly."""
    obj = CanonicalObject("job_templates", {"name": "Deploy"})
    with pytest.raises(KeyError):
        obj.identity(("name", "organization"))


def test_new_type_flows_through_without_touching_exporter(
    tmp_path: Path,
) -> None:
    """A brand-new object type works via a registry entry alone.

    This proves the extensibility goal at the foundation level: defining a new
    ObjectType and round-tripping its objects needs no change to the format
    layer (and, in later phases, none to the exporter/importer).
    """
    widgets = ObjectType(
        key="widgets",
        cli_flag="--widgets",
        filename="widgets.json",
        natural_key=("name",),
        org_scoped=False,
        fields=("name", "color"),
    )
    objects = [CanonicalObject(widgets.key, {"name": "W1", "color": "red"})]

    path = tmp_path / widgets.filename
    write_type_file(path, widgets.key, objects, **_META)
    result = read_type_file(path)

    assert result.object_type == "widgets"
    assert result.objects == objects
    assert result.objects[0].identity(widgets.natural_key) == ("W1",)


def test_relation_dataclass_defaults() -> None:
    """Relation.many defaults to False."""
    rel = Relation("organization", "organizations")
    assert rel.many is False
