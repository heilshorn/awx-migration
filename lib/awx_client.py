"""AWX client facade — the only layer that knows the AWX data model.

:class:`AwxClient` is the abstract contract used by the (future) exporter and
importer.  They work exclusively with :class:`~lib.canonical.CanonicalObject`
instances; every AWX ⇄ canonical translation happens behind this facade, so the
upper layers never see AWX JSON.

Two implementations are foreseen:

* :class:`AwxCliClient` — drives the ``awx`` command-line binary (this phase).
* ``AwxRestClient`` — a future REST-API implementation (not present yet).

This module owns both translation directions: AWX → canonical (export) and
canonical → AWX (import).  No other layer parses or produces native AWX JSON.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .awx_cli import AwxCli, AwxCliError
from .awx_connection import AwxConnection
from .awx_objects import OBJECT_TYPES, ObjectType
from .canonical import CanonicalObject

log: logging.Logger = logging.getLogger("awx-migration")

#: Conflict policies accepted by :meth:`AwxCliClient.import_objects`.
_CONFLICT_POLICIES: tuple[str, ...] = ("update", "skip", "fail")

#: AWX ``type`` value of an organization inside a natural key.  Org-scoped
#: references nest their organization under this type.
_ORGANIZATION_TYPE: str = "organization"


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

    @abstractmethod
    def missing_credentials(
        self, refs: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Return the subset of credential *refs* absent from the target AWX.

        Args:
            refs: Canonical credential references (reduced natural-key dicts,
                as stored on a job template's ``credentials`` field).

        Returns:
            The references that do not resolve to an existing credential.  A
            query failure yields an empty list (nothing known-missing) so an
            import is never blocked by a transient read error.
        """


# ---------------------------------------------------------------------------
# Reference adapter — AWX natural keys ⇄ canonical references.
#
# The canonical form is deliberately AWX-agnostic: a reference to a
# non-org-scoped type is a plain name string; a reference to an org-scoped type
# is ``{"name": …, "organization": …}`` (names only, no AWX ``type`` and no
# nested AWX dicts).  This adapter is the single place that knows AWX's
# natural-key shape.
# ---------------------------------------------------------------------------


def _split_name_org(value: Any) -> tuple[str | None, str | None]:
    """Extract ``(name, organization_name)`` from an AWX or canonical reference.

    Accepts a bare name string, an AWX natural-key dict (``{"name": …,
    "organization": {"name": …}, "type": …}``), or a canonical org-scoped
    reference (``{"name": …, "organization": …}``).  A raw integer ID or any
    other shape yields ``(None, None)`` — references are never IDs.
    """
    if isinstance(value, str):
        return value, None
    if isinstance(value, Mapping):
        name = value.get("name")
        name = name if isinstance(name, str) else None
        org = value.get("organization")
        if isinstance(org, Mapping):
            org = org.get("name")
        org = org if isinstance(org, str) else None
        return name, org
    return None, None


def _ref_to_canonical(value: Any, target: "ObjectType | None") -> Any:
    """Reduce one AWX reference to its canonical form.

    Args:
        value: The raw AWX reference value.
        target: The referenced object's :class:`ObjectType`, or ``None`` when
            unknown.

    Returns:
        A plain name for a reference to a non-org-scoped type, a
        ``{"name": …, "organization": …}`` mapping for an org-scoped one, or
        ``None`` when the reference cannot be resolved to a name.
    """
    name, org = _split_name_org(value)
    if name is None:
        return None
    if target is not None and target.org_scoped:
        return {"name": name, "organization": org}
    return name


def _ref_to_awx(value: Any, target: "ObjectType | None") -> Any:
    """Build one AWX-import natural key from a canonical reference.

    The inverse of :func:`_ref_to_canonical`.  Produces the natural-key dict
    ``awx import`` expects — always carrying ``type``, and nesting the
    organization for org-scoped targets — or ``None`` when the reference has no
    usable name (so the caller can omit the field).
    """
    name, org = _split_name_org(value)
    if name is None:
        return None
    type_name = target.awx_type_name if target is not None else None
    ref: dict[str, Any] = {"type": type_name, "name": name}
    if target is not None and target.org_scoped and org is not None:
        ref["organization"] = {"type": _ORGANIZATION_TYPE, "name": org}
    return ref


