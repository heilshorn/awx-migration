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
    ) -> str:
        """Execute *command* inside *pod* and return stripped stdout.

        Invalidates the pod cache if the error suggests the pod has gone away.

        Args:
            pod: PostgreSQL pod name.
            command: Command with arguments.
            timeout: Timeout in seconds.

        Returns:
            Stripped stdout string.

        Raises:
            PostgresError: On execution failure.
        """
        try:
            return self._kubectl.exec(pod, command, timeout=timeout)
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
    ) -> str:
        """Run *sql* via psql with ``-t -A`` and return raw stripped stdout.

        Suitable for queries that return a single scalar value.

        Args:
            pod: PostgreSQL pod name.
            database: Target database name.
            sql: SQL statement.
            user: PostgreSQL role. Defaults to :data:`DB_USER`.
            timeout: Timeout in seconds.

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
        return self._exec(pod, cmd, timeout=timeout)

    def _psql_rows(
        self,
        pod: str,
        database: str,
        sql: str,
        *,
        user: str = DB_USER,
        timeout: int = PSQL_TIMEOUT,
    ) -> list[dict[str, str]]:
        """Run *sql* via psql with ``--csv`` and return rows as dicts.

        Args:
            pod: PostgreSQL pod name.
            database: Target database name.
            sql: SQL SELECT statement.
            user: PostgreSQL role. Defaults to :data:`DB_USER`.
            timeout: Timeout in seconds.

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
        output = self._exec(pod, cmd, timeout=timeout)
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
        return self._psql(pod, "postgres", "SHOW server_version;")

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
        return self._psql(pod, "postgres", sql).strip() == "1"

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
        return self._psql(pod, "postgres", sql)

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
        raw = self._psql(pod, "postgres", sql, user=DB_ADMIN_USER)
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
            f'DROP DATABASE IF EXISTS "{database}";',
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
        rows = self._psql_rows(pod, database, sql)
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
        raw = self._psql(pod, database, sql)
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
        return self._psql_rows(pod, database, sql)

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
            self._exec(
                pod,
                [
                    "pg_dump", "-U", DB_USER, "-Fc",
                    "-d", database, "-f", remote,
                ],
                timeout=self.DUMP_TIMEOUT,
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

        Restore aborts on the first error (``--exit-on-error``).
        The target database must already exist.

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
                    "--clean", "--if-exists",
                    "--no-owner", "--no-privileges",
                    "--exit-on-error",
                    remote,
                ],
                timeout=self.DUMP_TIMEOUT,
            )
        finally:
            self._remove_pod_file(pod, remote)

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
