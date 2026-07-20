"""Shared CLI-level helpers for the export and import commands.

This module lives strictly on the CLI layer, above the exporter/importer.  It
factors out the small pieces that ``awx_export.py`` and ``awx_import.py`` would
otherwise duplicate: building the AWX connection, choosing a default output
directory, listing organizations, and printing result summaries.

It deliberately contains **no export or import logic** and knows nothing about
AWX JSON, ``CanonicalObject``, ``ObjectType``, or the export format.  Summaries
are consumed by duck typing so this module need not import the exporter or
importer.  It never calls :func:`sys.exit`; domain errors propagate to the
caller, which decides how to handle them.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from . import utils
from .awx_cli import AwxCliError
from .awx_client import AwxClient, AwxClientError, make_client
from .awx_connection import (
    AwxConnection,
    AwxConnectionError,
    resolve_connection,
)
from .config import NAMESPACE
from .kubectl import Kubectl, KubectlError

log: logging.Logger = logging.getLogger("awx-migration")

#: Domain errors common to both CLIs.  Export/import-specific errors
#: (ExportError/ImportError) are intentionally excluded — the callers handle
#: those differently.
COMMON_CLI_ERRORS: tuple[type[Exception], ...] = (
    KubectlError,
    AwxConnectionError,
    AwxCliError,
    AwxClientError,
)


def build_connection(
    args: Any,
) -> tuple[Kubectl, AwxConnection, AwxClient]:
    """Build the Kubectl wrapper, AWX connection, and client from *args*.

    Args:
        args: Parsed CLI arguments (namespace + connection options).  Read via
            :func:`getattr` so any namespace-like object works.

    Returns:
        ``(kubectl, connection, client)``.

    Raises:
        KubectlError, AwxConnectionError, AwxCliError, AwxClientError: On
            failure — propagated to the caller, never swallowed.
    """
    kubectl = Kubectl(namespace=getattr(args, "namespace", NAMESPACE))
    connection = resolve_connection(kubectl, args)
    client = make_client(connection)
    return kubectl, connection, client


def default_output_directory(prefix: str) -> Path:
    """Return a timestamped default output directory, e.g. ``prefix-<ts>``.

    Args:
        prefix: Directory-name prefix such as ``"awx-export"``.

    Returns:
        A :class:`~pathlib.Path` like ``awx-export-YYYYMMDD-HHMMSS``.
    """
    return Path(f"{prefix}-{utils.timestamp()}")


def list_organizations(client: AwxClient) -> list[str]:
    """Print the organizations known to *client* and return their names.

    Args:
        client: An AWX client (only :meth:`AwxClient.list_organizations` is
            used).

    Returns:
        The organization names, as returned by the client.

    Raises:
        AwxCliError, AwxClientError: Propagated from the client.
    """
    names = client.list_organizations()
    log.info("Organizations (%d):", len(names))
    for name in names:
        print(name)
    return names


def print_export_summary(summary: Any) -> None:
    """Log a human-readable export summary.

    Args:
        summary: Any object exposing ``counts`` (a mapping of type key to
            count) and ``directory``.
    """
    total = sum(summary.counts.values())
    types = ", ".join(sorted(summary.counts)) or "(none)"
    log.info("Export successful")
    log.info("  Types     : %s", types)
    log.info("  Objects   : %d", total)
    log.info("  Directory : %s", summary.directory)


def print_import_summary(summary: Any) -> None:
    """Log a human-readable import summary.

    Args:
        summary: Any object exposing ``imported_types``, ``object_count``,
            ``created``, ``updated``, ``skipped``, ``warnings``, ``errors``.
    """
    types = ", ".join(summary.imported_types) or "(none)"
    log.info("Import successful")
    log.info("  Types     : %s", types)
    log.info("  Objects   : %d", summary.object_count)
    log.info("  Created   : %d", len(summary.created))
    log.info("  Updated   : %d", len(summary.updated))
    log.info("  Skipped   : %d", len(summary.skipped))
    for warning in summary.warnings:
        log.warning("  %s", warning)
    for error in summary.errors:
        log.error("  %s", error)