def _object_organization(obj: CanonicalObject) -> str | None:
    """Return an object's organization name for org-scoped filtering.

    Prefers the identity metadata (:attr:`CanonicalObject.natural_key`), which
    is populated even for objects that carry no top-level ``organization``
    field (e.g. job templates); falls back to the ``organization`` field.
    """
    if obj.natural_key is not None and "organization" in obj.natural_key:
        return obj.natural_key.get("organization")
    value = obj.fields.get("organization")
    if isinstance(value, Mapping):
        name = value.get("name")
        return name if isinstance(name, str) else None
    return value if isinstance(value, str) else None


def _natural_key_to_canonical(raw: Any) -> dict[str, Any] | None:
    """Reduce AWX's ``natural_key`` to AWX-agnostic identity metadata.

    Drops the ``type`` markers and flattens any nested natural key (e.g. the
    organization) down to its name, yielding e.g.
    ``{"name": "Deploy", "organization": "Default"}``.  Returns ``None`` when
    there is no usable natural key.
    """
    if not isinstance(raw, Mapping):
        return None
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        if key == "type":
            continue
        if isinstance(value, Mapping):
            name = value.get("name")
            clean[key] = name if isinstance(name, str) else None
        else:
            clean[key] = value
    return clean or None


# ---------------------------------------------------------------------------
# Recursive natural-key adapter — for compound references (e.g. credentials).
#
# A reference to a type whose natural key is exactly ``("name",)`` is a plain
# name string (matching the scalar-relation convention above); any richer
# natural key becomes a dict of its natural-key fields, recursing into nested
# references.  This generalises — and stays byte-compatible with — the
# organization/inventory/project convention, and additionally handles a
# credential's ``(name, credential_type, organization)`` key where
# ``credential_type`` itself carries ``(name, kind)``.
# ---------------------------------------------------------------------------


def _as_ref_mapping(value: Any) -> dict[str, Any] | None:
    """Coerce a reference *value* to a plain mapping, or ``None``.

    A bare name string is promoted to ``{"name": value}`` so single-field
    natural keys accept both shapes.
    """
    if isinstance(value, str):
        return {"name": value}
    if isinstance(value, Mapping):
        return dict(value)
    return None


def _ref_from_awx(
    value: Any,
    target: "ObjectType | None",
    object_types: Mapping[str, "ObjectType"],
) -> Any:
    """Reduce one AWX reference to its AWX-agnostic canonical form (recursive).

    Returns a bare name for a ``("name",)`` target, a dict of natural-key
    fields otherwise (nested references reduced in turn), or ``None`` when the
    reference cannot be resolved.
    """
    if value is None:
        return None
    raw = _as_ref_mapping(value)
    if raw is None:
        return None
    if target is None:
        name = raw.get("name")
        return name if isinstance(name, str) else None
    if tuple(target.natural_key) == ("name",):
        name = raw.get("name")
        return name if isinstance(name, str) else None

    relations = {rel.field: rel for rel in target.relations}
    result: dict[str, Any] = {}
    for field_name in target.natural_key:
        sub = raw.get(field_name)
        rel = relations.get(field_name)
        if rel is not None:
            result[field_name] = _ref_from_awx(
                sub, object_types.get(rel.target_type), object_types
            )
        else:
            result[field_name] = sub
    return result


def _ref_to_awx_nk(
    value: Any,
    target: "ObjectType | None",
    object_types: Mapping[str, "ObjectType"],
) -> Any:
    """Rebuild the AWX-import natural key from a canonical reference (recursive).

    The inverse of :func:`_ref_from_awx`.  Produces the fully typed, nested
    natural key ``awx import`` expects (each level carries ``type``), or
    ``None`` when the reference has no usable name.  A ``None`` sub-reference
    (e.g. an org-less credential's ``organization``) is preserved as ``None``,
    matching AWX's own export shape.
    """
    if value is None:
        return None
    raw = _as_ref_mapping(value)
    if raw is None:
        return None
    name = raw.get("name")
    if not isinstance(name, str):
        return None
    type_name = target.awx_type_name if target is not None else None
    if target is None or tuple(target.natural_key) == ("name",):
        return {"type": type_name, "name": name}

    relations = {rel.field: rel for rel in target.relations}
    ref: dict[str, Any] = {"type": type_name}
    for field_name in target.natural_key:
        sub = raw.get(field_name)
        rel = relations.get(field_name)
        if rel is not None:
            ref[field_name] = _ref_to_awx_nk(
                sub, object_types.get(rel.target_type), object_types
            )
        else:
            ref[field_name] = sub
    return ref


