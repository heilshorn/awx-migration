#!/usr/bin/env python3
"""AWX Import — import individual AWX objects from a JSON bundle.

Thin orchestration layer and the counterpart to :mod:`awx_export`: it parses
arguments, sets up logging, builds the AWX connection via
:mod:`lib.cli_common`, drives the :class:`~lib.importer.Importer`, and prints a
summary.

All import logic lives in ``lib/``; this module contains none.  It never sees
AWX JSON or :class:`~lib.canonical.CanonicalObject` — those stay inside the
importer and the client.
"""

from __future__ import annotations

import argparse
import logging
import sys

from lib.cli_common import (
    COMMON_CLI_ERRORS,
    build_connection,
    list_organizations,
    print_import_summary,
)
from lib.awx_objects import OBJECT_TYPES
from lib.config import NAMESPACE
from lib.export_validator import ExportValidationError
from lib.importer import ImportError as ImporterError
from lib.importer import Importer
from lib.logger import setup_logger

VERSION = "2.0.0"

# Sentinel value for --organization that triggers "list organizations".
_ORG_LS = "ls"

# Conflict policies accepted by --on-conflict (passed through to the importer).
_CONFLICT_POLICIES = ("update", "skip", "fail")

log: logging.Logger = logging.getLogger("awx-migration")


class _ImportUsageError(RuntimeError):
    """Raised for invalid CLI option combinations (exit code 1)."""


# Domain errors handled with a clean message + exit code 1.  The common CLI
# errors are shared with the export CLI; ImportError and ExportValidationError
# are import-specific.
_DOMAIN_ERRORS = (
    *COMMON_CLI_ERRORS,
    ImporterError,
    ExportValidationError,
    _ImportUsageError,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse and return CLI arguments."""
    p = argparse.ArgumentParser(
        description=(
            "Import individual AWX objects (job templates, inventories, "
            "projects, …) from a versioned JSON bundle created by awx-export."
        ),
    )
    p.add_argument(
        "path",
        metavar="PATH",
        help="Export bundle directory to import",
    )
    p.add_argument(
        "--type",
        action="append",
        metavar="TYPE",
        choices=sorted(OBJECT_TYPES),
        help=(
            "Import only this object type (repeatable). "
            f"One of: {', '.join(sorted(OBJECT_TYPES))}"
        ),
    )
    p.add_argument(
        "--name",
        default=None,
        metavar="NAME",
        help="Import only this single object (requires exactly one --type)",
    )
    p.add_argument(
        "--organization",
        default=None,
        metavar="NAME",
        help=(
            "Restrict the import to this organization, or 'ls' to list all "
            "organizations and exit"
        ),
    )
    p.add_argument(
        "--on-conflict",
        choices=_CONFLICT_POLICIES,
        default="update",
        help="How to handle existing objects  (default: update)",
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


def _validate_selection(args: argparse.Namespace) -> None:
    """Validate the CLI option combination.

    Raises:
        _ImportUsageError: If ``--name`` is combined with anything other than
            exactly one ``--type``.
    """
    if args.name is not None and (not args.type or len(args.type) != 1):
        raise _ImportUsageError("--name requires exactly one --type")


def main(argv: list[str] | None = None) -> None:
    """Orchestrate the AWX object import workflow."""
    args = _parse_args(argv)
    setup_logger(verbose=args.verbose)

    log.info("AWX Import %s", VERSION)

    try:
        _kubectl, _connection, client = build_connection(args)

        # --organization ls: list organizations and stop.
        if args.organization == _ORG_LS:
            list_organizations(client)
            return

        _validate_selection(args)

        log.info("Bundle    : %s", args.path)
        summary = Importer(client).import_path(
            args.path,
            types=args.type,
            name=args.name,
            on_conflict=args.on_conflict,
        )
        print_import_summary(summary)

    except _DOMAIN_ERRORS as exc:
        log.error("Import failed: %s", exc)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - top-level safety net
        log.error("Unexpected error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
