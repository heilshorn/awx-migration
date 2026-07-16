"""AWX client facade — the only layer that knows the AWX data model.

:class:`AwxClient` is the abstract contract used by the (future) exporter and
importer.  They work exclusively with :class:`~lib.canonical.CanonicalObject`
instances; every AWX ⇄ canonical translation happens behind this facade, so the
upper layers never see AWX JSON.

Two implementations are foreseen:

* :class:`AwxCliClient` — drives the ``awx`` command-line binary (this phase).
* ``AwxRestClient`` — a future REST-API implementation (not present yet).

In this phase only the infrastructure is implemented: construction, the AWX
environment for the CLI, and :meth:`AwxCliClient.list_organizations`.  The
translation-heavy methods (:meth:`export`, :meth:`import_objects`,
:meth:`exists`) intentionally raise :class:`NotImplementedError`.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .awx_cli import AwxCli
from .awx_connection import AwxConnection
from .awx_objects import OBJECT_TYPES, ObjectType
from .canonical import CanonicalObject


class AwxClientError(RuntimeError):
    """Raised on facade-level failures (e.g. unparseable AWX output)."""


@dataclass
class ImportResult:
    """Outcome of importing one object type.

    Attributes:
        created: Natural-key labels of objects created.
        updated: Natural-key labels of objects updated.
        skipped: Natural-key labels of objects skipped.
        warnings: Non-fatal messages produced during the import.
        errors: Fatal messages for objects that failed to import.
    """

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class AwxClient(ABC):
    """Abstract contract for talking to AWX in canonical terms."""

    @abstractmethod
    def list_organizations(self) -> list[str]:
        """Return the names of all organizations, sorted."""

    @abstractmethod
    def export(
        self, object_type: str, *, organization: str | None = None
    ) -> list[CanonicalObject]:
        """Export all objects of *object_type* as canonical objects.

        Args:
            object_type: Registry key of the type to export.
            organization: Restrict to this organization, or ``None`` for all.
        """

    @abstractmethod
    def import_objects(
        self,
        object_type: str,
        objects: Sequence[CanonicalObject],
        *,
        on_conflict: str,
    ) -> "ImportResult":
        """Import canonical *objects* of *object_type* into AWX.

        Args:
            object_type: Registry key of the type being imported.
            objects: Canonical objects to import.
            on_conflict: Conflict policy, one of ``"update"``, ``"skip"``,
                ``"fail"``.
        """

    @abstractmethod
    def exists(self, object_type: str, identity: tuple) -> bool:
        """Return ``True`` if an object with *identity* already exists.

        Args:
            object_type: Registry key of the type to check.
            identity: Natural-key identity tuple
                (:meth:`CanonicalObject.identity`).
        """


def _reference_name(value: Any) -> str | None:
    """Reduce a single AWX reference to a natural-key name.

    AWX ``export`` represents a related object either as a natural-key object
    (``{"name": "Linux", ...}``) or as a bare name string.  A raw integer ID is
    deliberately dropped (references are never stored as IDs); an unresolvable
    reference becomes ``None``.

    Args:
        value: The raw reference value from an AWX object.

    Returns:
        The referenced object's name, or ``None`` if it cannot be determined.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        name = value.get("name")
        return name if isinstance(name, str) else None
    return None


def _reference_value(value: Any, *, many: bool) -> Any:
    """Translate a relation value to a natural key, or a list of them.

    Args:
        value: The raw relation value.
        many: ``True`` when the relation holds a list of references.

    Returns:
        A single name (or ``None``) for a to-one relation, or a list of names
        for a to-many relation.
    """
    if many:
        if not isinstance(value, (list, tuple)):
            return []
        return [_reference_name(item) for item in value]
    return _reference_name(value)


