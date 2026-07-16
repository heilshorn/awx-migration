"""AWX API connection resolution for export/import.

Derives how to reach the AWX API — host address and credentials — from CLI
arguments and, as a fallback, from the cluster via the existing
:class:`~lib.kubectl.Kubectl` layer.  This module reads from Kubernetes but
never modifies it, and it contains no ``awx`` CLI or REST semantics; it only
produces a plain :class:`AwxConnection` value object.

Credential priority (highest first):

1. ``--awx-token``
2. ``--awx-username`` / ``--awx-password``
3. The ``awx-admin-password`` Secret (username defaults to ``admin``)

Host priority (highest first):

1. ``--awx-host``
2. NodePort derivation from the AWX service plus a cluster node IP — the same
   approach the restore uses for the registry service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import NAMESPACE
from .kubectl import Kubectl, KubectlError

# AWX defaults.  Kept local to this module so lib/config.py stays untouched;
# they can be promoted to config later without changing this module's API.
AWX_ADMIN_SECRET: str = "awx-admin-password"
AWX_ADMIN_USER: str = "admin"
AWX_SERVICE: str = "awx-service"

# Secret data key holding the admin password.
_ADMIN_PASSWORD_FIELD: str = "password"


class AwxConnectionError(RuntimeError):
    """Raised when the AWX connection cannot be resolved."""


@dataclass(frozen=True)
class AwxConnection:
    """Resolved parameters for reaching the AWX API.

    Exactly one authentication method is populated: either :attr:`token`, or
    :attr:`username` + :attr:`password`.  ``password`` and ``token`` are hidden
    from :func:`repr` so they do not leak into logs.

    Attributes:
        host: Base URL of the AWX API, e.g. ``"https://10.0.0.5:30080"``.
        username: API username, or ``None`` when token auth is used.
        password: API password, or ``None`` when token auth is used.
        token: OAuth2 token, or ``None`` when user/password auth is used.
        verify_ssl: Whether TLS certificates should be verified.
    """

    host: str
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    token: str | None = field(default=None, repr=False)
    verify_ssl: bool = True


# ---------------------------------------------------------------------------
# Host derivation
# ---------------------------------------------------------------------------


def _first_node_port(service: dict[str, Any]) -> dict[str, Any] | None:
    """Return the first port entry of *service* that exposes a nodePort."""
    for port in service.get("spec", {}).get("ports", []):
        if port.get("nodePort"):
            return port
    return None


def _scheme_for(port_entry: dict[str, Any]) -> str:
    """Return ``"https"`` or ``"http"`` inferred from a service port entry."""
    name = str(port_entry.get("name") or "").lower()
    if port_entry.get("port") == 443 or "https" in name:
        return "https"
    return "http"


def _derive_host(kubectl: Kubectl, *, service_name: str = AWX_SERVICE) -> str:
    """Derive the AWX host URL from a NodePort service and a node IP.

    Prefers the service named *service_name*; falls back to any service in the
    namespace that exposes a nodePort.

    Args:
        kubectl: Kubectl wrapper bound to the AWX namespace.
        service_name: Preferred AWX service name.

    Returns:
        Base URL string, e.g. ``"http://10.0.0.5:30080"``.

    Raises:
        AwxConnectionError: If no NodePort service or node IP can be found.
    """
    try:
        services = kubectl.list_services()
    except KubectlError as exc:
        raise AwxConnectionError(
            f"Could not list services to derive the AWX host: {exc}"
        ) from exc

    service = next(
        (
            s
            for s in services
            if s.get("metadata", {}).get("name") == service_name
            and _first_node_port(s) is not None
        ),
        None,
    )
    if service is None:
        service = next(
            (s for s in services if _first_node_port(s) is not None), None
        )
    if service is None:
        raise AwxConnectionError(
            "Could not find an AWX NodePort service; pass --awx-host "
            "explicitly."
        )

    port_entry = _first_node_port(service)
    assert port_entry is not None  # guaranteed by the selection above
    node_port = port_entry["nodePort"]

    try:
        node_ip = kubectl.node_ip()
    except KubectlError as exc:
        raise AwxConnectionError(
            f"Could not determine a node IP for the AWX host: {exc}"
        ) from exc

    return f"{_scheme_for(port_entry)}://{node_ip}:{node_port}"


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def _read_admin_password(kubectl: Kubectl) -> str:
    """Read the AWX admin password from the ``awx-admin-password`` Secret.

    Args:
        kubectl: Kubectl wrapper bound to the AWX namespace.

    Returns:
        The decoded admin password.

    Raises:
        AwxConnectionError: If the Secret is missing or has no usable value.
    """
    try:
        data = kubectl.get_secret(AWX_ADMIN_SECRET)
    except KubectlError as exc:
        raise AwxConnectionError(
            f"Could not read Secret '{AWX_ADMIN_SECRET}': {exc}"
        ) from exc

    password = data.get(_ADMIN_PASSWORD_FIELD)
    if password is None and len(data) == 1:
        password = next(iter(data.values()))
    if not password:
        raise AwxConnectionError(
            f"Secret '{AWX_ADMIN_SECRET}' has no usable password "
            f"(looked for key '{_ADMIN_PASSWORD_FIELD}')."
        )
    return password


# ---------------------------------------------------------------------------
# Public resolver
# ---------------------------------------------------------------------------


def resolve_connection(kubectl: Kubectl, args: Any) -> AwxConnection:
    """Resolve an :class:`AwxConnection` from CLI *args* and the cluster.

    *args* is read via :func:`getattr` so any namespace-like object works
    (argparse namespace, ``SimpleNamespace``, …).  Recognised attributes:
    ``awx_host``, ``awx_username``, ``awx_password``, ``awx_token``,
    ``insecure``.

    Args:
        kubectl: Kubectl wrapper bound to the AWX namespace (used only for the
            NodePort host fallback and the admin-password Secret fallback).
        args: Object carrying the CLI options listed above.

    Returns:
        A fully populated :class:`AwxConnection`.

    Raises:
        AwxConnectionError: If the host or credentials cannot be resolved.
    """
    verify_ssl = not bool(getattr(args, "insecure", False))

    host = getattr(args, "awx_host", None)
    host = host.rstrip("/") if host else _derive_host(kubectl)

    token = getattr(args, "awx_token", None)
    if token:
        return AwxConnection(
            host=host,
            username=None,
            password=None,
            token=token,
            verify_ssl=verify_ssl,
        )

    username = getattr(args, "awx_username", None) or AWX_ADMIN_USER
    password = getattr(args, "awx_password", None)
    if not password:
        password = _read_admin_password(kubectl)

    return AwxConnection(
        host=host,
        username=username,
        password=password,
        token=None,
        verify_ssl=verify_ssl,
    )


# Namespace default is exposed for callers that build their own Kubectl.
DEFAULT_NAMESPACE: str = NAMESPACE
