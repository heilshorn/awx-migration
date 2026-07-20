"""Unit tests for lib.cli_common — shared CLI helpers (mocked)."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib import cli_common
from lib.awx_connection import AwxConnectionError


# -- build_connection -------------------------------------------------


def test_build_connection_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    def fake_kubectl(namespace=None):  # type: ignore[no-untyped-def]
        calls["namespace"] = namespace
        return SimpleNamespace(namespace=namespace)

    def fake_resolve(kubectl, args):  # type: ignore[no-untyped-def]
        calls["resolve"] = (kubectl, args)
        return SimpleNamespace(host="https://awx.example")

    def fake_make_client(connection, *a, **k):  # type: ignore[no-untyped-def]
        calls["connection"] = connection
        return SimpleNamespace(kind="cli")

    monkeypatch.setattr(cli_common, "Kubectl", fake_kubectl)
    monkeypatch.setattr(cli_common, "resolve_connection", fake_resolve)
    monkeypatch.setattr(cli_common, "make_client", fake_make_client)

    args = SimpleNamespace(namespace="awx-prod")
    kubectl, connection, client = cli_common.build_connection(args)

    assert calls["namespace"] == "awx-prod"
    assert calls["resolve"] == (kubectl, args)
    assert calls["connection"] is connection
    assert client.kind == "cli"


def test_build_connection_defaults_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        cli_common,
        "Kubectl",
        lambda namespace=None: seen.setdefault("ns", namespace),
    )
    monkeypatch.setattr(
        cli_common, "resolve_connection", lambda k, a: SimpleNamespace()
    )
    monkeypatch.setattr(
        cli_common, "make_client", lambda c, *a, **k: SimpleNamespace()
    )

    cli_common.build_connection(SimpleNamespace())  # no 'namespace' attr
    from lib.config import NAMESPACE

    assert seen["ns"] == NAMESPACE


def test_build_connection_propagates_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_common, "Kubectl", lambda namespace=None: SimpleNamespace()
    )

    def boom(kubectl, args):  # type: ignore[no-untyped-def]
        raise AwxConnectionError("no host")

    monkeypatch.setattr(cli_common, "resolve_connection", boom)
    with pytest.raises(AwxConnectionError):
        cli_common.build_connection(SimpleNamespace(namespace="awx"))


# -- default_output_directory -----------------------------------------


def test_default_output_directory() -> None:
    result = cli_common.default_output_directory("awx-export")
    assert isinstance(result, Path)
    assert str(result).startswith("awx-export-")
    # prefix + '-' + a non-empty timestamp
    assert len(str(result)) > len("awx-export-")


def test_default_output_directory_prefix_varies() -> None:
    assert str(cli_common.default_output_directory("awx-import")).startswith(
        "awx-import-"
    )


# -- list_organizations -----------------------------------------------


def test_list_organizations_prints_and_returns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = SimpleNamespace(list_organizations=lambda: ["Default", "Eng"])
    result = cli_common.list_organizations(client)

    assert result == ["Default", "Eng"]
    out = capsys.readouterr().out
    assert "Default" in out
    assert "Eng" in out


# -- summaries --------------------------------------------------------


def test_print_export_summary(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="awx-migration")
    summary = SimpleNamespace(
        counts={"organizations": 2, "job_templates": 3},
        directory=Path("out-dir"),
    )
    cli_common.print_export_summary(summary)

    assert "Export successful" in caplog.text
    assert "out-dir" in caplog.text
    assert "5" in caplog.text  # 2 + 3 objects


def test_print_import_summary(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="awx-migration")
    summary = SimpleNamespace(
        created=["A"],
        updated=["B", "C"],
        skipped=[],
        warnings=["heads up"],
        errors=["bad"],
        object_count=3,
        imported_types=["organizations", "job_templates"],
    )
    cli_common.print_import_summary(summary)

    assert "Import successful" in caplog.text
    assert "heads up" in caplog.text
    assert "bad" in caplog.text
    assert "organizations, job_templates" in caplog.text


# -- error set --------------------------------------------------------


def test_common_cli_errors_membership() -> None:
    from lib.awx_cli import AwxCliError
    from lib.awx_client import AwxClientError
    from lib.awx_connection import AwxConnectionError as ConnErr
    from lib.kubectl import KubectlError

    assert set(cli_common.COMMON_CLI_ERRORS) == {
        KubectlError,
        ConnErr,
        AwxCliError,
        AwxClientError,
    }


def test_common_cli_errors_exclude_export_import_errors() -> None:
    from lib.exporter import ExportError
    from lib.importer import ImportError as ImporterError

    assert ExportError not in cli_common.COMMON_CLI_ERRORS
    assert ImporterError not in cli_common.COMMON_CLI_ERRORS
