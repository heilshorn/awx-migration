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
from lib.k3s_registry import K3sRegistryError, K3sRegistryMirror
from lib.postgres import Postgres, PostgresError
from lib.registry_backup import RegistryError, RegistryRestore, RegistryTool
from lib.registry_rewrite import (
    RegistryRewrite,
    RegistryRewriteConfig,
    RegistryRewriteError,
)
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

    rw = p.add_argument_group(
        "registry rewrite (optional)",
        "Rewrite Execution Environment image registry prefixes after the "
        "database restore.  Both flags must be supplied together.",
    )
    rw.add_argument(
        "--registry-from",
        default=None,
        metavar="REGISTRY",
        help=(
            "Source registry prefix to replace, "
            "e.g. '10.6.207.31:30500'"
        ),
    )
    rw.add_argument(
        "--registry-to",
        default=None,
        metavar="REGISTRY",
        help=(
            "Replacement registry prefix, "
            "e.g. 'registry.example.local:30500'"
        ),
    )
    rw.add_argument(
        "--restore-registry",
        action="store_true",
        help=(
            "Restore the OCI/Docker registry from the backup (namespace, "
            "manifests, and images) before rewriting the Execution "
            "Environments. Requires --registry-from and --registry-to, a "
            "registry section in the backup, and 'skopeo' or 'crane'."
        ),
    )

    return p.parse_args()


def _build_registry_rewrite_config(
    args: argparse.Namespace,
) -> RegistryRewriteConfig | None:
    """Build a :class:`~lib.registry_rewrite.RegistryRewriteConfig` from CLI args.

    Returns ``None`` when neither ``--registry-from`` nor ``--registry-to``
    is supplied (feature disabled).  Raises if only one of the two is given.

    Args:
        args: Parsed :mod:`argparse` namespace.

    Returns:
        Configured :class:`RegistryRewriteConfig`, or ``None`` if the
        registry rewrite feature is not requested.

    Raises:
        RegistryRewriteError: If exactly one of the two flags is provided,
                               or if either value is empty after stripping.
    """
    has_from = bool(args.registry_from)
    has_to = bool(args.registry_to)

    if not has_from and not has_to:
        return None

    if has_from and not has_to:
        raise RegistryRewriteError(
            "--registry-from requires --registry-to to be set as well"
        )
    if has_to and not has_from:
        raise RegistryRewriteError(
            "--registry-to requires --registry-from to be set as well"
        )

    return RegistryRewriteConfig(
        source=args.registry_from,
        target=args.registry_to,
    )


def _restore_registry(
    manifest: Manifest,
    tmp_dir: Path,
    *,
    source: str | None,
    target: str | None,
) -> RegistryRewriteConfig:
    """Restore the registry recorded in the backup manifest.

    Reads the registry namespace from the manifest, detects an image tool, and
    delegates to :class:`~lib.registry_backup.RegistryRestore`.  The source and
    target registry addresses are derived automatically when not supplied:
    ``source`` from the backed-up image references, ``target`` from the
    restored NodePort service combined with a cluster node IP.

    Args:
        manifest: Loaded Manifest instance (must contain a ``registry`` section).
        tmp_dir: Extracted backup directory root.
        source: Optional ``--registry-from`` override (else auto-derived).
        target: Optional ``--registry-to`` override (else auto-derived).

    Returns:
        The effective :class:`RegistryRewriteConfig` used, for reuse by the
        Execution Environment rewrite.

    Raises:
        RegistryError: If the registry section is missing or the restore fails.
    """
    registry = manifest.get("registry")
    if not registry:
        raise RegistryError("Backup manifest has no registry section")
    namespace = registry["namespace"]
    tool = RegistryTool.detect()
    registry_kubectl = Kubectl(namespace=namespace)
    return RegistryRestore(registry_kubectl, tool).restore(
        tmp_dir / "registry", source=source, target=target
    )


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


# AWX deployments that hold PostgreSQL connections.  The operator is listed
# first so it can be stopped before web/task — otherwise it would immediately
# reconcile them back up while we are trying to scale them down.
_OPERATOR_DEPLOYMENT: str = "awx-operator-controller-manager"
_AWX_DEPLOYMENTS: tuple[str, ...] = (
    _OPERATOR_DEPLOYMENT,
    "awx-web",
    "awx-task",
)
_AWX_CLIENT_SELECTORS: tuple[str, ...] = (
    "app.kubernetes.io/name=awx-web",
    "app.kubernetes.io/name=awx-task",
)


