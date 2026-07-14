"""k3s/containerd registry mirror configuration for awx-migration.

When an Execution Environment registry is restored as a plain-HTTP NodePort
registry, k3s/containerd cannot pull the EE images from it unless
``/etc/rancher/k3s/registries.yaml`` declares a *mirror* and an insecure-TLS
*config* for that registry host.  Without it the AWX pods fail with
``ImagePullBackOff`` on startup.

This module ensures such an entry exists — creating the file or supplementing
it without ever overwriting existing entries — and restarts k3s when (and only
when) it made a change.

The feature is only exercised for the ``--restore-registry`` flow with an HTTP
target registry (see :func:`target_uses_http`).  It runs *before* AWX is scaled
back up, so the pods can pull their EE image on first start.

Design constraints honoured here:
    * All Kubernetes access (node readiness) goes through the injected
      :class:`~lib.kubectl.Kubectl` instance — never a direct kubectl call.
    * The only subprocess call is ``systemctl restart k3s``, kept inside this
      library.
    * Parsing and re-serialising an operator-authored ``registries.yaml``
      requires a YAML parser; PyYAML is imported lazily so a missing
      dependency produces a clear, actionable error instead of an ImportError
      at module load.
"""

from __future__ import annotations

import logging
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .kubectl import Kubectl, KubectlError
from .utils import timestamp

log: logging.Logger = logging.getLogger("awx-migration")

#: Default location of the k3s registry configuration file.
REGISTRIES_PATH: Path = Path("/etc/rancher/k3s/registries.yaml")

#: systemctl restart of k3s can take a moment; give it a generous timeout.
_RESTART_TIMEOUT: int = 120

#: Probe timeout for the HTTP ``/v2/`` reachability check, in seconds.
_PROBE_TIMEOUT: int = 10


class K3sRegistryError(RuntimeError):
    """Raised on any k3s registry-mirror configuration failure."""


def _require_yaml() -> Any:
    """Import and return the PyYAML module, or raise a clear error.

    Returns:
        The imported ``yaml`` module.

    Raises:
        K3sRegistryError: If PyYAML is not installed.
    """
    try:
        import yaml  # noqa: PLC0415 - imported lazily so the dependency is
        #                              only required for the HTTP-registry path.
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise K3sRegistryError(
            "Configuring the k3s registry mirror requires PyYAML, but it is "
            "not installed. Install it (pip install PyYAML) or omit "
            "--restore-registry for an HTTP registry."
        ) from exc
    return yaml


