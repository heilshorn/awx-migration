"""PostgreSQL abstraction layer for awx-migration.

All PostgreSQL operations are executed inside the Kubernetes PostgreSQL pod
via kubectl exec.  Backup and restore scripts must never call psql or pg_dump
directly — every access goes through this module.
"""

from __future__ import annotations

import csv
import io
import logging
from pathlib import Path

from .kubectl import Kubectl, KubectlError

try:
    from .config import DB_USER, DB_ADMIN_USER
except ImportError:
    DB_USER = "awx"
    DB_ADMIN_USER = "postgres"

log: logging.Logger = logging.getLogger("awx-migration")

# kubectl error substrings that indicate the pod is gone
_POD_GONE_MARKERS: tuple[str, ...] = (
    "not found",
    "does not exist",
    "no running pod",
)


class PostgresError(RuntimeError):
    """Raised on any PostgreSQL operation failure."""


class Postgres:
    """PostgreSQL operations executed inside the Kubernetes PostgreSQL pod.

    All commands are dispatched via the injected :class:`~lib.kubectl.Kubectl`
    instance.  No subprocess call to kubectl is made directly in this module.

    Pod caching:
        The resolved pod name is cached after the first successful lookup.
        If a subsequent command fails with an error indicating the pod is gone,
        the cache is invalidated so the next call resolves a fresh pod.

    User separation:
        Normal operations run as :data:`DB_USER`.
        Administrative operations (terminate, drop, create) run as
        :data:`DB_ADMIN_USER`.

    Streaming dumps:
        :meth:`pg_dump` writes the dump to a temporary file inside the pod
        and transfers it via ``kubectl cp``.  :meth:`pg_restore` does the
        reverse.  The dump data never passes through Python process memory.
    """

    PSQL_TIMEOUT: int = 60
    MAINT_TIMEOUT: int = 300
    DUMP_TIMEOUT: int = 3600

    #: Retry count for operations that are safe to repeat (read-only queries
    #: and idempotent commands such as ``pg_dump -f``).  Mutating commands run
    #: with the default of a single attempt and must not use this.
    SAFE_RETRIES: int = Kubectl.RETRY_COUNT

    def __init__(self, kubectl: Kubectl) -> None:
        """Initialise with a configured Kubectl instance.

        Args:
            kubectl: Kubectl wrapper bound to the target namespace.
        """
        self._kubectl = kubectl
        self._cached_pod: str | None = None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _pod(self) -> str:
        """Return the running PostgreSQL pod name, using the cache when valid.

        Raises:
            PostgresError: If no Running pod can be found.
        """
        if self._cached_pod is not None:
            return self._cached_pod
        try:
            name = self._kubectl.postgres_pod()
        except KubectlError as exc:
            raise PostgresError(
                f"Cannot locate PostgreSQL pod: {exc}"
            ) from exc
        self._cached_pod = name
        return name

    def _invalidate_pod_cache(self) -> None:
        """Clear the cached pod name so the next call resolves it afresh."""
        self._cached_pod = None

    def _exec(
        self,
        pod: str,
        command: list[str],
        *,
        timeout: int = PSQL_TIMEOUT,
        retries: int = 1,
    ) -> str:
        """Execute *command* inside *pod* and return stripped stdout.

        Invalidates the pod cache if the error suggests the pod has gone away.

        Args:
            pod: PostgreSQL pod name.
            command: Command with arguments.
            timeout: Timeout in seconds.
            retries: Maximum number of attempts. Defaults to ``1`` (no retry),
                the safe choice for mutating commands. Pass
                :data:`SAFE_RETRIES` for read-only or idempotent commands.

        Returns:
            Stripped stdout string.

        Raises:
            PostgresError: On execution failure.
        """
        try:
            return self._kubectl.exec(
                pod, command, timeout=timeout, retries=retries
            )
        except KubectlError as exc:
            if any(m in str(exc).lower() for m in _POD_GONE_MARKERS):
                self._invalidate_pod_cache()
            raise PostgresError(
                f"Command {command[0]!r} failed in pod '{pod}': {exc}"
            ) from exc

    def _psql(
        self,
        pod: str,
        database: str,
        sql: str,
        *,
        user: str = DB_USER,
        timeout: int = PSQL_TIMEOUT,
        retries: int = 1,
    ) -> str:
        """Run *sql* via psql with ``-t -A`` and return raw stripped stdout.

        Suitable for queries that return a single scalar value.

        Args:
            pod: PostgreSQL pod name.
            database: Target database name.
            sql: SQL statement.
            user: PostgreSQL role. Defaults to :data:`DB_USER`.
            timeout: Timeout in seconds.
            retries: Maximum attempts. Defaults to ``1``; read-only callers
                may pass :data:`SAFE_RETRIES`.

        Returns:
            Stripped psql output.

        Raises:
            PostgresError: On failure.
        """
        cmd = [
            "psql", "-U", user, "-d", database,
            "-t", "-A", "-c", sql,
        ]
        log.debug("psql -U %s -d %s -c %r", user, database, sql)
        return self._exec(pod, cmd, timeout=timeout, retries=retries)

    def _psql_rows(
        self,
        pod: str,
        database: str,
        sql: str,
        *,
        user: str = DB_USER,
        timeout: int = PSQL_TIMEOUT,
        retries: int = 1,
    ) -> list[dict[str, str]]:
        """Run *sql* via psql with ``--csv`` and return rows as dicts.

        Args:
            pod: PostgreSQL pod name.
            database: Target database name.
            sql: SQL SELECT statement.
            user: PostgreSQL role. Defaults to :data:`DB_USER`.
            timeout: Timeout in seconds.
            retries: Maximum attempts. Defaults to ``1``; read-only callers
                may pass :data:`SAFE_RETRIES`.

        Returns:
            List of row dicts keyed by column name.

        Raises:
            PostgresError: On execution or CSV parse failure.
        """
        cmd = [
            "psql", "-U", user, "-d", database,
            "--csv", "-c", sql,
        ]
        log.debug("psql -U %s --csv -d %s -c %r", user, database, sql)
        output = self._exec(pod, cmd, timeout=timeout, retries=retries)
        try:
            reader = csv.DictReader(io.StringIO(output))
            return [dict(row) for row in reader]
        except csv.Error as exc:
            raise PostgresError(
                f"Failed to parse psql CSV output: {exc}"
            ) from exc

    def _remove_pod_file(self, pod: str, remote_path: str) -> None:
        """Remove a temporary file from *pod*, suppressing errors."""
        try:
            self._kubectl.exec(pod, ["rm", "-f", remote_path], timeout=30)
        except KubectlError:
            log.warning(
                "Failed to remove temporary file '%s' from pod '%s'",
                remote_path,
                pod,
            )

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def version(self) -> str:
        """Return the PostgreSQL server version number.

        Returns:
            Version string, e.g. ``"15.12"``.

        Raises:
            PostgresError: On failure.
        """
        pod = self._pod()
        return self._psql(
            pod, "postgres", "SHOW server_version;", retries=self.SAFE_RETRIES
        )

    def database_exists(self, name: str) -> bool:
        """Return True if a database with *name* exists.

        Args:
            name: Database name to check.

        Returns:
            True when the database is present.

        Raises:
            PostgresError: On psql failure.
        """
        pod = self._pod()
        sql = f"SELECT 1 FROM pg_database WHERE datname = '{name}';"
        return self._psql(
            pod, "postgres", sql, retries=self.SAFE_RETRIES
        ).strip() == "1"

    def database_size(self, name: str) -> str:
        """Return the human-readable on-disk size of database *name*.

        Args:
            name: Database name.

        Returns:
            Size string as returned by ``pg_size_pretty``, e.g. ``"1234 MB"``.

        Raises:
            PostgresError: On psql failure.
        """
        pod = self._pod()
        sql = f"SELECT pg_size_pretty(pg_database_size('{name}'));"
        return self._psql(pod, "postgres", sql, retries=self.SAFE_RETRIES)

    def active_connections(self, database: str) -> int:
        """Return the number of active connections to *database*.

        Uses :data:`DB_ADMIN_USER` to ensure all sessions are visible in
        ``pg_stat_activity``.

        Args:
            database: Database name.

        Returns:
            Connection count.

        Raises:
            PostgresError: On psql failure or unexpected output.
        """
        pod = self._pod()
        sql = (
            f"SELECT count(*) FROM pg_stat_activity "
            f"WHERE datname = '{database}';"
        )
        raw = self._psql(
            pod, "postgres", sql,
            user=DB_ADMIN_USER, retries=self.SAFE_RETRIES,
        )
        try:
            return int(raw.strip())
        except ValueError as exc:
            raise PostgresError(
                f"Unexpected active_connections output: {raw!r}"
            ) from exc

    def terminate_connections(self, database: str) -> int:
        """Terminate all connections to *database* except the calling session.

        Args:
            database: Database name.

        Returns:
            Number of connections that were terminated.

        Raises:
            PostgresError: On psql failure or unexpected output.
        """
        pod = self._pod()
        sql = (
            f"SELECT count(pg_terminate_backend(pid)) "
            f"FROM pg_stat_activity "
            f"WHERE datname = '{database}' AND pid <> pg_backend_pid();"
        )
        raw = self._psql(pod, "postgres", sql, user=DB_ADMIN_USER)
        try:
            count = int(raw.strip())
        except ValueError as exc:
            raise PostgresError(
                f"Unexpected terminate_connections output: {raw!r}"
            ) from exc
        log.info("Terminated %d connection(s) to database '%s'", count, database)
        return count

    # ------------------------------------------------------------------
    # Database management
    # ------------------------------------------------------------------

    def drop_database(self, database: str) -> None:
        """Terminate all connections to *database* and then drop it.

        Automatically calls :meth:`terminate_connections` before dropping
        so the caller does not need to handle this explicitly.

        The drop uses ``WITH (FORCE)`` (PostgreSQL 13+), which atomically
        terminates any remaining backend still attached to the database and
        then drops it.  This guards against a stray session that reconnected
        in the brief window after :meth:`terminate_connections` — the AWX
        deployments should already be scaled to zero by the caller, but
        ``WITH (FORCE)`` is a cheap second line of defence.

        Args:
            database: Database name.

        Raises:
            PostgresError: On psql failure.
        """
        self.terminate_connections(database)
        log.info("Dropping database '%s'", database)
        pod = self._pod()
        self._psql(
            pod,
            "postgres",
            f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE);',
            user=DB_ADMIN_USER,
        )

    def create_database(self, database: str, owner: str) -> None:
        """Create *database* owned by *owner*.

        Args:
            database: Database name.
            owner: Owner role name.

        Raises:
            PostgresError: On psql failure.
        """
        log.info("Creating database '%s' (owner: '%s')", database, owner)
        pod = self._pod()
        self._psql(
            pod,
            "postgres",
            f'CREATE DATABASE "{database}" OWNER "{owner}";',
            user=DB_ADMIN_USER,
        )

    # ------------------------------------------------------------------
    # Table inspection
    # ------------------------------------------------------------------

    def list_tables(self, database: str) -> list[str]:
        """Return a sorted list of user table names in *database*.

        Args:
            database: Database name.

        Returns:
            List of table names from the ``public`` schema.

        Raises:
            PostgresError: On psql failure.
        """
        pod = self._pod()
        sql = (
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = 'public' ORDER BY tablename;"
        )
        rows = self._psql_rows(pod, database, sql, retries=self.SAFE_RETRIES)
        return [r["tablename"] for r in rows]

    def table_count(self, database: str) -> int:
        """Return the number of user tables in *database*.

        Args:
            database: Database name.

        Returns:
            Table count.

        Raises:
            PostgresError: On psql failure or unexpected output.
        """
        pod = self._pod()
        sql = (
            "SELECT count(*) FROM pg_tables WHERE schemaname = 'public';"
        )
        raw = self._psql(pod, database, sql, retries=self.SAFE_RETRIES)
        try:
            return int(raw.strip())
        except ValueError as exc:
            raise PostgresError(
                f"Unexpected table_count output: {raw!r}"
            ) from exc

    # ------------------------------------------------------------------
    # Generic query interface
    # ------------------------------------------------------------------

    def query(self, database: str, sql: str) -> list[dict[str, str]]:
        """Execute *sql* and return the full result set.

        Args:
            database: Target database name.
            sql: SQL statement (typically SELECT).

        Returns:
            List of row dicts keyed by column name.

        Raises:
            PostgresError: On execution or parse failure.
        """
        pod = self._pod()
        return self._psql_rows(pod, database, sql, retries=self.SAFE_RETRIES)

    def query_one(
        self, database: str, sql: str
    ) -> dict[str, str] | None:
        """Execute *sql* and return the first row, or None.

        Args:
            database: Target database name.
            sql: SQL statement expected to return at most one row.

        Returns:
            First row as a dict, or None if the result set is empty.

        Raises:
            PostgresError: On execution or parse failure.
        """
        rows = self.query(database, sql)
        return rows[0] if rows else None

    def execute(self, database: str, sql: str) -> None:
        """Execute *sql*, discarding any output (DDL, DML, …).

        Args:
            database: Target database name.
            sql: SQL statement to execute.

        Raises:
            PostgresError: On execution failure.
        """
        pod = self._pod()
        self._psql(pod, database, sql)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def vacuum(self, database: str) -> None:
        """Run VACUUM on *database*.

        Args:
            database: Database name.

        Raises:
            PostgresError: On psql failure.
        """
        log.info("Running VACUUM on database '%s'", database)
        pod = self._pod()
        self._exec(
            pod,
            ["psql", "-U", DB_USER, "-d", database, "-c", "VACUUM;"],
            timeout=self.MAINT_TIMEOUT,
        )

    def analyze(self, database: str) -> None:
        """Run ANALYZE on *database*.

        Args:
            database: Database name.

        Raises:
            PostgresError: On psql failure.
        """
        log.info("Running ANALYZE on database '%s'", database)
        pod = self._pod()
        self._exec(
            pod,
            ["psql", "-U", DB_USER, "-d", database, "-c", "ANALYZE;"],
            timeout=self.MAINT_TIMEOUT,
        )

    # ------------------------------------------------------------------
    # Backup and restore  (streaming via pod temp file + kubectl cp)
    # ------------------------------------------------------------------

    def pg_dump(self, database: str, outfile: str | Path) -> None:
        """Create a custom-format pg_dump of *database* to *outfile*.

        The dump is written to a temporary file inside the PostgreSQL pod and
        then transferred to the local filesystem via ``kubectl cp``.  The dump
        data never passes through Python process memory.

        Equivalent to: ``pg_dump -U <DB_USER> -Fc -d <database> -f <outfile>``

        Args:
            database: Source database name.
            outfile: Local destination path for the dump file.

        Raises:
            PostgresError: On pg_dump, transfer, or any intermediate failure.
        """
        remote = f"/tmp/awx_dump_{database}.dump"
        pod = self._pod()
        log.info("Creating PostgreSQL dump of database '%s'...", database)
        log.debug("pg_dump -U %s -Fc -d %s -f %s", DB_USER, database, remote)
        try:
            # pg_dump writes to a fresh temp file (-f overwrites), so a retry
            # after a transient failure is safe to repeat.
            self._exec(
                pod,
                [
                    "pg_dump", "-U", DB_USER, "-Fc",
                    "-d", database, "-f", remote,
                ],
                timeout=self.DUMP_TIMEOUT,
                retries=self.SAFE_RETRIES,
            )
            log.info("Transferring dump to '%s'...", outfile)
            try:
                self._kubectl.cp_from_pod(
                    pod, remote, outfile, timeout=self.DUMP_TIMEOUT
                )
            except KubectlError as exc:
                raise PostgresError(
                    f"Dump transfer from pod '{pod}' failed: {exc}"
                ) from exc
        finally:
            self._remove_pod_file(pod, remote)

    def pg_restore(self, database: str, infile: str | Path) -> None:
        """Restore a custom-format pg_dump backup into *database*.

        The dump file is transferred into the PostgreSQL pod via
        ``kubectl cp`` and then restored with ``pg_restore``.  The dump data
        never passes through Python process memory.

        The restore runs inside a single transaction (``--single-transaction``)
        so it is atomic: on any error the entire restore is rolled back,
        leaving the freshly-created database empty rather than partially
        populated.  This is critical because pg_restore is **not idempotent** —
        a partially-committed restore followed by a second attempt would
        duplicate rows (violating primary keys) and corrupt referential
        integrity.  For the same reason the command is executed with
        ``retries=1``; it must never be re-run against a partial result.

        ``--single-transaction`` implies ``--exit-on-error`` and aborts on the
        first failure.  ``--clean`` is intentionally **not** used: the caller
        (:meth:`restore_database`) drops and recreates the database first, so
        the target is already empty, and ``--clean`` would additionally fail on
        AWX's partitioned event tables (a partition's local index cannot be
        dropped while the parent partitioned index depends on it).

        The target database must already exist and be empty.

        Args:
            database: Target database name.
            infile: Local path to the dump file produced by :meth:`pg_dump`.

        Raises:
            PostgresError: On transfer, restore, or any intermediate failure.
        """
        remote = f"/tmp/awx_restore_{database}.dump"
        pod = self._pod()
        log.info("Restoring PostgreSQL database '%s'...", database)
        log.debug("pg_restore -U %s -d %s %s", DB_USER, database, infile)
        try:
            try:
                self._kubectl.cp_to_pod(
                    infile, pod, remote, timeout=self.DUMP_TIMEOUT
                )
            except KubectlError as exc:
                raise PostgresError(
                    f"Dump transfer to pod '{pod}' failed: {exc}"
                ) from exc
            self._exec(
                pod,
                [
                    "pg_restore", "-U", DB_USER,
                    "-d", database,
                    "--no-owner", "--no-privileges",
                    "--single-transaction",
                    remote,
                ],
                timeout=self.DUMP_TIMEOUT,
                retries=1,
            )
        finally:
            self._remove_pod_file(pod, remote)

    # ------------------------------------------------------------------
    # Role password synchronisation
    # ------------------------------------------------------------------

    def synchronize_role_password(
        self,
        username: str,
        password: str,
        *,
        database: str = "postgres",
        verify: bool = True,
    ) -> None:
        """Set the PostgreSQL role password and optionally verify the change.

        Executes ``ALTER ROLE <username> PASSWORD '<password>'`` as the
        superuser (:data:`DB_ADMIN_USER`), then — when *verify* is ``True`` —
        confirms the new password is accepted by connecting as *username*.

        This step is necessary after every ``pg_restore`` because
        ``pg_dump``/``pg_restore`` do not carry role passwords.  Without
        synchronisation the password stored in the Kubernetes Secret and the
        one accepted by PostgreSQL diverge, which prevents AWX from starting.

        Args:
            username: PostgreSQL role whose password should be updated.
            password: New plaintext password (read from the Kubernetes Secret,
                      never hard-coded).
            database: Database used for the ALTER ROLE statement and the
                      optional connectivity check.  Defaults to ``"postgres"``.
            verify: When ``True`` (default), attempt a ``SELECT 1`` as
                    *username* with the new password to confirm it works.
                    A failure is logged as an error and re-raised.

        Raises:
            PostgresError: If ``ALTER ROLE`` fails or if *verify* is ``True``
                           and the new password is rejected by PostgreSQL.
        """
        pod = self._pod()
        log.info(
            "Synchronising PostgreSQL role password for user '%s'", username
        )

        # ALTER ROLE does not support query parameters via psql; the password
        # is injected here.  It originates from the Kubernetes Secret (already
        # cluster-trusted data) and never touches user-controlled input, so
        # this is not an injection risk in the usual sense.  We still escape
        # single quotes to be safe against unusual secret values.
        escaped = password.replace("'", "''")
        alter_sql = f"ALTER ROLE \"{username}\" PASSWORD '{escaped}';"
        log.debug("ALTER ROLE \"%s\" PASSWORD '***'", username)

        self._psql(pod, database, alter_sql, user=DB_ADMIN_USER)
        log.info(
            "Password for PostgreSQL role '%s' updated successfully", username
        )

        if not verify:
            return

        log.info(
            "Verifying new password for PostgreSQL role '%s'", username
        )
        # Build a connection string with the new password and run a trivial
        # query.  PGPASSWORD is set in the subprocess environment via the
        # psql -w flag and the PGPASSWORD env-var passed through kubectl exec.
        # Because kubectl exec does not forward environment variables by default
        # we embed the password via a psql connection URI instead.
        escaped_uri = password.replace("@", "%40").replace(":", "%3A")
        uri = (
            f"postgresql://{username}:{escaped_uri}"
            f"@localhost/{database}"
        )
        verify_cmd = ["psql", uri, "-t", "-A", "-c", "SELECT 1;"]
        try:
            self._exec(pod, verify_cmd, timeout=self.PSQL_TIMEOUT)
        except PostgresError as exc:
            raise PostgresError(
                f"Password verification failed for role '{username}': "
                f"the new password was set but the connection was rejected. "
                f"Detail: {exc}"
            ) from exc

        log.info(
            "Password verification succeeded for PostgreSQL role '%s'",
            username,
        )

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    def backup_database(self, database: str, outfile: str | Path) -> None:
        """Create a full backup of *database* to *outfile*.

        Convenience wrapper around :meth:`pg_dump`.

        Args:
            database: Source database name.
            outfile: Local destination path for the dump file.

        Raises:
            PostgresError: On any failure during the backup.
        """
        log.info(
            "Starting backup of database '%s' to '%s'...", database, outfile
        )
        self.pg_dump(database, outfile)
        log.info("Backup of database '%s' complete", database)

    def restore_database(
        self,
        database: str,
        owner: str,
        infile: str | Path,
    ) -> None:
        """Perform a full restore of *database* from *infile*.

        Executes the following steps in order:

        1. :meth:`terminate_connections` — disconnect all existing sessions.
        2. :meth:`drop_database` — remove the database if it exists.
        3. :meth:`create_database` — create a fresh, empty database.
        4. :meth:`pg_restore` — load the dump into the new database.

        Args:
            database: Target database name.
            owner: Role that will own the restored database.
            infile: Local path to the dump file produced by
                    :meth:`pg_dump` or :meth:`backup_database`.

        Raises:
            PostgresError: On any failure during the restore sequence.
        """
        log.info(
            "Starting full restore of database '%s' from '%s'...",
            database,
            infile,
        )
        self.terminate_connections(database)
        self.drop_database(database)
        self.create_database(database, owner)
        self.pg_restore(database, infile)
        log.info("Restore of database '%s' complete", database)
