"""Unit tests for lib.awx_connection — connection resolution (mocked)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from lib.awx_connection import (
    AWX_ADMIN_SECRET,
    AWX_ADMIN_USER,
    AwxConnection,
    AwxConnectionError,
    resolve_connection,
)
from lib.kubectl import KubectlError


class FakeKubectl:
    """Minimal Kubectl stand-in exposing only what the resolver needs."""

    def __init__(
        self,
        *,
        services: list[dict[str, Any]] | None = None,
        node_ip: str = "10.0.0.5",
        secret: dict[str, str] | None = None,
    ) -> None:
        self._services = services or []
        self._node_ip = node_ip
        self._secret = secret
        self.get_secret_calls: list[str] = []

    def list_services(self) -> list[dict[str, Any]]:
        return self._services

    def node_ip(self) -> str:
        return self._node_ip

    def get_secret(self, name: str) -> dict[str, str]:
        self.get_secret_calls.append(name)
        if self._secret is None:
            raise KubectlError(f"secret '{name}' not found")
        return self._secret


def _nodeport_service(
    name: str = "awx-service", *, node_port: int = 30080, port: int = 80
) -> dict[str, Any]:
    return {
        "metadata": {"name": name},
        "spec": {"ports": [{"name": "http", "port": port, "nodePort": node_port}]},
    }


def _args(**overrides: Any) -> SimpleNamespace:
    base = {
        "awx_host": None,
        "awx_username": None,
        "awx_password": None,
        "awx_token": None,
        "insecure": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# -- credential priority ----------------------------------------------


def test_token_has_highest_priority() -> None:
    kubectl = FakeKubectl(secret={"password": "from-secret"})
    args = _args(
        awx_host="https://awx.example",
        awx_token="tok-123",
        awx_username="someone",
        awx_password="pw",
    )
    conn = resolve_connection(kubectl, args)
    assert conn.token == "tok-123"
    assert conn.username is None
    assert conn.password is None
    assert kubectl.get_secret_calls == []  # secret not consulted


def test_username_password_over_secret() -> None:
    kubectl = FakeKubectl(secret={"password": "from-secret"})
    args = _args(
        awx_host="https://awx.example",
        awx_username="operator",
        awx_password="explicit",
    )
    conn = resolve_connection(kubectl, args)
    assert conn.username == "operator"
    assert conn.password == "explicit"
    assert conn.token is None
    assert kubectl.get_secret_calls == []  # secret not consulted


def test_secret_fallback_when_no_explicit_credentials() -> None:
    kubectl = FakeKubectl(secret={"password": "from-secret"})
    args = _args(awx_host="https://awx.example")
    conn = resolve_connection(kubectl, args)
    assert conn.username == AWX_ADMIN_USER
    assert conn.password == "from-secret"
    assert kubectl.get_secret_calls == [AWX_ADMIN_SECRET]


def test_secret_single_key_fallback() -> None:
    kubectl = FakeKubectl(secret={"admin_password": "single"})
    conn = resolve_connection(kubectl, _args(awx_host="https://awx.example"))
    assert conn.password == "single"


def test_missing_secret_raises() -> None:
    kubectl = FakeKubectl(secret=None)  # get_secret raises
    with pytest.raises(AwxConnectionError):
        resolve_connection(kubectl, _args(awx_host="https://awx.example"))


def test_username_defaults_to_admin_with_secret() -> None:
    kubectl = FakeKubectl(secret={"password": "pw"})
    conn = resolve_connection(kubectl, _args(awx_host="https://awx.example"))
    assert conn.username == AWX_ADMIN_USER


# -- host resolution --------------------------------------------------


def test_explicit_host_wins_and_is_stripped() -> None:
    kubectl = FakeKubectl(secret={"password": "pw"})
    conn = resolve_connection(
        kubectl, _args(awx_host="https://awx.example/")
    )
    assert conn.host == "https://awx.example"


def test_host_derived_from_nodeport() -> None:
    kubectl = FakeKubectl(
        services=[_nodeport_service(node_port=30080, port=80)],
        node_ip="10.0.0.5",
        secret={"password": "pw"},
    )
    conn = resolve_connection(kubectl, _args())
    assert conn.host == "http://10.0.0.5:30080"


def test_host_derivation_uses_https_for_443() -> None:
    kubectl = FakeKubectl(
        services=[_nodeport_service(node_port=30443, port=443)],
        secret={"password": "pw"},
    )
    conn = resolve_connection(kubectl, _args())
    assert conn.host.startswith("https://")


def test_host_derivation_falls_back_to_any_nodeport_service() -> None:
    kubectl = FakeKubectl(
        services=[_nodeport_service(name="other-svc", node_port=31000)],
        node_ip="10.0.0.9",
        secret={"password": "pw"},
    )
    conn = resolve_connection(kubectl, _args())
    assert conn.host == "http://10.0.0.9:31000"


def test_host_derivation_without_nodeport_raises() -> None:
    kubectl = FakeKubectl(services=[], secret={"password": "pw"})
    with pytest.raises(AwxConnectionError):
        resolve_connection(kubectl, _args())


# -- verify_ssl -------------------------------------------------------


def test_insecure_disables_verify_ssl() -> None:
    kubectl = FakeKubectl(secret={"password": "pw"})
    conn = resolve_connection(
        kubectl, _args(awx_host="https://awx.example", insecure=True)
    )
    assert conn.verify_ssl is False


def test_verify_ssl_defaults_true() -> None:
    kubectl = FakeKubectl(secret={"password": "pw"})
    conn = resolve_connection(kubectl, _args(awx_host="https://awx.example"))
    assert conn.verify_ssl is True


# -- value object -----------------------------------------------------


def test_password_and_token_hidden_in_repr() -> None:
    conn = AwxConnection(
        host="https://awx.example",
        username="admin",
        password="s3cret",
        token="tok",
    )
    text = repr(conn)
    assert "s3cret" not in text
    assert "tok" not in text