def _quiesce_awx(kubectl: Kubectl) -> dict[str, int]:
    """Scale AWX deployments to zero so nothing holds a DB connection.

    A running AWX web/task pod reconnects to PostgreSQL within milliseconds of
    having its session terminated, which makes ``DROP DATABASE`` impossible.
    This captures each deployment's current replica count, scales the operator
    down first (so it cannot reconcile the others back up), then scales web and
    task to zero, and finally waits for the client pods to terminate.

    Every step is best-effort: failures are logged as warnings rather than
    raised, so a naming mismatch does not abort the restore (``DROP DATABASE
    ... WITH (FORCE)`` still provides a fallback).

    Args:
        kubectl: Initialised Kubectl instance.

    Returns:
        Mapping of deployment name to its original replica count, for use by
        :func:`_resume_awx`.
    """
    log.info("Scaling AWX down for the database restore")
    original: dict[str, int] = {}
    for name in _AWX_DEPLOYMENTS:
        try:
            original[name] = kubectl.get_replicas("deployment", name)
        except KubectlError as exc:
            log.warning(
                "Could not read replica count of '%s' (assuming 1): %s",
                name, exc,
            )
            original[name] = 1
    log.info("Captured replica counts: %s", original)

    for name in _AWX_DEPLOYMENTS:
        try:
            kubectl.scale("deployment", name, 0)
        except KubectlError as exc:
            log.warning("Could not scale down deployment '%s': %s", name, exc)

    for selector in _AWX_CLIENT_SELECTORS:
        try:
            kubectl.wait_until_gone(selector, timeout=180)
        except KubectlError as exc:
            log.warning(
                "Pods matching '%s' did not terminate in time: %s",
                selector, exc,
            )
    return original


def _resume_awx(kubectl: Kubectl, replicas: dict[str, int]) -> None:
    """Scale AWX deployments back to their captured replica counts.

    Web and task are restored first (best-effort — the operator owns their
    replica counts via ``task_manage_replicas`` and reconciles them once it is
    running), then the operator.  Scaling the *operator* back up is fatal on
    failure and re-raised: without a running operator nothing reconciles
    web/task, so the restore must not continue as if it had succeeded.

    Args:
        kubectl: Initialised Kubectl instance.
        replicas: Mapping returned by :func:`_quiesce_awx`.

    Raises:
        KubectlError: If the operator deployment cannot be scaled back up.
    """
    log.info("Scaling AWX back up: %s", replicas)
    # Web/task first (best-effort): the operator will reconcile them once up.
    for name in ("awx-web", "awx-task"):
        target = replicas.get(name, 1)
        try:
            kubectl.scale("deployment", name, target)
        except KubectlError as exc:
            log.warning(
                "Could not scale deployment '%s' back to %d: %s",
                name, target, exc,
            )
    # The operator is the control plane that reconciles web/task; a failure to
    # scale it back up must abort the restore, not vanish as a warning.
    op_target = replicas.get(_OPERATOR_DEPLOYMENT, 1)
    kubectl.scale("deployment", _OPERATOR_DEPLOYMENT, op_target)


def _resume_awx_after_failure(
    kubectl: Kubectl | None,
    replicas: dict[str, int] | None,
) -> None:
    """Best-effort resume of AWX after a failed restore.

    Only acts when AWX was actually scaled down (``replicas`` is not None and
    ``kubectl`` was initialised).  Bringing the deployments back returns the
    cluster to a managed state instead of leaving it silently at zero
    replicas; the pods may crash-loop against a half-restored database, but
    that is a visible, expected consequence of a failed restore.

    Args:
        kubectl: Initialised Kubectl instance, or None if the failure occurred
                 before initialisation.
        replicas: Captured replica counts, or None if AWX was never scaled
                  down.
    """
    if kubectl is None or replicas is None:
        return
    log.warning(
        "Restore failed after AWX was scaled down — "
        "attempting to restore replica counts."
    )
    try:
        _resume_awx(kubectl, replicas)
    except KubectlError as exc:
        # Already in the failure path: the now-fatal operator scale must not
        # mask the original error nor prevent the caller's sys.exit().
        log.warning("Best-effort resume after failure did not complete: %s", exc)


def _assert_replicas_positive(kubectl: Kubectl, name: str) -> None:
    """Raise unless a deployment is configured for at least one replica.

    A Deployment scaled to ``replicas: 0`` reports ``condition=Available=True``
    (minimum availability is trivially satisfied with no pods), so a bare
    readiness check would pass for a deployment that has no pods at all.  This
    guards against exactly the observed failure mode where ``awx-task`` stays
    at ``replicas: 0`` after the operator fails to reconcile it.

    Raises:
        KubectlError: If the deployment has fewer than one desired replica.
    """
    replicas = kubectl.get_replicas("deployment", name)
    if replicas < 1:
        raise KubectlError(
            f"Deployment '{name}' is scaled to {replicas} replica(s); "
            f"expected at least 1 — AWX is not fully restored."
        )


