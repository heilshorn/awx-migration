#!/usr/bin/env python3
"""AWX Export — export individual AWX objects to a JSON bundle.

Thin orchestration layer, mirroring :mod:`awx_backup` and :mod:`awx_restore`:
it parses arguments, sets up logging, resolves the AWX connection, builds the
client, drives the :class:`~lib.exporter.Exporter`, optionally packs the result
with the existing :class:`~lib.archive.Archive`, and prints a summary.

All export logic lives in ``lib/``; this module contains none.  Unlike
backup/restore, an export never contains secrets — it is for migration and
version control, not disaster recovery.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from lib.archive import Archive, ArchiveError
from lib.awx_objects import OBJECT_TYPES, ObjectType
from lib.cli_common import (
    COMMON_CLI_ERRORS,
    build_connection,
    default_output_directory,
    list_organizations,
    print_export_summary,
)
from lib.config import NAMESPACE
from lib.exporter import ExportError, Exporter, ExportSummary
from lib.logger import setup_logger
from lib import utils

VERSION = "2.0.0"

# Sentinel value for --organization that triggers "list organizations".
_ORG_LS = "ls"

# AWX version is not queried in this phase; recorded as this placeholder.
_AWX_VERSION_UNKNOWN = "unknown"

log: logging.Logger = logging.getLogger("awx-migration")


class _ExportUsageError(RuntimeError):
    """Raised for invalid CLI option combinations (exit code 1)."""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse and return CLI arguments."""
    p = argparse.ArgumentParser(
        description=(
            "Export individual AWX objects (job templates, inventories, "
            "projects, …) to a versioned JSON bundle. Contains no secrets."
        ),
    )
    p.add_argument(
        "--output",
        default=None,
        metavar="DIR",
        help="Output directory  (default: awx-export-YYYYMMDD-HHMMSS)",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Export all supported object types",
    )
    p.add_argument(
        "--type",
        action="append",
        metavar="TYPE",
        choices=sorted(OBJECT_TYPES),
        help=(
            "Object type to export (repeatable). "
            f"One of: {', '.join(sorted(OBJECT_TYPES))}"
        ),
    )
    p.add_argument(
        "--name",
        default=None,
        metavar="NAME",
        help="Export only this single object (requires exactly one --type)",
    )
    p.add_argument(
        "--organization",
        default=None,
        metavar="NAME",
        help=(
            "Restrict export to this organization, or 'ls' to list all "
            "organizations and exit"
        ),
    )
    p.add_argument(
        "--archive",
        action="store_true",
        help="Also pack the export directory into a .tar.gz archive",
    )
    p.add_argument(
        "--namespace",
        default=NAMESPACE,
        metavar="NS",
        help=f"Kubernetes namespace  (default: {NAMESPACE})",
    )
    p.add_argument(
        "--awx-host",
        default=None,
        metavar="URL",
        help="AWX API base URL  (default: derived from the NodePort service)",
    )
    p.add_argument(
        "--awx-token",
        default=None,
        metavar="TOKEN",
        help="AWX OAuth2 token (highest-priority credential)",
    )
    p.add_argument(
        "--awx-username",
        default=None,
        metavar="USER",
        help="AWX username  (default: admin)",
    )
    p.add_argument(
        "--awx-password",
        default=None,
        metavar="PASS",
        help="AWX password  (default: read from the awx-admin-password Secret)",
    )
    p.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging",
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    return p.parse_args(argv)


def _select_types(args: argparse.Namespace) -> list[ObjectType]:
    """Resolve the selected object types from ``--all`` / ``--type``."""
    if args.all:
        return list(OBJECT_TYPES.values())
    return [OBJECT_TYPES[name] for name in (args.type or [])]


def _validate_selection(
    args: argparse.Namespace, selected: list[ObjectType]
) -> None:
    """Validate the type selection against the requested operation.

    Raises:
        _ExportUsageError: If nothing is selected, or ``--name`` is combined
            with anything other than exactly one type.
    """
    if not selected:
        raise _ExportUsageError(
            "Nothing to export: pass --all or at least one --type"
        )
    if args.name and len(selected) != 1:
        raise _ExportUsageError("--name requires exactly one --type")


def _maybe_archive(
    args: argparse.Namespace, output_dir: Path
) -> tuple[Path, int] | None:
    """Pack *output_dir* into a .tar.gz when ``--archive`` is set.

    Returns:
        ``(archive_path, size_bytes)`` when an archive was created, else
        ``None``.
    """
    if not args.archive:
        return None
    archive = Archive()
    archive_file = Path(f"{output_dir}.tar.gz")
    archive.create_archive(output_dir, archive_file)
    return archive_file, archive.archive_size(archive_file)


def _print_summary(
    summary: ExportSummary, archive_info: tuple[Path, int] | None
) -> None:
    """Log the final export summary (shared summary plus the archive line)."""
    print_export_summary(summary)
    if archive_info is not None:
        archive_file, size = archive_info
        log.info("  Archive   : %s (%s)", archive_file, utils.human_size(size))


def main(argv: list[str] | None = None) -> None:
    """Orchestrate the AWX object export workflow."""
    args = _parse_args(argv)
    setup_logger(verbose=args.verbose)

    log.info("AWX Export %s", VERSION)

    try:
        _kubectl, _connection, client = build_connection(args)

        # --organization ls: list organizations and stop.
        if args.organization == _ORG_LS:
            list_organizations(client)
            return

        selected = _select_types(args)
        _validate_selection(args, selected)

        organization = args.organization
        output_dir = (
            Path(args.output)
            if args.output
            else default_output_directory("awx-export")
        )
        log.info("Output    : %s", output_dir)

        exporter = Exporter(
            client,
            selected,
            tool_version=VERSION,
            awx_version=_AWX_VERSION_UNKNOWN,
        )
        if args.name:
            summary = exporter.export_object(
                output_dir,
                selected[0],
                args.name,
                organization=organization,
            )
        else:
            summary = exporter.export_all(
                output_dir, organization=organization
            )

        archive_info = _maybe_archive(args, output_dir)
        _print_summary(summary, archive_info)

    except (
        *COMMON_CLI_ERRORS,
        ExportError,
        ArchiveError,
        _ExportUsageError,
    ) as exc:
        log.error("Export failed: %s", exc)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - top-level safety net
        log.error("Unexpected error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
