"""Unit tests for lib.awx_client — the AWX facade (mocked CLI)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from lib.awx_client import (
    AwxClient,
    AwxCliClient,
    AwxClientError,
    ImportResult,
    make_client,
)
from lib.awx_connection import AwxConnection


class FakeCli:
    """AwxCli stand-in that records calls and returns a preset output."""

    def __init__(self, output: str = "") -> None:
        self.output = output
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
        timeout: int = 120,
        retries: int = 3,
    ) -> str:
        self.calls.append({"args": list(args), "env": dict(env or {})})
        return self.output


def _conn(**overrides: Any) -> AwxConnection:
    base: dict[str, Any] = {
        "host": "https://awx.example",
        "username": "admin",
        "password": "pw",
        "token": None,
        "verify_ssl": True,
    }
    base.update(overrides)
    return AwxConnection(**base)


# -- environment creation --------------------------------------------


def test_env_user_password_variant() -> None:
    client = AwxCliClient(_conn(), cli=FakeCli())
    env = client._build_env()
    assert env["TOWER_HOST"] == "https://awx.example"
    assert env["CONTROLLER_HOST"] == "https://awx.example"
    assert env["TOWER_USERNAME"] == "admin"
    assert env["TOWER_PASSWORD"] == "pw"
    assert env["TOWER_VERIFY_SSL"] == "true"
    assert "TOWER_OAUTH_TOKEN" not in env


def test_env_token_variant() -> None:
    client = AwxCliClient(
        _conn(username=None, password=None, token="tok-9"), cli=FakeCli()
    )
    env = client._build_env()
    assert env["TOWER_OAUTH_TOKEN"] == "tok-9"
    assert env["CONTROLLER_OAUTH_TOKEN"] == "tok-9"
    assert "TOWER_USERNAME" not in env
    assert "TOWER_PASSWORD" not in env


def test_env_verify_ssl_false() -> None:
    client = AwxCliClient(_conn(verify_ssl=False), cli=FakeCli())
    env = client._build_env()
    assert env["TOWER_VERIFY_SSL"] == "false"
    assert env["CONTROLLER_VERIFY_SSL"] == "false"


# -- factory ----------------------------------------------------------


def test_make_client_returns_cli_client() -> None:
    client = make_client(_conn(), cli=FakeCli())
    assert isinstance(client, AwxCliClient)
    assert isinstance(client, AwxClient)


def test_make_client_unknown_kind_raises() -> None:
    with pytest.raises(ValueError):
        make_client(_conn(), kind="rest", cli=FakeCli())


# -- list_organizations ----------------------------------------------


def test_list_organizations_parses_results_and_sorts() -> None:
    cli = FakeCli('{"results": [{"name": "Eng"}, {"name": "Default"}]}')
    client = AwxCliClient(_conn(), cli=cli)
    assert client.list_organizations() == ["Default", "Eng"]
    # Correct command and credentials passed through.
    assert cli.calls[0]["args"] == ["organizations", "list", "-f", "json"]
    assert cli.calls[0]["env"]["TOWER_HOST"] == "https://awx.example"


def test_list_organizations_accepts_top_level_list() -> None:
    cli = FakeCli('[{"name": "A"}, {"name": "B"}]')
    client = AwxCliClient(_conn(), cli=cli)
    assert client.list_organizations() == ["A", "B"]


def test_list_organizations_ignores_entries_without_name() -> None:
    cli = FakeCli('{"results": [{"name": "A"}, {"id": 5}]}')
    client = AwxCliClient(_conn(), cli=cli)
    assert client.list_organizations() == ["A"]


def test_list_organizations_invalid_json_raises() -> None:
    client = AwxCliClient(_conn(), cli=FakeCli("not json"))
    with pytest.raises(AwxClientError):
        client.list_organizations()


# -- not-yet-implemented methods --------------------------------------


# -- ImportResult -----------------------------------------------------


def test_import_result_defaults_are_independent_lists() -> None:
    a = ImportResult()
    b = ImportResult()
    a.created.append("x")
    assert b.created == []