def _verify_deployment_ready(
    kubectl: Kubectl,
    name: str,
    selector: str,
    description: str,
    *,
    timeout: int = 300,
) -> None:
    """Confirm a managed AWX deployment has a Running, Ready pod.

    Reuses the existing wait helpers rather than adding a new health check:
      * :func:`_assert_replicas_positive` makes the ">0 replicas" expectation
        explicit and fails fast (not only via the pod-wait timeout).
      * ``wait_for_pod`` blocks until at least one *Running* pod exists — this
        fails via timeout if the deployment is stuck at ``replicas: 0``.
      * ``wait_for_deployment_available`` confirms the pod passed its readiness
        probe (``condition=Available``).

    Args:
        kubectl: Initialised Kubectl instance.
        name: Deployment name.
        selector: Label selector for the deployment's pods.
        description: Human-readable component name for log lines.
        timeout: Maximum wait per underlying step, in seconds.

    Raises:
        KubectlError: If the deployment is not Running/Ready within *timeout*.
    """
    log.info("Waiting for AWX %s...", description)
    _assert_replicas_positive(kubectl, name)
    kubectl.wait_for_pod(selector, timeout=timeout)
    kubectl.wait_for_deployment_available(name, timeout=timeout)
    log.info("AWX %s Ready", description)


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

    # Replica counts captured when AWX is scaled down; remains None until the
    # quiesce step so the error handler knows whether a resume is owed.
    original_replicas: dict[str, int] | None = None
    # Bound in Step 6; kept in the outer scope so the error handler can use it.
    kubectl: Kubectl | None = None
    # True once the database has been restored: lets the error handler
    # distinguish a pre-DB failure (exit 1) from a post-DB control-plane
    # failure where AWX did not fully recover (exit 2, degraded).
    db_restored = False

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

        # Validate registry-rewrite args early so a misconfiguration is
        # caught before any destructive operations are performed.
        rewrite_cfg: RegistryRewriteConfig | None
        if args.restore_registry:
            # With --restore-registry the source/target registry addresses are
            # derived automatically (source from the backup images, target from
            # the restored NodePort service). --registry-from / --registry-to
            # are optional overrides, so partial or absent values are allowed
            # here; the effective config is produced by the registry restore.
            rewrite_cfg = None
            if mf.get("registry") is None:
                raise RegistryError(
                    "--restore-registry was given but the backup contains no "
                    "registry section. Re-create the backup with "
                    "--registry-namespace."
                )
        else:
            rewrite_cfg = _build_registry_rewrite_config(args)

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

        # Step 9 — Quiesce AWX so no pod holds a PostgreSQL connection.
        # Without this, web/task pods reconnect within milliseconds of being
        # terminated and DROP DATABASE fails with "being accessed by other
        # users".  The database (a StatefulSet) is left running.
        original_replicas = _quiesce_awx(kubectl)

        # Step 10 — Restore PostgreSQL
        # The database is dropped only AFTER all preflight checks have passed
        # and AWX has been scaled down.
        database = mf.get("postgres", "database") or DATABASE
        dump_file = tmp_dir / (mf.get("database", "filename") or "database.dump")
        log.info("Restoring PostgreSQL")
        pg.restore_database(database, DBUSER, dump_file)

        # Step 10b — Synchronise PostgreSQL role password from Secret
        # pg_dump/pg_restore does not carry role passwords, so after a restore
        # the password accepted by PostgreSQL may differ from the one stored in
        # awx-postgres-configuration.  We read the password directly from the
        # already-imported Secret and apply it via ALTER ROLE.
        log.info("Synchronising PostgreSQL role password from Secret")
        pg_cfg = sec.postgres_config()
        pg.synchronize_role_password(
            username=pg_cfg["username"],
            password=pg_cfg["password"],
            database=pg_cfg["database"],
        )
        db_restored = True
        log.info("Database restore completed")

        # Step 11 — Optional registry restore, performed while AWX is still
        # scaled to zero and *before* AWX is scaled back up.  The restore is
        # independent of AWX (its own namespace) and derives the effective
        # source/target addresses (from the backup images and the restored
        # NodePort service), returning the config the EE rewrite reuses later.
        if args.restore_registry:
            log.info("Restoring registry")
            rewrite_cfg = _restore_registry(
                mf, tmp_dir,
                source=args.registry_from,
                target=args.registry_to,
            )

            # Step 11a — Ensure k3s/containerd can pull EE images from an HTTP
            # target registry: add the mirror + insecure-TLS entry to
            # /etc/rancher/k3s/registries.yaml (without overwriting existing
            # entries) and restart k3s if it changed.  Done before scaling AWX
            # up so web/task pods can pull their EE image on first start.
            # No-op for HTTPS registries.
            log.info("Ensuring k3s registry mirror for the target registry")
            K3sRegistryMirror(kubectl).ensure_mirror(rewrite_cfg.target)

        # Step 11b — Scale AWX back up to its captured replica counts.
        # original_replicas is intentionally NOT cleared here: the cluster is
        # only truly "resumed" once operator, web and task are verified Ready
        # (Step 12).  Clearing it now would stop _resume_awx_after_failure from
        # running if the verification below fails.
        _resume_awx(kubectl, original_replicas)

        # Step 11b-2 — Operator must be Running/Ready before web/task: with
        # task_manage_replicas=true it is the only actor that reconciles them
        # back to the CR replica count.  Uses the existing operator resolver
        # (selector + name-prefix fallback) rather than a hard-coded selector.
        log.info("Waiting for AWX operator...")
        _assert_replicas_positive(kubectl, _OPERATOR_DEPLOYMENT)
        kubectl.wait_for_deployment_available(_OPERATOR_DEPLOYMENT)
        kubectl.operator_pod()  # confirms a Running operator pod exists
        log.info("AWX operator Ready")

        # Step 11c — Web must be Running/Ready before the EE rewrite below
        # execs awx-manage inside the web pod.
        _verify_deployment_ready(
            kubectl, "awx-web", "app.kubernetes.io/name=awx-web", "web"
        )

        # Step 11d — Rewrite registry prefixes in Execution Environments
        # Runs after the web pod is Running so that awx-manage shell can
        # connect to the restored database using the pod's updated credentials.
        if rewrite_cfg is not None:
            log.info(
                "Rewriting Execution Environment registry prefixes"
                " ('%s' → '%s')",
                rewrite_cfg.source,
                rewrite_cfg.target,
            )
            rw = RegistryRewrite(kubectl, rewrite_cfg)
            rw.rewrite_execution_environments()
        else:
            log.debug("Registry rewrite not requested — skipping.")

        # Step 12 — Final consistency check: the task deployment must be
        # Running/Ready too.  A task deployment stuck at replicas:0 fails here
        # instead of being swallowed as a best-effort warning — without a task
        # pod AWX cannot execute any jobs.
        _verify_deployment_ready(
            kubectl, "awx-task", "app.kubernetes.io/name=awx-task", "task"
        )

        # Operator, web and task all verified Running/Ready — the cluster is
        # genuinely resumed, so only now is nothing owed to the failure handler.
        original_replicas = None

        # Step 13 — Cleanup
        if not args.keep_temp:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            log.debug("Removed temporary directory '%s'", tmp_dir)
        else:
            log.info("Temporary directory kept: '%s'", tmp_dir)

        log.info("AWX restore completed successfully — ID: %s", restore_id)
        log.info(
            "\n"
            "AWX has been started successfully.\n"
            "Depending on the environment, AWX may require a few additional "
            "minutes\n"
            "to complete internal initialization (scheduler, receptor, "
            "dispatcher).\n\n"
            "It is recommended to wait a short time before starting the first "
            "job."
        )

    except (
        KubectlError,
        PostgresError,
        RegistryError,
        RegistryRewriteError,
        K3sRegistryError,
        SecretError,
        ArchiveError,
        ManifestError,
        MigrationError,
    ) as exc:
        if db_restored:
            log.error(
                "Database restore completed, but AWX did not fully recover "
                "(control plane not Ready): %s",
                exc,
            )
            _resume_awx_after_failure(kubectl, original_replicas)
            sys.exit(2)
        log.error("Restore failed: %s", exc)
        _resume_awx_after_failure(kubectl, original_replicas)
        sys.exit(1)
    except Exception as exc:
        if db_restored:
            log.error(
                "Database restore completed, but AWX did not fully recover: %s",
                exc,
            )
            _resume_awx_after_failure(kubectl, original_replicas)
            sys.exit(2)
        log.error("Unexpected error: %s", exc)
        _resume_awx_after_failure(kubectl, original_replicas)
        sys.exit(1)


if __name__ == "__main__":
    main()