class AwxCliClient(AwxClient):
    """AWX client backed by the ``awx`` command-line binary."""

    def __init__(
        self,
        connection: AwxConnection,
        *,
        cli: AwxCli | None = None,
        object_types: Mapping[str, ObjectType] | None = None,
    ) -> None:
        """Initialise the CLI-backed client.

        Args:
            connection: Resolved AWX connection parameters.
            cli: Binary wrapper to use.  Defaults to a detected :class:`AwxCli`.
            object_types: Object-type registry.  Defaults to
                :data:`~lib.awx_objects.OBJECT_TYPES`.
        """
        self._connection = connection
        self._cli = cli if cli is not None else AwxCli.detect()
        self._object_types: Mapping[str, ObjectType] = (
            object_types if object_types is not None else OBJECT_TYPES
        )

    # -- infrastructure ------------------------------------------------

    def _build_env(self) -> dict[str, str]:
        """Return the AWX-specific environment for a CLI call.

        Sets both the ``TOWER_*`` and ``CONTROLLER_*`` variants so the client
        works across awxkit versions.  Only the AWX variables are returned;
        merging with the process environment is the binary wrapper's job.

        Returns:
            Mapping of environment variable name to value.
        """
        conn = self._connection
        verify = "true" if conn.verify_ssl else "false"
        env: dict[str, str] = {
            "TOWER_HOST": conn.host,
            "CONTROLLER_HOST": conn.host,
            "TOWER_VERIFY_SSL": verify,
            "CONTROLLER_VERIFY_SSL": verify,
        }
        if conn.token:
            env["TOWER_OAUTH_TOKEN"] = conn.token
            env["CONTROLLER_OAUTH_TOKEN"] = conn.token
        else:
            env["TOWER_USERNAME"] = conn.username or ""
            env["CONTROLLER_USERNAME"] = conn.username or ""
            env["TOWER_PASSWORD"] = conn.password or ""
            env["CONTROLLER_PASSWORD"] = conn.password or ""
        return env

    def _run(self, args: Sequence[str], *, stdin: str | None = None) -> str:
        """Run the ``awx`` binary with the AWX environment applied."""
        return self._cli.run(args, env=self._build_env(), stdin=stdin)

    # -- read-only queries ---------------------------------------------

    def list_organizations(self) -> list[str]:
        """Return the names of all organizations, sorted.

        Returns:
            Sorted list of organization names.

        Raises:
            AwxClientError: If the CLI output cannot be parsed.
            AwxCliError: If the CLI invocation fails.
        """
        out = self._run(["organizations", "list", "-f", "json"])
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            raise AwxClientError(
                f"Could not parse 'awx organizations list' output: {exc}"
            ) from exc

        if isinstance(data, Mapping):
            results = data.get("results", [])
        elif isinstance(data, list):
            results = data
        else:
            raise AwxClientError(
                "Unexpected 'awx organizations list' output shape: "
                f"{type(data).__name__}"
            )

        names = [
            item["name"]
            for item in results
            if isinstance(item, Mapping) and "name" in item
        ]
        return sorted(names)

    # -- translation-heavy methods (later phases) ----------------------

    def export(
        self, object_type: str, *, organization: str | None = None
    ) -> list[CanonicalObject]:
        """Export all objects of *object_type* as canonical objects.

        Runs ``awx export`` for the type, translates each raw AWX object into a
        :class:`~lib.canonical.CanonicalObject` by applying the type's field
        whitelist and mapping every relation to a natural key (never an ID),
        then optionally filters by organization.

        The organization filter is applied locally: the full type is exported
        first (the ``awx`` CLI cannot filter reliably), then organization-scoped
        objects are narrowed to *organization*.  Types that are not
        organization-scoped are returned unfiltered.

        Args:
            object_type: Registry key of the type to export.
            organization: Restrict organization-scoped objects to this
                organization, or ``None`` for all.

        Returns:
            The exported objects as canonical objects.

        Raises:
            AwxClientError: If the type is unknown or the CLI output is
                malformed.
            AwxCliError: If the CLI invocation fails.
        """
        obj_type = self._object_types.get(object_type)
        if obj_type is None:
            raise AwxClientError(f"Unknown object type: {object_type!r}")

        raw_objects = self._export_raw(obj_type)
        canonical = [self._to_canonical(obj_type, raw) for raw in raw_objects]

        if organization is not None and obj_type.org_scoped:
            canonical = [
                obj
                for obj in canonical
                if obj.fields.get("organization") == organization
            ]
        return canonical

    def _export_raw(self, obj_type: ObjectType) -> list[dict[str, Any]]:
        """Run ``awx export`` for *obj_type* and return its raw object list.

        Raises:
            AwxClientError: If the output is not valid JSON or has an
                unexpected shape.
        """
        out = self._run(["export", obj_type.cli_flag])
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            raise AwxClientError(
                f"Could not parse 'awx export {obj_type.cli_flag}' output: "
                f"{exc}"
            ) from exc

        if isinstance(data, Mapping):
            items = data.get(obj_type.key, [])
        elif isinstance(data, list):
            items = data
        else:
            raise AwxClientError(
                "Unexpected 'awx export' output shape: "
                f"{type(data).__name__}"
            )

        if not isinstance(items, list):
            raise AwxClientError(
                f"'awx export' did not return a list for {obj_type.key!r}"
            )

        result: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, Mapping):
                raise AwxClientError(
                    f"'awx export' returned a non-object entry for "
                    f"{obj_type.key!r}"
                )
            result.append(dict(item))
        return result

    def _to_canonical(
        self, obj_type: ObjectType, raw: Mapping[str, Any]
    ) -> CanonicalObject:
        """Translate one raw AWX object into a canonical object.

        Only whitelisted fields are copied; relations are reduced to natural
        keys.  Any field not in the whitelist (``id``, ``url``, timestamps,
        ``summary_fields`` …) is dropped.
        """
        relations = {rel.field: rel for rel in obj_type.relations}
        fields: dict[str, Any] = {}
        for field_name in obj_type.fields:
            if field_name not in raw:
                continue
            value = raw[field_name]
            rel = relations.get(field_name)
            if rel is not None:
                fields[field_name] = _reference_value(value, many=rel.many)
            else:
                fields[field_name] = value
        return CanonicalObject(type=obj_type.key, fields=fields)

    def import_objects(
        self,
        object_type: str,
        objects: Sequence[CanonicalObject],
        *,
        on_conflict: str,
    ) -> ImportResult:
        """Not implemented in this phase."""
        raise NotImplementedError(
            "AwxCliClient.import_objects is implemented in a later phase"
        )

    def exists(self, object_type: str, identity: tuple) -> bool:
        """Not implemented in this phase."""
        raise NotImplementedError(
            "AwxCliClient.exists is implemented in a later phase"
        )


def make_client(
    connection: AwxConnection,
    kind: str = "cli",
    *,
    cli: AwxCli | None = None,
    object_types: Mapping[str, ObjectType] | None = None,
) -> AwxClient:
    """Create an :class:`AwxClient` of the requested *kind*.

    Args:
        connection: Resolved AWX connection parameters.
        kind: Client implementation to build.  Only ``"cli"`` is available.
        cli: Optional binary wrapper override (for the CLI client).
        object_types: Optional object-type registry override.

    Returns:
        A concrete :class:`AwxClient`.

    Raises:
        ValueError: If *kind* is not a known client kind.
    """
    if kind == "cli":
        return AwxCliClient(
            connection, cli=cli, object_types=object_types
        )
    raise ValueError(f"Unknown AWX client kind: {kind!r}")
