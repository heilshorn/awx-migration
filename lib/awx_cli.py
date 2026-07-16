"""Thin subprocess wrapper around the ``awx`` command-line binary.

Analogous to :class:`~lib.kubectl.Kubectl` and the registry tool wrapper: this
class locates the ``awx`` binary and runs it with retries, a timeout, optional
stdin, and an optional supplemental environment.  It returns raw stdout.

It contains **no AWX semantics** — it neither knows about object types nor
parses AWX output.  All AWX knowledge lives one layer up, in the client facade.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from typing import Optional

log: logging.Logger = logging.getLogger("awx-migration")

_BINARY_NAME: str = "awx"


class AwxCliError(RuntimeError):
    """Raised on any ``awx`` CLI invocation failure."""


class AwxCli:
    """Locates and runs the ``awx`` binary with retry and timeout handling."""

    DEFAULT_TIMEOUT: int = 120
    RETRY_COUNT: int = 3
    RETRY_DELAY: float = 2.0

    def __init__(self, binary: str | None = None) -> None:
        """Initialise the wrapper.

        Args:
            binary: Path to the ``awx`` binary.  When ``None``, it is located
                on ``PATH``.

        Raises:
            AwxCliError: If *binary* is ``None`` and no ``awx`` binary is found.
        """
        self._binary: str = binary if binary is not None else self._locate()

    @property
    def binary(self) -> str:
        """Return the resolved path to the ``awx`` binary."""
        return self._binary

    @staticmethod
    def _locate() -> str:
        found = shutil.which(_BINARY_NAME)
        if found is None:
            raise AwxCliError(
                f"'{_BINARY_NAME}' binary not found in PATH "
                "(install awxkit to enable export/import)."
            )
        return found

    @classmethod
    def detect(cls) -> "AwxCli":
        """Locate the ``awx`` binary on ``PATH`` and return a wrapper for it.

        Returns:
            A configured :class:`AwxCli` instance.

        Raises:
            AwxCliError: If no ``awx`` binary is found in ``PATH``.
        """
        return cls(cls._locate())

    def run(
        self,
        args: Sequence[str],
        *,
        env: Optional[Mapping[str, str]] = None,
        stdin: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
        retries: int = RETRY_COUNT,
    ) -> str:
        """Execute ``awx`` with *args* and return its stdout.

        Args:
            args: Arguments passed to the ``awx`` binary (without the binary
                itself), e.g. ``["organizations", "list", "-f", "json"]``.
            env: Supplemental environment variables merged over the current
                process environment for this call (e.g. AWX credentials).
            stdin: Optional text piped to the process stdin.
            timeout: Per-attempt timeout in seconds.
            retries: Maximum number of attempts before raising.

        Returns:
            Stripped stdout of the successful invocation.

        Raises:
            AwxCliError: After all retries are exhausted, or if the process
                cannot be launched.
        """
        cmd = [self._binary, *args]
        run_env = {**os.environ, **env} if env else None
        last_error: AwxCliError = AwxCliError("awx: no attempt made")

        for attempt in range(1, retries + 1):
            log.debug("awx [%d/%d]: %s", attempt, retries, " ".join(cmd))
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    input=stdin,
                    env=run_env,
                )
                if result.returncode == 0:
                    return result.stdout.strip()
                stderr = (result.stderr or "").strip()
                log.warning(
                    "awx failed (attempt %d/%d, rc=%d): %s",
                    attempt,
                    retries,
                    result.returncode,
                    stderr,
                )
                last_error = AwxCliError(
                    f"awx {' '.join(args[:2])} returned rc="
                    f"{result.returncode}: {stderr}"
                )
            except subprocess.TimeoutExpired:
                log.warning(
                    "awx timed out after %ds (attempt %d/%d)",
                    timeout,
                    attempt,
                    retries,
                )
                last_error = AwxCliError(
                    f"awx {' '.join(args[:2])} timed out after {timeout}s"
                )
            except OSError as exc:
                raise AwxCliError(f"Failed to launch awx: {exc}") from exc

            if attempt < retries:
                delay = self.RETRY_DELAY * attempt
                log.debug("Retrying awx in %.1fs …", delay)
                time.sleep(delay)

        raise last_error
