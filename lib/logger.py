"""Logging setup for awx-migration."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

LOGGER_NAME = "awx-migration"
_LOG_FORMAT = "%(levelname)s %(message)s"


def setup_logger(
    *,
    verbose: bool = False,
    logfile: Optional[str | Path] = None,
) -> logging.Logger:
    """Configure and return the awx-migration root logger.

    Installs a StreamHandler to stderr and, optionally, a FileHandler.
    Safe to call multiple times — existing handlers are removed first.

    Args:
        verbose: Set log level to DEBUG when True, INFO otherwise.
        logfile: Optional path to a log file. The file is created (including
                 parent directories) if it does not exist.

    Returns:
        Configured :class:`logging.Logger` instance.
    """
    logger = logging.getLogger(LOGGER_NAME)

    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()

    level = logging.DEBUG if verbose else logging.INFO
    logger.setLevel(level)

    formatter = logging.Formatter(_LOG_FORMAT)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(formatter)
    logger.addHandler(console)

    if logfile is not None:
        log_path = Path(logfile)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    logger.propagate = False
    return logger
