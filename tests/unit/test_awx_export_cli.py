"""Unit tests for awx_export — the export CLI orchestrator (mocked)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import awx_export
from lib.awx_connection import AwxConnectionError
from lib.exporter import ExportError, ExportSummary


class _FakeClient:
    def __init__(self) -> None:
        self.orgs = ["Default", "Eng"]
        self.list_calls = 0

    def list_organizations(self) -> list[str]:
        self.list_calls += 1
        return self.orgs


class _RecordingExporter:
    def __init__(self, client, object_types, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.client = client
        self.object_types = list(object_types)
        self.kwargs = kwargs
        self.calls: list[tuple] = []

    def export_all(self, output_dir, *, organization=None) -> ExportSummary:  # type: ignore[no-untyped-def]
        self.calls.append(("export_all", Path(output_dir), organization))
        return ExportSummary(
            directory=Path(output_dir),
            counts={ot.key: 1 for ot in self.object_types},
        )

    def export_object(  # type: ignore[no-untyped-def]
        self, output_dir, object_type, name, *, organization=None
    ) -> ExportSummary:
        self.calls.append(
            ("export_object", Path(output_dir), object_type, name, organization)
        )
        return ExportSummary(
            directory=Path(output_dir), counts={object_type.key: 1}
        )


class _RecordingArchive:
    def __init__(self) -> None:
        self.created: list[tuple[Path, Path]] = []

    def create_archive(self, src, dst) -> None:  # type: ignore[no-untyped-def]
        self.created.append((Path(src), Path(dst)))

    def archive_size(self, path) -> int:  # type: ignore[no-untyped-def]
        return 12345


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Patch all external collaborators of awx_export and capture them."""
    state = SimpleNamespace(
        client=_FakeClient(),
        exporters=[],
        archives=[],
        connection_error=None,
        export_error=None,
    )

    def fake_build_connection(args):  # type: ignore[no-untyped-def]
        if state.connection_error is not None:
            raise state.connection_error
        kubectl = SimpleNamespace(namespace=getattr(args, "namespace", None))
        connection = SimpleNamespace(host="https://awx.example")
        return kubectl, connection, state.client

    monkeypatch.setattr(awx_export, "build_connection", fake_build_connection)

    def make_exporter(client, object_types, **kwargs):  # type: ignore[no-untyped-def]
        exporter = _RecordingExporter(client, object_types, **kwargs)
        if state.export_error is not None:
            def _raise(*a, **k):  # type: ignore[no-untyped-def]
                raise state.export_error
            exporter.export_all = _raise  # type: ignore[assignment]
            exporter.export_object = _raise  # type: ignore[assignment]
        state.exporters.append(exporter)
        return exporter

    monkeypatch.setattr(awx_export, "Exporter", make_exporter)

    def make_archive():  # type: ignore[no-untyped-def]
        archive = _RecordingArchive()
        state.archives.append(archive)
        return archive

    monkeypatch.setattr(awx_export, "Archive", make_archive)
    return state


# -- happy paths ------------------------------------------------------


def test_export_all_invokes_exporter(cli: SimpleNamespace) -> None:
    awx_export.main(["--all", "--output", "out"])
    assert len(cli.exporters) == 1
    exporter = cli.exporters[0]
    assert exporter.calls == [("export_all", Path("out"), None)]
    # All registry types were selected.
    assert {ot.key for ot in exporter.object_types} == {
        "organizations",
        "projects",
        "inventories",
        "job_templates",
    }


def test_export_type_selects_only_requested(cli: SimpleNamespace) -> None:
    awx_export.main(
        ["--type", "projects", "--type", "organizations", "--output", "out"]
    )
    exporter = cli.exporters[0]
    assert {ot.key for ot in exporter.object_types} == {
        "projects",
        "organizations",
    }
    assert exporter.calls[0][0] == "export_all"


def test_export_object_with_single_type(cli: SimpleNamespace) -> None:
    awx_export.main(
        ["--type", "job_templates", "--name", "Deploy", "--output", "out"]
    )
    exporter = cli.exporters[0]
    kind, out, obj_type, name, org = exporter.calls[0]
    assert kind == "export_object"
    assert out == Path("out")
    assert obj_type.key == "job_templates"
    assert name == "Deploy"
    assert org is None


def test_organization_is_forwarded(cli: SimpleNamespace) -> None:
    awx_export.main(["--all", "--organization", "Default", "--output", "out"])
    assert cli.exporters[0].calls == [
        ("export_all", Path("out"), "Default")
    ]


def test_default_output_dir_is_timestamped(cli: SimpleNamespace) -> None:
    awx_export.main(["--all"])
    _, out, _ = cli.exporters[0].calls[0]
    assert str(out).startswith("awx-export-")


# -- --organization ls ------------------------------------------------


def test_organization_ls_lists_and_stops(
    cli: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    awx_export.main(["--organization", "ls"])
    assert cli.client.list_calls == 1
    assert cli.exporters == []  # no export performed
    out = capsys.readouterr().out
    assert "Default" in out
    assert "Eng" in out


# -- archive ----------------------------------------------------------


def test_archive_is_created_when_requested(cli: SimpleNamespace) -> None:
    awx_export.main(["--all", "--archive", "--output", "export-dir"])
    assert len(cli.archives) == 1
    assert cli.archives[0].created == [
        (Path("export-dir"), Path("export-dir.tar.gz"))
    ]


def test_no_archive_by_default(cli: SimpleNamespace) -> None:
    awx_export.main(["--all", "--output", "out"])
    assert cli.archives == []


# -- validation & error handling --------------------------------------


def test_name_without_type_exits_1(cli: SimpleNamespace) -> None:
    with pytest.raises(SystemExit) as exc:
        awx_export.main(["--name", "Deploy"])
    assert exc.value.code == 1
    assert cli.exporters == []


def test_name_with_multiple_types_exits_1(cli: SimpleNamespace) -> None:
    with pytest.raises(SystemExit) as exc:
        awx_export.main(
            ["--type", "projects", "--type", "organizations", "--name", "X"]
        )
    assert exc.value.code == 1


def test_nothing_selected_exits_1(cli: SimpleNamespace) -> None:
    with pytest.raises(SystemExit) as exc:
        awx_export.main([])
    assert exc.value.code == 1


def test_invalid_type_choice_exits_2(cli: SimpleNamespace) -> None:
    # argparse rejects an unknown --type choice with exit code 2.
    with pytest.raises(SystemExit) as exc:
        awx_export.main(["--type", "bogus"])
    assert exc.value.code == 2


def test_connection_error_exits_1(cli: SimpleNamespace) -> None:
    cli.connection_error = AwxConnectionError("no host")
    with pytest.raises(SystemExit) as exc:
        awx_export.main(["--all"])
    assert exc.value.code == 1


def test_export_error_exits_1(cli: SimpleNamespace) -> None:
    cli.export_error = ExportError("disk full")
    with pytest.raises(SystemExit) as exc:
        awx_export.main(["--all", "--output", "out"])
    assert exc.value.code == 1
