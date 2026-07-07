#!/usr/bin/env python3
"""
AWX Backup Tool - Commit 001
Framework only.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys

VERSION = "0.1.0"
DEFAULT_NAMESPACE = "awx"

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s: %(message)s")
log = logging.getLogger("awx-backup")


class BackupError(RuntimeError):
    pass


def run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise BackupError(result.stderr.strip())
    return result.stdout.strip()


class Kubectl:

    def __init__(self, namespace: str = DEFAULT_NAMESPACE):
        self.namespace = namespace

    def run(self, args: list[str]) -> str:
        return run(["kubectl", "-n", self.namespace, *args])

    def pods(self):
        data = json.loads(self.run(["get", "pods", "-o", "json"]))
        return data["items"]


def parse_args():
    p = argparse.ArgumentParser(description="AWX backup")
    p.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {VERSION}")
    return p.parse_args()


def main():
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if shutil.which("kubectl") is None:
        raise BackupError("kubectl not found in PATH")

    k = Kubectl(args.namespace)
    pods = k.pods()

    log.info("AWX Backup %s", VERSION)
    log.info("Namespace : %s", args.namespace)
    log.info("Pods found: %d", len(pods))
    log.info("Framework test successful.")


if __name__ == "__main__":
    try:
        main()
    except BackupError as exc:
        log.error(str(exc))
        sys.exit(1)