def target_uses_http(address: str) -> bool:
    """Return True if the registry at *address* answers ``GET /v2/`` over HTTP.

    Probes ``http://<address>/v2/`` proxy-free (ignoring ``HTTP(S)_PROXY``
    environment variables, for the same reason as the registry restore: the
    address is a direct NodePort that must not be routed through a corporate
    proxy).  HTTP 200 and 401 both indicate a live registry served over plain
    HTTP.  Never raises — any failure yields ``False``.

    Args:
        address: Registry ``host:port``.

    Returns:
        True when the registry serves plain HTTP, False otherwise (including
        HTTPS-only registries and unreachable addresses).
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"http://{address}/v2/"
    try:
        req = urllib.request.Request(url, method="GET")
        with opener.open(req, timeout=_PROBE_TIMEOUT) as resp:
            return resp.status in (200, 401)
    except urllib.error.HTTPError as exc:
        return exc.code in (200, 401)
    except (urllib.error.URLError, OSError) as exc:
        log.debug("HTTP probe of %s failed: %s", url, exc)
        return False


class K3sRegistryMirror:
    """Ensures ``registries.yaml`` has an insecure HTTP mirror for a registry.

    Node readiness after a k3s restart is checked through the injected
    :class:`~lib.kubectl.Kubectl` instance; the mirror file and the k3s restart
    are handled directly on the host.
    """

    def __init__(self, kubectl: Kubectl, path: Path = REGISTRIES_PATH) -> None:
        """Initialise with a Kubectl instance and the registries.yaml path.

        Args:
            kubectl: Kubectl wrapper (used only for the node-readiness wait;
                node operations are cluster-scoped, so its namespace is
                irrelevant).
            path: Location of the k3s registry configuration file. Defaults to
                :data:`REGISTRIES_PATH`.
        """
        self._kubectl = kubectl
        self._path = Path(path)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def ensure_mirror(self, address: str) -> bool:
        """Ensure k3s has an insecure HTTP mirror for the registry *address*.

        No-op (returns ``False``) when the target registry does not serve plain
        HTTP.  Otherwise loads ``registries.yaml``, merges in the mirror and
        insecure-TLS config without overwriting any existing entry, and — only
        if that changed the file — backs up the original, writes the new file,
        restarts k3s, and waits for the node to become ``Ready`` again.

        Args:
            address: Target registry ``host:port`` (e.g. from the restored
                NodePort service).

        Returns:
            True if the file was changed and k3s restarted, False otherwise.

        Raises:
            K3sRegistryError: On any file, YAML, systemctl, or node-readiness
                failure.
        """
        if not target_uses_http(address):
            log.info(
                "Target registry '%s' does not serve plain HTTP — leaving "
                "%s untouched.",
                address, self._path,
            )
            return False

        yaml = _require_yaml()
        data = self._load(yaml)
        endpoint = f"http://{address}"
        changed = self._merge(data, address, endpoint)

        if not changed:
            log.info(
                "k3s registry mirror for '%s' already present in %s — no "
                "change needed.",
                address, self._path,
            )
            return False

        log.info(
            "Adding k3s registry mirror for '%s' to %s", address, self._path
        )
        self._backup_existing()
        self._write(yaml, data)
        self._restart_k3s()
        self._wait_node_ready()
        log.info("k3s registry mirror for '%s' is active.", address)
        return True

    # ------------------------------------------------------------------
    # File handling
    # ------------------------------------------------------------------

    def _load(self, yaml: Any) -> dict[str, Any]:
        """Load and parse ``registries.yaml``; return ``{}`` when absent/empty.

        Args:
            yaml: The PyYAML module.

        Returns:
            The parsed mapping, or an empty dict if the file does not exist or
            is empty.

        Raises:
            K3sRegistryError: If the file cannot be read or is not a YAML
                mapping.
        """
        if not self._path.exists():
            return {}
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise K3sRegistryError(
                f"Cannot read '{self._path}': {exc}"
            ) from exc
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise K3sRegistryError(
                f"Cannot parse '{self._path}' as YAML: {exc}"
            ) from exc
        if parsed is None:
            return {}
        if not isinstance(parsed, dict):
            raise K3sRegistryError(
                f"Unexpected structure in '{self._path}': expected a mapping "
                f"at the top level, found {type(parsed).__name__}."
            )
        return parsed

    def _backup_existing(self) -> None:
        """Copy the current ``registries.yaml`` to a timestamped ``.bak`` file.

        A no-op when the file does not yet exist.  The backup lets an operator
        restore the previous configuration if needed.

        Raises:
            K3sRegistryError: If the backup copy cannot be written.
        """
        if not self._path.exists():
            return
        backup = self._path.with_name(f"{self._path.name}.bak-{timestamp()}")
        try:
            backup.write_bytes(self._path.read_bytes())
        except OSError as exc:
            raise K3sRegistryError(
                f"Cannot back up '{self._path}' to '{backup}': {exc}"
            ) from exc
        log.info("Backed up existing registry config to '%s'", backup)

    def _write(self, yaml: Any, data: dict[str, Any]) -> None:
        """Serialise *data* to ``registries.yaml``.

        Args:
            yaml: The PyYAML module.
            data: The merged registry configuration mapping.

        Raises:
            K3sRegistryError: If the parent directory or file cannot be
                written (e.g. insufficient privileges).
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                yaml.safe_dump(data, default_flow_style=False, sort_keys=True),
                encoding="utf-8",
            )
        except PermissionError as exc:
            raise K3sRegistryError(
                f"Insufficient privileges to write '{self._path}': {exc}. "
                "Re-run the restore as root (the k3s registry configuration "
                "must be written by a privileged user)."
            ) from exc
        except OSError as exc:
            raise K3sRegistryError(
                f"Cannot write '{self._path}': {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Merge logic (non-destructive)
    # ------------------------------------------------------------------

    def _merge(self, data: dict[str, Any], host: str, endpoint: str) -> bool:
        """Merge a mirror + insecure-TLS config for *host* into *data* in place.

        Existing entries are never overwritten: a present ``mirrors[host]`` only
        has *endpoint* appended when missing, and a present ``configs[host]``
        only has absent keys added — an explicitly set ``insecure_skip_verify``
        value (even ``false``) is left as the operator configured it.

        Args:
            data: Parsed ``registries.yaml`` mapping (mutated in place).
            host: Registry ``host:port`` used as the mirror/config key.
            endpoint: Endpoint URL to register, e.g. ``"http://host:port"``.

        Returns:
            True if *data* was modified, False if the entries already existed.
        """
        changed = False

        mirrors = data.setdefault("mirrors", {})
        if not isinstance(mirrors, dict):
            raise K3sRegistryError(
                f"'mirrors' in '{self._path}' is not a mapping "
                f"({type(mirrors).__name__}); refusing to modify it."
            )
        mirror = mirrors.get(host)
        if not isinstance(mirror, dict):
            mirrors[host] = {"endpoint": [endpoint]}
            changed = True
        else:
            endpoints = mirror.setdefault("endpoint", [])
            if not isinstance(endpoints, list):
                raise K3sRegistryError(
                    f"mirrors['{host}'].endpoint in '{self._path}' is not a "
                    f"list ({type(endpoints).__name__}); refusing to modify it."
                )
            if endpoint not in endpoints:
                endpoints.append(endpoint)
                changed = True

        configs = data.setdefault("configs", {})
        if not isinstance(configs, dict):
            raise K3sRegistryError(
                f"'configs' in '{self._path}' is not a mapping "
                f"({type(configs).__name__}); refusing to modify it."
            )
        config = configs.get(host)
        if not isinstance(config, dict):
            configs[host] = {"tls": {"insecure_skip_verify": True}}
            changed = True
        else:
            tls = config.setdefault("tls", {})
            if not isinstance(tls, dict):
                raise K3sRegistryError(
                    f"configs['{host}'].tls in '{self._path}' is not a mapping "
                    f"({type(tls).__name__}); refusing to modify it."
                )
            if "insecure_skip_verify" not in tls:
                tls["insecure_skip_verify"] = True
                changed = True

        return changed

    # ------------------------------------------------------------------
    # k3s restart + readiness
    # ------------------------------------------------------------------

    def _restart_k3s(self) -> None:
        """Restart the k3s service so it re-reads ``registries.yaml``.

        Raises:
            K3sRegistryError: If ``systemctl`` is unavailable, the restart
                fails, or the command times out.
        """
        log.info("Restarting k3s (systemctl restart k3s) to apply the mirror")
        try:
            result = subprocess.run(
                ["systemctl", "restart", "k3s"],
                capture_output=True,
                text=True,
                timeout=_RESTART_TIMEOUT,
            )
        except FileNotFoundError as exc:
            raise K3sRegistryError(
                "Cannot restart k3s: 'systemctl' not found in PATH."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise K3sRegistryError(
                f"'systemctl restart k3s' timed out after {_RESTART_TIMEOUT}s."
            ) from exc
        if result.returncode != 0:
            stderr = result.stderr.strip()
            hint = (
                " (are you running as root?)"
                if "permission" in stderr.lower() or "denied" in stderr.lower()
                else ""
            )
            raise K3sRegistryError(
                f"'systemctl restart k3s' failed (rc={result.returncode}): "
                f"{stderr}{hint}"
            )

    def _wait_node_ready(self) -> None:
        """Wait until the cluster node(s) are ``Ready`` after the k3s restart.

        Delegates to :meth:`lib.kubectl.Kubectl.wait_for_nodes_ready`, which
        tolerates the transient API-server outage during the restart.

        Raises:
            K3sRegistryError: If no node becomes Ready within the wait timeout.
        """
        try:
            self._kubectl.wait_for_nodes_ready()
        except KubectlError as exc:
            raise K3sRegistryError(
                f"Node did not become Ready after restarting k3s: {exc}"
            ) from exc
