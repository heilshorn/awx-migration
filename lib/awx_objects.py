"""Object-type registry for AWX export/import.

The registry is the single extension point of the export/import feature: adding
support for a new AWX object type should require **only** a new
:class:`ObjectType` entry in :data:`OBJECT_TYPES` — no changes to the exporter,
importer, or file format.

An :class:`ObjectType` is a pure, declarative data container.  It holds no
logic; the optional hook callables (``validator``, ``exporter``, ``importer``,
``post_export``, ``post_import``) are placeholders for later phases and default
to ``None``.  This module contains no AWX access logic and no subprocess calls.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Relation:
    """Declares that a canonical field references another object type.

    References are always expressed via natural keys (names), so a relation
    maps a field name to the registry key of the referenced type.

    Attributes:
        field: Canonical field holding the reference, e.g. ``"inventory"``.
        target_type: Registry key of the referenced type, e.g.
            ``"inventories"``.
        many: ``True`` when the field holds a list of references.
    """

    field: str
    target_type: str
    many: bool = False


@dataclass(frozen=True)
class ObjectType:
    """Declarative description of one supported AWX object type.

    Attributes:
        key: Registry key / canonical type name, e.g. ``"job_templates"``.
        cli_flag: ``awx`` CLI export flag for this type, e.g.
            ``"--job_templates"`` (consumed by the CLI facade in a later phase).
        filename: File name used for this type inside an export bundle.
        natural_key: Ordered field names forming the object's natural key.
        org_scoped: ``True`` when objects belong to an organization (their
            natural key includes ``"organization"``).
        fields: Whitelist of canonical fields exported for this type.  Only
            listed fields are ever written; new internal AWX fields can never
            leak into an export.
        relations: Reference fields and their target types.
        depends_on: Registry keys that must be imported before this type.
        validator: Optional ``(CanonicalObject) -> list[str]`` hook returning
            validation messages.  Reserved for later phases.
        exporter: Optional full-override export hook.  Reserved.
        importer: Optional full-override import hook.  Reserved.
        post_export: Optional post-processing hook for exported objects.
            Reserved.
        post_import: Optional cross-type fix-up hook run after import.
            Reserved.
    """

    key: str
    cli_flag: str
    filename: str
    natural_key: tuple[str, ...]
    org_scoped: bool
    fields: tuple[str, ...]
    relations: tuple[Relation, ...] = ()
    depends_on: tuple[str, ...] = ()
    validator: Callable[..., Any] | None = None
    exporter: Callable[..., Any] | None = None
    importer: Callable[..., Any] | None = None
    post_export: Callable[..., Any] | None = None
    post_import: Callable[..., Any] | None = None


# ---------------------------------------------------------------------------
# Registry — Phase 2.1 example set.
# Extend by adding a new ObjectType entry; nothing else needs to change.
# ---------------------------------------------------------------------------
OBJECT_TYPES: dict[str, ObjectType] = {
    "organizations": ObjectType(
        key="organizations",
        cli_flag="--organizations",
        filename="organizations.json",
        natural_key=("name",),
        org_scoped=False,
        fields=("name", "description", "max_hosts"),
    ),
    "projects": ObjectType(
        key="projects",
        cli_flag="--projects",
        filename="projects.json",
        natural_key=("name", "organization"),
        org_scoped=True,
        fields=(
            "name",
            "description",
            "organization",
            "scm_type",
            "scm_url",
            "scm_branch",
            "scm_clean",
            "scm_delete_on_update",
            "scm_update_on_launch",
        ),
        relations=(Relation("organization", "organizations"),),
        depends_on=("organizations",),
    ),
    "inventories": ObjectType(
        key="inventories",
        cli_flag="--inventories",
        filename="inventories.json",
        natural_key=("name", "organization"),
        org_scoped=True,
        fields=(
            "name",
            "description",
            "organization",
            "kind",
            "variables",
        ),
        relations=(Relation("organization", "organizations"),),
        depends_on=("organizations",),
    ),
    "job_templates": ObjectType(
        key="job_templates",
        cli_flag="--job_templates",
        filename="job_templates.json",
        natural_key=("name", "organization"),
        org_scoped=True,
        fields=(
            "name",
            "description",
            "organization",
            "job_type",
            "inventory",
            "project",
            "playbook",
            "execution_environment",
            "forks",
            "limit",
            "verbosity",
            "job_tags",
            "skip_tags",
            "ask_variables_on_launch",
        ),
        relations=(
            Relation("organization", "organizations"),
            Relation("inventory", "inventories"),
            Relation("project", "projects"),
        ),
        depends_on=("organizations", "projects", "inventories"),
    ),
}


def import_order(selected: Iterable[ObjectType]) -> list[ObjectType]:
    """Order *selected* object types so dependencies come first.

    Performs a depth-first topological sort using each type's ``depends_on``.
    Dependencies that are not part of *selected* are ignored for ordering, so
    any subset of types can be ordered on its own.  The result is
    deterministic: independent types keep their input order.

    Args:
        selected: Object types to order.

    Returns:
        A new list of the same object types, dependencies before dependents.

    Raises:
        ValueError: If ``depends_on`` forms a cycle among *selected*.
    """
    items = list(selected)
    by_key: dict[str, ObjectType] = {ot.key: ot for ot in items}
    ordered: list[ObjectType] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(obj_type: ObjectType) -> None:
        if obj_type.key in visited:
            return
        if obj_type.key in visiting:
            raise ValueError(
                f"Dependency cycle detected involving {obj_type.key!r}"
            )
        visiting.add(obj_type.key)
        for dep in obj_type.depends_on:
            dep_type = by_key.get(dep)
            if dep_type is not None:
                visit(dep_type)
        visiting.discard(obj_type.key)
        visited.add(obj_type.key)
        ordered.append(obj_type)

    for obj_type in items:
        visit(obj_type)
    return ordered
