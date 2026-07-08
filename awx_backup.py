#!/usr/bin/env python3
"""AWX Backup — orchestrates a complete AWX backup.

Coordinates PostgreSQL dump, Kubernetes Secrets export, manifest creation,
checksum calculation, and tar.gz archive packaging.  All domain logic lives
in lib/; this module is the thin orchestration layer only.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import socket
import sys
import uuid
from pathlib import Path

from lib.config import DATABASE, NAMESPACE
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
            "Create a complete AWX backup "
            "(PostgreSQL dump + Kubernetes Secrets + manifest + tar.gz archive)"
        ),
    )
    p.add_argument(
        "--namespace",
        default=NAMESPACE,
        metavar="NS",
        help=f"Kubernetes namespace  (default: {NAMESPACE})",
    )
    p.add_argument(
        "--output",
        default=None,
        metavar="FILE",
        help="Output .tar.gz filename  (default: awx-backup-YYYYMMDD-HHMMSS.tar.gz)",
    )
    p.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep the temporary backup directory after archiving",
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


def _get_awx_version(kubectl: Kubectl) -> str:
    """Determine the AWX application version with multiple fallbacks.

    Resolution order:
    1. ``kubectl get awx awx`` → ``.status.version``
    2. Label ``app.kubernetes.io/version`` on the awx-web pod
    3. ``"unknown"``

    Args:
        kubectl: Initialised Kubectl instance.

    Returns:
        AWX version string, or ``"unknown"`` when it cannot be determined.
    """
    try:
        version = kubectl.run(
            ["get", "awx", "awx", "-o", "jsonpath={.status.version}"],
            retries=1,
            timeout=10,
        )
        if version:
            log.debug("AWX version determined from CR status")
            return version
    except KubectlError:
        pass

    try:
        web_pod = kubectl.web_pod()
        version = kubectl.run(
            [
                "get", "pod", web_pod,
                "-o",
                "jsonpath={.metadata.labels.app\\.kubernetes\\.io/version}",
            ],
            retries=1,
            timeout=10,
        )
        if version:
            log.debug("AWX version determined from Pod label")
            return version
    except KubectlError:
        pass

    log.debug("AWX version could not be determined; using 'unknown'")
    return "unknown"


def _get_operator_version(kubectl: Kubectl) -> str:
    """Determine the AWX operator version with multiple fallbacks.

    Resolution order:
    1. Label ``app.kubernetes.io/operator-version`` on the awx-web pod
    2. Container image tag of the ``awx-operator-controller-manager`` deployment
    3. ``"unknown"``

    Args:
        kubectl: Initialised Kubectl instance.

    Returns:
        Operator version string, or ``"unknown"`` when it cannot be determined.
    """
    try:
        web_pod = kubectl.web_pod()
        version = kubectl.run(
            [
                "get", "pod", web_pod,
                "-o",
                "jsonpath={.metadata.labels.app\\.kubernetes\\.io/operator-version}",
            ],
            retries=1,
            timeout=10,
        )
        if version:
            log.debug("Operator version determined from Pod label")
            return version
    except KubectlError:
        pass

    try:
        image = kubectl.run(
            [
                "get", "deployment",
                "awx-operator-controller-manager",
                "-o",
                "jsonpath={.spec.template.spec.containers[0].image}",
            ],
            retries=1,
            timeout=10,
        )
        if image:
            version = image.split(":")[-1] if ":" in image else image
            if version:
                log.debug("Operator version determined from deployment image tag")
                return version
    except KubectlError:
        pass

    log.debug("Operator version could not be determined; using 'unknown'")
    return "unknown"


def _get_awx_versions(kubectl: Kubectl) -> tuple[str, str]:
    """Query the cluster for AWX and operator version strings.

    Both lookups are best-effort; failures result in ``"unknown"``.

    Args:
        kubectl: Initialised Kubectl instance.

    Returns:
        ``(awx_version, operator_version)`` — each defaults to
        ``"unknown"`` when it cannot be determined.
    """
    return _get_awx_version(kubectl), _get_operator_version(kubectl)


def main() -> None:
    """Orchestrate the complete AWX backup workflow."""
    args = _parse_args()
    setup_logger(verbose=args.verbose)

    ts = utils.timestamp()
    backup_id = f"{ts}-{uuid.uuid4().hex[:8]}"
    output_file = Path(args.output or f"awx-backup-{ts}.tar.gz")
    tmp_dir = Path(f"tmp_backup_{backup_id}")

    log.info("AWX Backup %s", VERSION)
    log.info("Backup ID: %s", backup_id)
    log.info("Namespace: %s", args.namespace)
    log.info("Output:    %s", output_file)

    try:
        # Prepare temp directory layout
        secrets_dir = tmp_dir / "secrets"
        utils.mkdir(tmp_dir)
        utils.mkdir(secrets_dir)

        # Initialise library objects
        kubectl = Kubectl(namespace=args.namespace)
        pg = Postgres(kubectl)
        sec = Secrets(kubectl)
        arc = Archive()
        mf = Manifest()

        # Step 1 — PostgreSQL dump
        dump_file = tmp_dir / "database.dump"
        log.info("Creating PostgreSQL dump...")
        pg.backup_database(DATABASE, dump_file)
        dump_sha256 = arc.sha256(dump_file)
        dump_size = dump_file.stat().st_size

        # Step 2 — Secrets export
        log.info("Exporting Secrets...")
        exported_secrets = sec.export_all(secrets_dir)

        # Step 3 — Cluster metadata (best-effort)
        pg_version = pg.version()
        db_size_row = pg.query_one(
            "postgres",
            f"SELECT pg_database_size('{DATABASE}') AS size",
        )
        db_bytes = int(db_size_row["size"]) if db_size_row else 0
        awx_version, op_version = _get_awx_versions(kubectl)

        # Step 4 — Build manifest (archive info intentionally omitted;
        #           see architecture notes in the module docstring)
        log.info("Creating Manifest...")
        mf.create()
        mf.set_tool(VERSION)
        mf.set_backup(hostname=socket.gethostname(), namespace=args.namespace)
        mf.set_awx(version=awx_version, operator_version=op_version)
        mf.set_postgres(
            version=pg_version,
            database=DATABASE,
            database_size=db_bytes,
        )
        mf.set_database(
            filename="database.dump",
            sha256=dump_sha256,
            size=dump_size,
        )
        mf.set_secrets(names=exported_secrets)

        # Step 5 — Checksums of data files (manifest.json not yet written,
        #           so there is no self-referential checksum entry)
        log.info("Calculating Checksums...")
        mf.add_checksums(arc.checksums(tmp_dir))
        mf.save(tmp_dir / "manifest.json")

        # Step 6 — Archive (single pass)
        log.info("Creating Archive...")
        arc.create_archive(tmp_dir, output_file)
        archive_bytes = arc.archive_size(output_file)
        ratio = arc.compression_ratio(tmp_dir, output_file)
        log.info(
            "Archive: %s  compression ratio: %.1fx",
            utils.human_size(archive_bytes),
            ratio,
        )

        # Step 7 — Cleanup
        if not args.keep_temp:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            log.debug("Removed temporary directory '%s'", tmp_dir)
        else:
            log.info("Temporary directory kept: '%s'", tmp_dir)

        log.info("Backup completed successfully — ID: %s", backup_id)

    except (
        KubectlError,
        PostgresError,
        SecretError,
        ArchiveError,
        ManifestError,
        MigrationError,
    ) as exc:
        log.error("Backup failed: %s", exc)
        sys.exit(1)
    except Exception as exc:
        log.error("Unexpected error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
