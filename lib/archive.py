"""Archive management for awx-migration backups.

Handles creation, extraction, integrity verification, and inspection of
AWX backup archives.  No Kubernetes or PostgreSQL logic — pure archive
operations only.
"""

from __future__ import annotations

import hashlib
import logging
import tarfile
from pathlib import Path

log: logging.Logger = logging.getLogger("awx-migration")


class ArchiveError(RuntimeError):
    """Raised on any archive operation failure."""


class Archive:
    """Creates, extracts, and verifies gzip-compressed tar backup archives.

    All I/O is streaming — no file is loaded completely into memory.
    Archives are deterministic: files are added in alphabetical order
    using relative POSIX paths, making repeated runs produce identical
    archives for identical input.
    """

    CHUNK_SIZE: int = 65536

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _iter_files(self, directory: Path) -> list[Path]:
        """Return regular, non-symlink files under *directory*, sorted alphabetically.

        Symlinks, device files, FIFOs, and sockets are excluded.

        Args:
            directory: Root directory to walk recursively.

        Returns:
            Sorted list of absolute :class:`~pathlib.Path` objects.
        """
        return sorted(
            p for p in directory.rglob("*")
            if p.is_file() and not p.is_symlink()
        )

    def _normalize_tarinfo(self, info: tarfile.TarInfo) -> tarfile.TarInfo:
        """Normalise a :class:`tarfile.TarInfo` entry for reproducible archives.

        Sets ``mtime``, ``uid``, ``gid``, ``uname``, ``gname``, and ``mode``
        to fixed values so that identical input data always produces an
        identical archive regardless of filesystem metadata.

        Args:
            info: TarInfo object to normalise (modified in place).

        Returns:
            The same *info* object with normalised fields.
        """
        info.mtime = 0
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.mode = 0o644 if info.isreg() else 0o755
        return info

    def _relative(self, path: Path, base: Path) -> str:
        """Return *path* as a relative POSIX string anchored at *base*.

        Args:
            path: File path to relativise.
            base: Base directory.

        Returns:
            Relative POSIX path string, e.g. ``"secrets/awx-secret-key.json"``.
        """
        return path.relative_to(base).as_posix()

    def _directory_size(self, directory: Path) -> int:
        """Return the total byte size of all files under *directory*.

        Args:
            directory: Root directory to measure.

        Returns:
            Sum of all file sizes in bytes.
        """
        return sum(p.stat().st_size for p in self._iter_files(directory))

    # ------------------------------------------------------------------
    # Create / extract
    # ------------------------------------------------------------------

    def create_archive(
        self,
        source_directory: str | Path,
        archive_file: str | Path,
    ) -> None:
        """Create a gzip-compressed tar archive from *source_directory*.

        Files are added in alphabetical order with relative paths so the
        archive is fully reproducible.  The source directory must already
        exist — this method does not create it.

        Args:
            source_directory: Root directory to archive.
            archive_file: Destination ``.tar.gz`` path. Parent directories
                          are created if absent.

        Raises:
            ArchiveError: If *source_directory* is missing or archiving fails.
        """
        src = Path(source_directory)
        dst = Path(archive_file)
        if not src.is_dir():
            raise ArchiveError(
                f"Source directory does not exist: '{src}'"
            )
        dst.parent.mkdir(parents=True, exist_ok=True)
        log.info("Creating archive '%s' from '%s'...", dst, src)
        files: list[Path] = []
        for path in sorted(p for p in src.rglob("*") if not p.is_dir()):
            if path.is_symlink():
                log.warning(
                    "Skipping symbolic link: '%s'", self._relative(path, src)
                )
            elif not path.is_file():
                log.warning(
                    "Skipping special file (device/FIFO/socket): '%s'",
                    self._relative(path, src),
                )
            else:
                files.append(path)
        try:
            with tarfile.open(dst, "w:gz") as tar:
                for path in files:
                    arcname = self._relative(path, src)
                    log.debug("Adding '%s'", arcname)
                    tar.add(
                        path,
                        arcname=arcname,
                        recursive=False,
                        filter=self._normalize_tarinfo,
                    )
        except (tarfile.TarError, OSError) as exc:
            raise ArchiveError(
                f"Failed to create archive '{dst}': {exc}"
            ) from exc
        log.info(
            "Archive created: '%s' (%d file(s), %d bytes)",
            dst,
            len(files),
            dst.stat().st_size,
        )

    def extract_archive(
        self,
        archive_file: str | Path,
        destination_directory: str | Path,
    ) -> None:
        """Extract a ``.tar.gz`` archive into *destination_directory*.

        All member paths are validated before extraction to prevent
        path-traversal attacks (zip-slip).

        Args:
            archive_file: Path to the ``.tar.gz`` file.
            destination_directory: Extraction target. Created if absent.

        Raises:
            ArchiveError: If the archive is missing, corrupt, or contains
                          unsafe member paths.
        """
        src = Path(archive_file)
        dst = Path(destination_directory).resolve()
        if not src.is_file():
            raise ArchiveError(
                f"Archive file does not exist: '{src}'"
            )
        dst.mkdir(parents=True, exist_ok=True)
        log.info("Extracting archive '%s' to '%s'...", src, dst)
        try:
            with tarfile.open(src, "r:gz") as tar:
                for member in tar.getmembers():
                    member_path = (dst / member.name).resolve()
                    if not member_path.is_relative_to(dst):
                        raise ArchiveError(
                            f"Unsafe path in archive member: '{member.name}'"
                        )
                    log.debug("Extracting '%s'", member.name)
                tar.extractall(dst)
        except tarfile.TarError as exc:
            raise ArchiveError(
                f"Failed to extract archive '{src}': {exc}"
            ) from exc
        except OSError as exc:
            raise ArchiveError(
                f"I/O error extracting archive '{src}': {exc}"
            ) from exc
        log.info("Archive extracted to '%s'", dst)

    # ------------------------------------------------------------------
    # Checksums
    # ------------------------------------------------------------------

    def sha256(self, filename: str | Path) -> str:
        """Compute the SHA-256 digest of *filename* using streaming reads.

        The file is processed in :attr:`CHUNK_SIZE`-byte blocks and is
        never fully loaded into memory.

        Args:
            filename: Path to the file.

        Returns:
            Lowercase hexadecimal SHA-256 digest string.

        Raises:
            ArchiveError: If the file cannot be read.
        """
        path = Path(filename)
        digest = hashlib.sha256()
        try:
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(self.CHUNK_SIZE), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise ArchiveError(
                f"Cannot read file for hashing '{path}': {exc}"
            ) from exc
        return digest.hexdigest()

    def checksums(self, directory: str | Path) -> dict[str, str]:
        """Compute SHA-256 digests for all files under *directory*.

        Files are processed in alphabetical order.  Paths in the returned
        dict are relative POSIX strings, making the result portable across
        operating systems.

        Args:
            directory: Root directory to scan recursively.

        Returns:
            Dict mapping each relative POSIX path to its hex SHA-256 digest.

        Raises:
            ArchiveError: If any file cannot be read.
        """
        base = Path(directory)
        log.info("Calculating checksums in '%s'...", base)
        result: dict[str, str] = {}
        for path in self._iter_files(base):
            rel = self._relative(path, base)
            log.debug("Hashing '%s'", rel)
            result[rel] = self.sha256(path)
        return result

    def verify_checksums(
        self,
        directory: str | Path,
        checksum_dict: dict[str, str],
    ) -> list[str]:
        """Verify files in *directory* against *checksum_dict*.

        Each entry in *checksum_dict* is verified by recomputing the
        SHA-256 digest of the corresponding file.  Files present in
        *directory* but absent from *checksum_dict* are not reported.

        Args:
            directory: Root directory to verify.
            checksum_dict: Mapping of relative POSIX path to expected hex
                           SHA-256 digest (as returned by :meth:`checksums`).

        Returns:
            List of error strings describing mismatches or missing files.
            An empty list means every checksum is correct.
        """
        base = Path(directory)
        log.info("Verifying checksums in '%s'...", base)
        errors: list[str] = []
        for rel, expected in sorted(checksum_dict.items()):
            path = base / rel
            if not path.is_file():
                errors.append(f"Missing file: '{rel}'")
                continue
            actual = self.sha256(path)
            if actual != expected:
                log.debug(
                    "Checksum mismatch for '%s': expected %s, got %s",
                    rel,
                    expected,
                    actual,
                )
                errors.append(
                    f"Checksum mismatch: '{rel}' "
                    f"(expected {expected[:12]}…, got {actual[:12]}…)"
                )
            else:
                log.debug("OK '%s'", rel)
        return errors

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def list_files(self, directory: str | Path) -> list[str]:
        """Return all files under *directory* as sorted relative POSIX paths.

        Args:
            directory: Root directory to scan recursively.

        Returns:
            Alphabetically sorted list of relative POSIX path strings.

        Raises:
            ArchiveError: If the directory cannot be read.
        """
        base = Path(directory)
        try:
            return [self._relative(p, base) for p in self._iter_files(base)]
        except OSError as exc:
            raise ArchiveError(
                f"Cannot list files in '{base}': {exc}"
            ) from exc

    def archive_size(self, archive_file: str | Path) -> int:
        """Return the size of *archive_file* in bytes.

        Args:
            archive_file: Path to the ``.tar.gz`` archive.

        Returns:
            File size in bytes.

        Raises:
            ArchiveError: If the file does not exist or cannot be stat'd.
        """
        path = Path(archive_file)
        try:
            return path.stat().st_size
        except OSError as exc:
            raise ArchiveError(
                f"Cannot stat archive '{path}': {exc}"
            ) from exc

    def compression_ratio(
        self,
        source_directory: str | Path,
        archive_file: str | Path,
    ) -> float:
        """Return the compression ratio of *archive_file* relative to *source_directory*.

        The ratio is defined as ``uncompressed_size / compressed_size``.
        A value of ``3.0`` means the original data is three times larger than
        the archive.

        Args:
            source_directory: Original source directory used to create the archive.
            archive_file: Resulting ``.tar.gz`` archive.

        Returns:
            Compression ratio as a float.  Returns ``0.0`` when the archive
            size is zero (degenerate case).

        Raises:
            ArchiveError: If sizes cannot be determined.
        """
        uncompressed = self._directory_size(Path(source_directory))
        compressed = self.archive_size(archive_file)
        if compressed == 0:
            return 0.0
        return uncompressed / compressed
