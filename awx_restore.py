#!/usr/bin/env python3
"""AWX Restore — restores a complete AWX backup.

Coordinates archive extraction, manifest validation, checksum verification,
Secrets import, PostgreSQL restore, and AWX restart.  All domain logic lives
in lib/; this module is the thin orchestration layer only.

Safety guarantee: the existing database is never touched until the archive
has been extracted, the manifest validated, and all checksums verified.  A
corrupt or incomplete backup cannot destroy a running AWX installation.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import uuid
from pathlib import Path

from lib.config import DATABASE, DBUSER, NAMESPACE
from lib.logger import setup_logger
from lib import utils
from lib.kubectl import Kubectl, KubectlError
from lib.postgres import Postgres, PostgresError
from lib.secrets import Secrets, SecretError
from lib.archive import Archive, ArchiveError
from lib.manifest import Manifest, ManifestError
from lib.utils import MigrationError

VERSION = "0.1.0"

log: logging.Logger = logging.getLogger("awx-migration")


def _parse_args() -> argparse.Namespace:
    """Parse and return CLI arguments."""
    p = argparse.ArgumentParser(
        description=(
            "Restore a complete AWX backup "
            "(PostgreSQL restore + Kubernetes Secrets import + AWX restart)"
        ),
    )
    p.add_argument(
        "backup",
        metavar="BACKUP",
        help="Path to the .tar.gz backup archive",
    )
    p.add_argument(
        "--namespace",
        default=NAMESPACE,
        metavar="NS",
        help=f"Kubernetes namespace  (default: {NAMESPACE})",
    )
    p.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep the extracted backup directory after restoring",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Continue restore even if checksum verification fails",
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
    return p.parse_args()


def _log_restore_plan(manifest: Manifest, backup_file: Path, namespace: str) -> None:
    """Log a human-readable restore plan so the operator can verify intent.

    Args:
        manifest: Loaded and validated Manifest instance.
        backup_file: Path to the source archive.
        namespace: Target Kubernetes namespace.
    """
    created = manifest.get("backup", "created") or "unknown"
    awx_ver = manifest.get("awx", "version") or "unknown"
    database = manifest.get("postgres", "database") or "unknown"
    secret_count = manifest.get("secrets", "count") or 0
    archive_size = manifest.get("archive", "size")
    size_str = utils.human_size(int(archive_size)) if archive_size else "unknown"

    separator = "-" * 48
    log.info("Restore Plan")
    log.info("  Backup created : %s", created)
    log.info("  AWX Version    : %s", awx_ver)
    log.info("  Database       : %s", database)
    log.info("  Secrets        : %s", secret_count)
    log.info("  Namespace      : %s", namespace)
    log.info("  Archive        : %s (%s)", backup_file, size_str)
    log.info(separator)
    log.info("Starting restore...")


def _verify_checksums(
    arc: Archive,
    tmp_dir: Path,
    checksum_dict: dict[str, str],
    *,
    force: bool,
) -> None:
    """Verify archive checksums and abort or warn depending on *force*.

    Args:
        arc: Initialised Archive instance.
        tmp_dir: Extracted backup directory.
        checksum_dict: Expected checksums from the manifest.
        force: When True, log errors as warnings instead of raising.

    Raises:
        ArchiveError: If checksums fail and *force* is False.
    """
    log.info("Verifying checksums")
    errors = arc.verify_checksums(tmp_dir, checksum_dict)
    if errors:
        for err in errors:
            log.error("Checksum error: %s", err)
        if not force:
            raise ArchiveError(
                f"Checksum verification failed ({len(errors)} error(s)). "
                "Use --force to override."
            )
        log.warning(
            "Checksum verification failed (%d error(s)) — continuing due to --force",
            len(errors),
        )
    else:
        log.debug("All checksums verified successfully")


def _restart_awx(kubectl: Kubectl) -> None:
    """Restart AWX deployments after the database restore.

    Rolls out the AWX operator first so it can reconcile state, then
    restarts web and task deployments.

    Args:
        kubectl: Initialised Kubectl instance.
    """
    log.info("Restarting AWX")
    for deployment in (
        "awx-operator-controller-manager",
        "awx-web",
        "awx-task",
    ):
        try:
            kubectl.rollout_restart("deployment", deployment)
        except KubectlError as exc:
            log.warning("Could not restart deployment '%s': %s", deployment, exc)


def main() -> None:
    """Orchestrate the complete AWX restore workflow."""
    args = _parse_args()
    setup_logger(verbose=args.verbose)

    backup_file = Path(args.backup)
    restore_id = uuid.uuid4().hex[:8]
    tmp_dir = Path(f"tmp_restore_{restore_id}")

    log.info("AWX Restore %s", VERSION)
    log.info("Restore ID : %s", restore_id)
    log.info("Backup     : %s", backup_file)
    log.info("Namespace  : %s", args.namespace)

    try:
        # Step 1 — Extract archive
        log.info("Extracting archive")
        arc = Archive()
        arc.extract_archive(backup_file, tmp_dir)

        # Step 2 — Load manifest
        log.info("Loading manifest")
        mf = Manifest()
        mf.load(tmp_dir / "manifest.json")

        # Step 3 — Validate manifest (abort on error)
        log.info("Validating manifest")
        errors = mf.validate()
        if errors:
            for err in errors:
                log.error("Manifest error: %s", err)
            raise ManifestError(
                f"Manifest validation failed ({len(errors)} error(s))"
            )

        # Step 4 — Verify checksums (abort unless --force)
        checksum_dict: dict[str, str] = mf.get("checksums") or {}
        _verify_checksums(arc, tmp_dir, checksum_dict, force=args.force)

        # Step 5 — Log restore plan so operator can verify intent
        _log_restore_plan(mf, backup_file, args.namespace)

        # Step 6 — Initialise library objects
        kubectl = Kubectl(namespace=args.namespace)
        pg = Postgres(kubectl)
        sec = Secrets(kubectl)
        secrets_dir = tmp_dir / "secrets"

        # Step 7 — Log cluster context
        awx_ver = mf.get("awx", "version") or "unknown"
        log.info(
            "Target cluster  namespace=%s  backup AWX version=%s",
            args.namespace,
            awx_ver,
        )

        # Step 8 — Import secrets (awx-secret-key first, then the rest)
        log.info("Importing Secrets")
        sec.import_all(secrets_dir)

        # Step 9 — Restore PostgreSQL
        # The database is dropped only AFTER all preflight checks have passed.
        database = mf.get("postgres", "database") or DATABASE
        dump_file = tmp_dir / (mf.get("database", "filename") or "database.dump")
        log.info("Restoring PostgreSQL")
        pg.restore_database(database, DBUSER, dump_file)

        # Step 10 — Restart AWX deployments
        _restart_awx(kubectl)

        # Step 11 — Wait for AWX web pod
        log.info("Waiting for AWX")
        kubectl.wait_for_pod("app.kubernetes.io/name=awx-web")

        # Step 12 — Optionally wait for task pod (best-effort)
        try:
            kubectl.wait_for_pod("app.kubernetes.io/name=awx-task")
        except KubectlError as exc:
            log.warning("AWX task pod did not become Running: %s", exc)

        # Step 13 — Cleanup
        if not args.keep_temp:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            log.debug("Removed temporary directory '%s'", tmp_dir)
        else:
            log.info("Temporary directory kept: '%s'", tmp_dir)

        log.info("Restore completed successfully — ID: %s", restore_id)

    except (
        KubectlError,
        PostgresError,
        SecretError,
        ArchiveError,
        ManifestError,
        MigrationError,
    ) as exc:
        log.error("Restore failed: %s", exc)
        sys.exit(1)
    except Exception as exc:
        log.error("Unexpected error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