def _credential_identity(ref: Any) -> tuple[Any, Any, Any]:
    """Return a comparable identity for a credential reference.

    Normalises both the reduced dict form (``credential_type`` as a nested
    ``{"name", "kind"}`` dict) and a flattened form (``credential_type`` as a
    plain name) to ``(name, credential_type_name, organization_name)``.
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
                if _object_organization(obj) == organization
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
            # AWX keys the list by its asset-type name, which is usually the
            # registry key but singular for some types (e.g. "inventory").
            # Try the AWX key first, then the registry key as a fallback.
            items = []
            for candidate in (obj_type.awx_type, obj_type.key):
                if candidate in data:
                    items = data[candidate]
                    break
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

        Only whitelisted fields are copied; relations are reduced to their
        canonical reference form.  Any field not in the whitelist (``id``,
        ``url``, timestamps, ``summary_fields`` …) is dropped.  AWX's
        ``natural_key`` is captured separately as identity metadata, not as a
        business field.
        """
        relations = {rel.field: rel for rel in obj_type.relations}
        fields: dict[str, Any] = {}
        for field_name in obj_type.fields:
            if field_name not in raw:
                continue
            value = raw[field_name]
            rel = relations.get(field_name)
            if rel is not None:
                target = self._object_types.get(rel.target_type)
                if rel.many:
                    items = value if isinstance(value, (list, tuple)) else []
                    fields[field_name] = [
                        _ref_to_canonical(item, target) for item in items
                    ]
                else:
                    fields[field_name] = _ref_to_canonical(value, target)
            else:
                fields[field_name] = value

        # References and embedded documents sourced from AWX's ``related``
        # block.
        if obj_type.related_refs or obj_type.related_docs:
            related = raw.get("related")
            related = related if isinstance(related, Mapping) else {}
            # Embedded documents (e.g. survey_spec) are carried verbatim, only
            # when they hold content — an empty ``{}`` means "no survey".
            for rdoc in obj_type.related_docs:
                value = related.get(rdoc.awx_related_key)
                if isinstance(value, Mapping) and value:
                    fields[rdoc.canonical_field] = dict(value)
            for rref in obj_type.related_refs:
                target = self._object_types.get(rref.target_type)
                raw_items = related.get(rref.awx_related_key)
                items = raw_items if isinstance(raw_items, (list, tuple)) else []
                refs = [
                    _ref_from_awx(item, target, self._object_types)
                    for item in items
                ]
                refs = [r for r in refs if r is not None]
                # Store the field only when there are references — an object
                # with no credentials carries no ``credentials`` field, mirroring
                # the whitelist's "present fields only" behaviour.
                if refs:
                    fields[rref.canonical_field] = refs

        natural_key = _natural_key_to_canonical(raw.get("natural_key"))
        return CanonicalObject(
            type=obj_type.key, fields=fields, natural_key=natural_key
        )

    def _find_existing_job_template(
        self,
        canonical: CanonicalObject,
    ) -> dict[str, Any] | None:
        """Return the existing AWX job-template asset matching *canonical*.

        The AWX CLI exposes the credential associations in
        ``summary_fields.credentials``.  We need the actual resource ID so
        that credentials can be temporarily disassociated before an update.
        """

        if canonical.natural_key is None:
            return None

        name = canonical.natural_key.get("name")
        organization = canonical.natural_key.get("organization")

        if not isinstance(name, str):
            return None

        out = self._run(
            ["job_templates", "list", "-f", "json"]
        )

        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            raise AwxClientError(
                f"Could not parse 'awx job_templates list' output: {exc}"
            ) from exc

        if isinstance(data, Mapping):
            candidates = data.get("results", [])
        elif isinstance(data, list):
            candidates = data
        else:
            raise AwxClientError(
                "Unexpected 'awx job_templates list' output shape: "
                f"{type(data).__name__}"
            )

        for item in candidates:
            if not isinstance(item, Mapping):
                continue

            if item.get("name") != name:
                continue

            summary = item.get("summary_fields")
            if not isinstance(summary, Mapping):
                continue

            org = summary.get("organization")
            existing_org = (
                org.get("name")
                if isinstance(org, Mapping)
                else None
            )

            if organization is not None and existing_org != organization:
                continue

            return dict(item)

        return None

    def _job_template_credentials(
        self,
        asset: Mapping[str, Any],
    ) -> list[str]:
        """Return credential names currently associated with a job template."""

        summary = asset.get("summary_fields")
        if not isinstance(summary, Mapping):
            return []

        credentials = summary.get("credentials")
        if not isinstance(credentials, list):
            return []

        result: list[str] = []
        for credential in credentials:
            if not isinstance(credential, Mapping):
                continue

            name = credential.get("name")
            if isinstance(name, str) and name:
                result.append(name)

        return result

    def _credential_names_from_canonical(
        self,
        canonical: CanonicalObject,
    ) -> list[str]:
        """Return credential names from a canonical job-template object."""

        values = canonical.fields.get("credentials")
        if not isinstance(values, (list, tuple)):
            return []

        result: list[str] = []

        for value in values:
            if isinstance(value, str):
                result.append(value)
                continue

            if isinstance(value, Mapping):
                name = value.get("name")
                if isinstance(name, str) and name:
                    result.append(name)

        return result

    def _disassociate_job_template_credentials(
        self,
        job_template_id: int | str,
        credentials: Sequence[str],
    ) -> None:
        """Temporarily remove all credentials from a job template."""

        for credential in credentials:
            log.debug(
                "Disassociating credential %r from job template %r",
                credential,
                job_template_id,
            )
            self._run(
                [
                    "job_templates",
                    "disassociate",
                    "--credential",
                    credential,
                    str(job_template_id),
                ]
            )

    def _associate_job_template_credentials(
        self,
        job_template_id: int | str,
        credentials: Sequence[str],
    ) -> None:
        """Associate credentials with a job template."""

        for credential in credentials:
            log.debug(
                "Associating credential %r with job template %r",
                credential,
                job_template_id,
            )
            self._run(
                [
                    "job_templates",
                    "associate",
                    "--credential",
                    credential,
                    str(job_template_id),
                ]
            )


    def import_objects(
        self,
        object_type: str,
        objects: Sequence[CanonicalObject],
        *,
        on_conflict: str = "update",
    ) -> ImportResult:
        """Import canonical *objects* of *object_type* into AWX.

        Translates each canonical object into its native AWX asset form
        (whitelist fields only, references as natural keys, never IDs), wraps
        them in an ``awx import`` bundle (``{object_type: [assets]}``), and
        pipes that JSON to ``awx import``.

        Conflict handling: the *on_conflict* policy is validated and threaded
        through, but the actual skip/fail mechanism is deferred to a later
        phase.  ``update`` (the default) relies on ``awx import``'s native
        upsert.  For ``skip``/``fail`` a warning notes that enforcement is not
        yet active.  Per-object created/updated attribution is likewise
        deferred; a successful bulk import reports no per-object breakdown yet
        (the object count is tracked by the importer).

        Args:
            object_type: Registry key of the type being imported.
            objects: Canonical objects to import.
            on_conflict: One of ``"update"``, ``"skip"``, ``"fail"``.

        Returns:
            An :class:`ImportResult`.  A failed ``awx import`` is reported via
            :attr:`ImportResult.errors` rather than raised.

        Raises:
            ValueError: If *on_conflict* is not a known policy.
            AwxClientError: If *object_type* is unknown.
        """
        if on_conflict not in _CONFLICT_POLICIES:
            raise ValueError(f"Unknown on_conflict policy: {on_conflict!r}")

        obj_type = self._object_types.get(object_type)
        if obj_type is None:
            raise AwxClientError(f"Unknown object type: {object_type!r}")

        result = ImportResult()
        if not objects:
            return result

        # Job templates need special handling for credential associations.
        #
        # awx import treats related.credentials as additive associations.
        # AWX allows only one Machine credential per job template, therefore
        # importing a job template that already has a different Machine
        # credential fails with:
        #
        #   Cannot assign multiple Machine credentials.
        #
        # For existing job templates we temporarily remove the current
        # credential associations, import the object without credentials,
        # and restore the desired associations afterwards.
        existing_job_template: dict[str, Any] | None = None
        old_credentials: list[str] = []
        desired_credentials: list[str] = []
        credential_update = False

        if obj_type.key == "job_templates" and len(objects) == 1:
            canonical = objects[0]

            try:
                existing_job_template = self._find_existing_job_template(
                    canonical
                )
            except (AwxClientError, AwxCliError) as exc:
                log.debug(
                    "Could not determine existing job template: %s",
                    exc,
                )

            if existing_job_template is not None:
                old_credentials = self._job_template_credentials(
                    existing_job_template
                )
                desired_credentials = self._credential_names_from_canonical(
                    canonical
                )
                credential_update = True

                log.debug(
                    "Existing job template %r found (id=%s)",
                    canonical.natural_key.get("name")
                    if canonical.natural_key
                    else "?",
                    existing_job_template.get("id"),
                )
                log.debug(
                    "Existing credentials: %s",
                    old_credentials,
                )
                log.debug(
                    "Desired credentials: %s",
                    desired_credentials,
                )

                job_template_id = existing_job_template.get("id")
                if job_template_id is None:
                    raise AwxClientError(
                        "Existing job template has no usable ID"
                    )

                self._disassociate_job_template_credentials(
                    job_template_id,
                    old_credentials,
                )

        assets = [self._to_awx(obj_type, o) for o in objects]

        if credential_update:
            # Credentials are handled explicitly below.  Do not let
            # awx import perform an additive association.
            for asset in assets:
                related = asset.get("related")
                if isinstance(related, Mapping):
                    related = dict(related)
                    related.pop("credentials", None)
                    asset["related"] = related

        bundle = {
            obj_type.awx_type: assets
        }
        payload = json.dumps(bundle)

        try:
            self._run(["import"], stdin=payload)
        except AwxCliError as exc:
            # Try to restore the previous state if the actual import failed.
            if credential_update and existing_job_template is not None:
                job_template_id = existing_job_template.get("id")
                if job_template_id is not None:
                    try:
                        self._associate_job_template_credentials(
                            job_template_id,
                            old_credentials,
                        )
                    except AwxCliError as restore_exc:
                        log.error(
                            "Could not restore previous credentials for "
                            "job template %r: %s",
                            job_template_id,
                            restore_exc,
                        )

            result.errors.append(
                f"awx import failed for {obj_type.key!r}: {exc}"
            )
            return result

        # Re-attach the credentials requested by the imported object.
        if credential_update and existing_job_template is not None:
            job_template_id = existing_job_template.get("id")

            if job_template_id is None:
                result.errors.append(
                    "Job template was imported, but its ID could not be "
                    "determined for credential association"
                )
                return result

            try:
                self._associate_job_template_credentials(
                    job_template_id,
                    desired_credentials,
                )
            except AwxCliError as exc:
                result.errors.append(
                    "Job template was imported, but credential "
                    f"association failed: {exc}"
                )
                return result

        if on_conflict != "update":
            result.warnings.append(
                f"on_conflict={on_conflict!r} is not yet enforced; "
                "performed a standard import (update semantics)"
            )
        return result

    def _to_awx(
        self, obj_type: ObjectType, canonical: CanonicalObject
    ) -> dict[str, Any]:
        """Translate one canonical object into its native AWX asset form.

        Only whitelisted fields are written; relations are expanded to AWX
        natural keys (carrying ``type`` and, for org-scoped targets, a nested
        organization), never IDs.
        """
        relations = {rel.field: rel for rel in obj_type.relations}
        asset: dict[str, Any] = {}
        for field_name in obj_type.fields:
            if field_name not in canonical.fields:
                continue
            value = canonical.fields[field_name]
            rel = relations.get(field_name)
            if rel is not None:
                target = self._object_types.get(rel.target_type)
                if rel.many:
                    items = value if isinstance(value, (list, tuple)) else []
                    refs = [_ref_to_awx(item, target) for item in items]
                    asset[field_name] = [r for r in refs if r is not None]
                else:
                    reference = _ref_to_awx(value, target)
                    if reference is not None:
                        asset[field_name] = reference
            else:
                asset[field_name] = value

        # References and embedded documents go back into AWX's ``related``
        # block.  The block is always emitted as a dict when the type declares
        # any related member — awxkit's import iterates ``related`` and chokes
        # on a missing/None block ("argument of type 'NoneType' is not
        # iterable").
        if obj_type.related_refs or obj_type.related_docs:
            related_out: dict[str, Any] = {}
            for rref in obj_type.related_refs:
                target = self._object_types.get(rref.target_type)
                values = canonical.fields.get(rref.canonical_field)
                values = values if isinstance(values, (list, tuple)) else []
                refs = [
                    _ref_to_awx_nk(v, target, self._object_types)
                    for v in values
                ]
                related_out[rref.awx_related_key] = [
                    r for r in refs if r is not None
                ]
            # Embedded documents (e.g. survey_spec) are written back verbatim,
            # only when present on the object.
            for rdoc in obj_type.related_docs:
                value = canonical.fields.get(rdoc.canonical_field)
                if isinstance(value, Mapping) and value:
                    related_out[rdoc.awx_related_key] = dict(value)
            asset["related"] = related_out

        # awx import identifies each asset by its own natural key, so it must
        # be present (awxkit raises KeyError('natural_key') otherwise).
        natural_key = self._asset_natural_key(obj_type, canonical)
        if natural_key is not None:
            asset["natural_key"] = natural_key
        return asset

    def _asset_natural_key(
        self, obj_type: ObjectType, canonical: CanonicalObject
    ) -> dict[str, Any] | None:
        """Build the AWX ``natural_key`` for *canonical* as an import asset.

        Uses the object's identity metadata when present, otherwise derives it
        from the natural-key fields.  An object is its own natural-key
        reference, so the same reference adapter is reused.
        """
        if canonical.natural_key is not None:
            nk_value: Any = canonical.natural_key
        else:
            nk_value = {
                key: canonical.fields.get(key) for key in obj_type.natural_key
            }
        return _ref_to_awx(nk_value, obj_type)

    def exists(self, object_type: str, identity: tuple) -> bool:
        """Return whether an object with *identity* exists in AWX.

        Exports the type and checks whether any object's natural-key identity
        matches *identity*.  "Not found" and query failures both yield
        ``False`` — this method never raises for a missing object.

        Args:
            object_type: Registry key of the type to check.
            identity: Natural-key identity tuple
                (:meth:`CanonicalObject.identity`).

        Returns:
            ``True`` if a matching object exists, ``False`` otherwise.

        Raises:
            AwxClientError: Only if *object_type* is unknown (a misuse), never
                for a missing object.
        """
        obj_type = self._object_types.get(object_type)
        if obj_type is None:
            raise AwxClientError(f"Unknown object type: {object_type!r}")

        try:
            existing = self.export(object_type)
        except (AwxClientError, AwxCliError) as exc:
            log.debug(
                "exists(%s): could not query AWX, treating as not found: %s",
                object_type,
                exc,
            )
            return False

        target = tuple(identity)
        for obj in existing:
            try:
                if obj.identity(obj_type.natural_key) == target:
                    return True
            except KeyError:
                continue
        return False

    def missing_credentials(
        self, refs: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Return the subset of credential *refs* absent from the target AWX.

        Lists the target's credentials (reading only their natural keys — the
        transient query is never written to disk, so the no-secrets guarantee
        holds) and returns the references whose ``(name, credential_type,
        organization)`` identity is not present.  Any query failure yields an
        empty list so an import is never blocked by a transient read error.
        """
        wanted = [dict(ref) for ref in refs if isinstance(ref, Mapping)]
        if not wanted:
            return []
        try:
            existing_objs = self.export("credentials")
        except (AwxClientError, AwxCliError) as exc:
            log.warning(
                "could not list credentials for the pre-flight check; "
                "not stripping any references: %s",
                exc,
            )
            return []
        existing = {
            _credential_identity(obj.natural_key or {})
            for obj in existing_objs
        }
        return [
            ref
            for ref in wanted
            if _credential_identity(ref) not in existing
        ]


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
