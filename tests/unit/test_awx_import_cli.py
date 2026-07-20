"""Unit tests for awx_import — the import CLI orchestrator (mocked)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import awx_import
from lib.export_validator import ExportValidationError
from lib.importer import ImportError as ImporterError
from lib.kubectl import KubectlError


class _FakeClient:
    def __init__(self) -> None:
        self.orgs = ["Default", "Eng"]


class _RecordingImporter:
    def __init__(self, client, summary, error=None) -> None:  # type: ignore[no-untyped-def]
        self.client = client
        self._summary = summary
        self._error = error
        self.calls: list[dict] = []

    def import_path(  # type: ignore[no-untyped-def]
        self, path, *, types=None, name=None, on_conflict="update"
    ):
        self.calls.append(
            {
                "path": path,
                "types": types,
                "name": name,
                "on_conflict": on_conflict,
            }
        )
        if self._error is not None:
            raise self._error
        return self._summary


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Patch awx_import's cli_common collaborators and capture them."""
    state = SimpleNamespace(
        client=_FakeClient(),
        summary=SimpleNamespace(name="import-summary"),
        build_calls=[],
        importers=[],
        printed=[],
        listed=[],
        build_error=None,
        import_error=None,
    )

    def fake_build(args):  # type: ignore[no-untyped-def]
        state.build_calls.append(args)
        if state.build_error is not None:
            raise state.build_error
        return SimpleNamespace(), SimpleNamespace(), state.client

    def make_importer(client, **kwargs):  # type: ignore[no-untyped-def]
        importer = _RecordingImporter(client, state.summary, state.import_error)
        state.importers.append(importer)
        return importer

    monkeypatch.setattr(awx_import, "build_connection", fake_build)
    monkeypatch.setattr(awx_import, "Importer", make_importer)
    monkeypatch.setattr(
        awx_import, "print_import_summary", lambda s: state.printed.append(s)
    )
    monkeypatch.setattr(
        awx_import, "list_organizations", lambda c: state.listed.append(c)
    )
    return state


# -- happy paths ------------------------------------------------------


def test_standard_import(cli: SimpleNamespace) -> None:
    awx_import.main(["bundle-dir"])

    assert cli.build_calls  # build_connection was used
    assert len(cli.importers) == 1
    assert cli.importers[0].calls == [
        {
            "path": "bundle-dir",
            "types": None,
            "name": None,
            "on_conflict": "update",
        }
    ]
    assert cli.printed == [cli.summary]  # print_import_summary was used


def test_type_filter_multiple(cli: SimpleNamespace) -> None:
    awx_import.main(
        ["bundle-dir", "--type", "projects", "--type", "organizations"]
    )
    call = cli.importers[0].calls[0]
    assert call["types"] == ["projects", "organizations"]


def test_name_with_single_type(cli: SimpleNamespace) -> None:
    awx_import.main(
        ["bundle-dir", "--type", "job_templates", "--name", "Deploy"]
    )
    call = cli.importers[0].calls[0]
    assert call["types"] == ["job_templates"]
    assert call["name"] == "Deploy"


def test_on_conflict_is_forwarded(cli: SimpleNamespace) -> None:
    awx_import.main(["bundle-dir", "--on-conflict", "skip"])
    assert cli.importers[0].calls[0]["on_conflict"] == "skip"


def test_default_on_conflict_is_update(cli: SimpleNamespace) -> None:
    awx_import.main(["bundle-dir"])
    assert cli.importers[0].calls[0]["on_conflict"] == "update"


# -- --organization ls ------------------------------------------------


def test_organization_ls_lists_and_stops(cli: SimpleNamespace) -> None:
    awx_import.main(["bundle-dir", "--organization", "ls"])
    assert cli.listed == [cli.client]  # list_organizations used
    assert cli.importers == []  # no import performed
    assert cli.printed == []


# -- validation & error handling --------------------------------------


def test_name_without_type_exits_1(cli: SimpleNamespace) -> None:
    with pytest.raises(SystemExit) as exc:
        awx_import.main(["bundle-dir", "--name", "Deploy"])
    assert exc.value.code == 1
    assert cli.importers == []  # never reached the importer


def test_name_with_multiple_types_exits_1(cli: SimpleNamespace) -> None:
    with pytest.raises(SystemExit) as exc:
        awx_import.main(
            [
                "bundle-dir",
                "--type",
                "projects",
                "--type",
                "organizations",
                "--name",
                "X",
            ]
        )
    assert exc.value.code == 1


def test_missing_path_exits_2(cli: SimpleNamespace) -> None:
    with pytest.raises(SystemExit) as exc:
        awx_import.main([])
    assert exc.value.code == 2  # argparse: required positional


def test_invalid_type_choice_exits_2(cli: SimpleNamespace) -> None:
    with pytest.raises(SystemExit) as exc:
        awx_import.main(["bundle-dir", "--type", "bogus"])
    assert exc.value.code == 2


def test_invalid_on_conflict_exits_2(cli: SimpleNamespace) -> None:
    with pytest.raises(SystemExit) as exc:
        awx_import.main(["bundle-dir", "--on-conflict", "bogus"])
    assert exc.value.code == 2


def test_import_error_exits_1(cli: SimpleNamespace) -> None:
    cli.import_error = ImporterError("invalid bundle")
    with pytest.raises(SystemExit) as exc:
        awx_import.main(["bundle-dir"])
    assert exc.value.code == 1


def test_validation_error_exits_1(cli: SimpleNamespace) -> None:
    cli.import_error = ExportValidationError("permission denied")
    with pytest.raises(SystemExit) as exc:
        awx_import.main(["bundle-dir"])
    assert exc.value.code == 1


def test_common_cli_error_exits_1(cli: SimpleNamespace) -> None:
    cli.build_error = KubectlError("no cluster")
    with pytest.raises(SystemExit) as exc:
        awx_import.main(["bundle-dir"])
    assert exc.value.code == 1
    assert cli.importers == []  # failed before importing
