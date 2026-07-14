"""Optional OCI/Docker registry backup and restore for awx-migration.

AWX Execution Environments reference images in a local registry.  This module
backs up that registry (its Kubernetes manifests plus the actual images as
OCI archives) and restores it into the target cluster, so custom EE images are
available again after a migration and no ``ImagePullBackOff`` occurs.

The registry is treated as an independent component in its own namespace; none
of the existing AWX backup/restore behaviour is changed.  The feature is only
active when the caller supplies the corresponding CLI options.

Design constraints honoured here:
    * All Kubernetes access goes through :class:`~lib.kubectl.Kubectl`.
    * Image transfers use ``skopeo`` (preferred) or ``crane`` — the only
      subprocess calls, kept inside this library.
    * The image set is limited to the images actually used by the Execution
      Environments (see :func:`lib.registry_rewrite.list_execution_environment_images`),
      so backups stay small.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .kubectl import Kubectl, KubectlError
from .registry_rewrite import RegistryRewriteConfig
from .utils import read_json, write_json

log: logging.Logger = logging.getLogger("awx-migration")

# Resource kinds backed up, in the order they must be applied on restore
# (dependencies first: config/data/storage before workloads that consume them).
_RESOURCE_KINDS: tuple[str, ...] = (
    "secret",
    "configmap",
    "persistentvolumeclaim",
    "deployment",
    "service",
)
_APPLY_ORDER: dict[str, int] = {kind: i for i, kind in enumerate(_RESOURCE_KINDS)}

# Volatile metadata fields stripped before a manifest can be applied elsewhere.
_METADATA_STRIP: frozenset[str] = frozenset({
    "uid",
    "resourceVersion",
    "creationTimestamp",
    "generation",
    "managedFields",
    "ownerReferences",
    "selfLink",
})
_ANNOTATION_STRIP: frozenset[str] = frozenset({
    "kubectl.kubernetes.io/last-applied-configuration",
    "deployment.kubernetes.io/revision",
})

# Auto-generated resources that must never be re-applied.
_SKIP_SECRET_TYPE: str = "kubernetes.io/service-account-token"
_SKIP_CONFIGMAP_NAME: str = "kube-root-ca.crt"

# Local NodePort registries typically serve plain HTTP / self-signed TLS.
_INSECURE_DEFAULT: bool = True

_IMAGE_TIMEOUT: int = 1800  # per image copy (seconds)

# Readiness gate: after the deployment rollout, poll GET /v2/ until the
# registry actually serves (HTTP 200 or 401).  503/404/timeout keep waiting.
_READY_TIMEOUT: int = 120
_READY_POLL: float = 3.0

# Publish gate: after each successful push, poll the registry HTTP API
# (GET /v2/_catalog and GET /v2/<repo>/tags/list) until the pushed tag is
# actually served, up to this timeout.  This is the exact state Kubernetes
# needs at image-pull time, so the restore only completes once it holds.
_PUBLISH_TIMEOUT: int = 60
_PUBLISH_POLL: float = 2.0

# Push retry: registries can answer HTTP 503 transiently while warming up.
# Retry each push with exponential backoff, up to this many attempts.  With
# 6 attempts the full backoff schedule applies: waits of 2, 4, 8, 16, 30 s
# before attempts 2..6 (~60 s total), then the 6th failure raises.
_PUSH_MAX_ATTEMPTS: int = 6
_PUSH_BACKOFF_SECONDS: tuple[int, ...] = (2, 4, 8, 16, 30)


class RegistryError(RuntimeError):
    """Raised on any registry backup or restore failure."""


# ---------------------------------------------------------------------------
# Image reference helpers
# ---------------------------------------------------------------------------

def _image_registry(ref: str) -> str | None:
    """Return the registry host[:port] part of *ref*, or None if it has none.

    Follows the Docker convention: the first path segment is a registry only
    when it contains a dot, a colon, or equals ``localhost``.

    Args:
        ref: Image reference, e.g. ``"10.6.207.31:30500/awx-ee:1.0"``.

    Returns:
        The registry component, or ``None`` for bare references such as
        ``"awx-ee:1.0"``.
    """
    first = ref.split("/", 1)[0]
    if "/" in ref and ("." in first or ":" in first or first == "localhost"):
        return first
    return None


def _image_port(ref: str) -> str | None:
    """Return the port of the registry component of *ref*, if present."""
    registry = _image_registry(ref)
    if registry and ":" in registry:
        return registry.rsplit(":", 1)[1]
    return None


def registry_prefix_from_images(images: list[dict[str, str]]) -> str:
    """Derive the common source registry prefix from saved image entries.

    Inspects the ``image`` reference of each index entry and returns the single
    registry host[:port] they share.  This lets the restore reconstruct the
    ``--registry-from`` value automatically from the backup itself.

    Args:
        images: Index entries as produced by :meth:`RegistryBackup.export`
            (each a ``{"file": ..., "image": <reference>}`` mapping).

    Returns:
        The shared registry prefix, e.g. ``"10.6.207.31:30500"``.

    Raises:
        RegistryError: If no prefix can be found or the images span more than
            one registry (in which case the caller must pass it explicitly).
    """
    prefixes = {
        _image_registry(entry["image"])
        for entry in images
        if _image_registry(entry["image"])
    }
    if len(prefixes) != 1:
        raise RegistryError(
            "Cannot auto-derive the source registry from the backup "
            f"(found {sorted(prefixes)}). Pass --registry-from explicitly."
        )
    return prefixes.pop()


def _sanitize_image_filename(ref: str) -> str:
    """Derive a filesystem-safe ``.tar`` filename from an image reference.

    The registry host is dropped; repository and tag are joined with
    underscores.  Example::

        10.6.207.31:30500/awx-ee-custom:24.6.1-5  ->  awx-ee-custom_24.6.1-5.tar

    Args:
        ref: Full image reference.

    Returns:
        A safe archive filename.  The authoritative reference is stored
        separately in ``images/index.json``.
    """
    registry = _image_registry(ref)
    remainder = ref[len(registry) + 1:] if registry else ref
    safe = remainder.replace("/", "_").replace(":", "_")
    return f"{safe}.tar"


# ---------------------------------------------------------------------------
# Image tool wrapper (skopeo / crane)
# ---------------------------------------------------------------------------

class RegistryTool:
    """Wraps ``skopeo`` or ``crane`` for OCI image save/push/inspect.

    The only subprocess calls in the registry feature live here.  Use
    :meth:`detect` to pick whichever tool is available (skopeo preferred).
    """

    def __init__(self, kind: str, binary: str) -> None:
        """Initialise with a tool kind and its resolved binary path.

        Args:
            kind: ``"skopeo"`` or ``"crane"``.
            binary: Absolute path to the executable.
        """
        self.kind = kind
        self._binary = binary

    @classmethod
    def detect(cls) -> RegistryTool:
        """Return a :class:`RegistryTool` for the first available binary.

        Returns:
            Configured tool wrapper (``skopeo`` preferred over ``crane``).

        Raises:
            RegistryError: If neither ``skopeo`` nor ``crane`` is in PATH.
        """
        for kind in ("skopeo", "crane"):
            binary = shutil.which(kind)
            if binary:
                log.info("Using image tool '%s' (%s)", kind, binary)
                return cls(kind, binary)
        raise RegistryError(
            "Registry image transfer requires 'skopeo' or 'crane' in PATH, "
            "but neither was found. Install one of them or omit the registry "
            "option."
        )

    # -- internal ------------------------------------------------------

    def _run(self, args: list[str], *, timeout: int) -> str:
        """Run the tool with *args* and return stdout.

        Args:
            args: Arguments following the binary.
            timeout: Timeout in seconds.

        Returns:
            Captured stdout.

        Raises:
            RegistryError: On non-zero exit, timeout, or launch failure.
        """
        cmd = [self._binary, *args]
        log.debug("%s: %s", self.kind, " ".join(cmd))
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise RegistryError(f"Cannot launch {self.kind}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RegistryError(
                f"{self.kind} timed out after {timeout}s: {' '.join(args)}"
            ) from exc
        if result.returncode != 0:
            raise RegistryError(
                f"{self.kind} failed (rc={result.returncode}): "
                f"{result.stderr.strip()}"
            )
        return result.stdout

    # -- operations ----------------------------------------------------

    def save_image(
        self, ref: str, dest: str | Path, *, insecure: bool = _INSECURE_DEFAULT
    ) -> None:
        """Save image *ref* from its registry to an OCI archive at *dest*.

        Args:
            ref: Source image reference.
            dest: Destination ``.tar`` path (OCI archive).
            insecure: Skip TLS verification of the source registry.

        Raises:
            RegistryError: On failure.
        """
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self.kind == "skopeo":
            args = ["copy"]
            if insecure:
                args.append("--src-tls-verify=false")
            args += [f"docker://{ref}", f"oci-archive:{dest}"]
        else:  # crane
            args = ["pull", ref, str(dest), "--format=oci"]
            if insecure:
                args.append("--insecure")
        self._run(args, timeout=_IMAGE_TIMEOUT)

    def push_image(
        self, src: str | Path, ref: str, *, insecure: bool = _INSECURE_DEFAULT
    ) -> None:
        """Push the OCI archive at *src* to image reference *ref*.

        Args:
            src: Source ``.tar`` path (OCI archive).
            ref: Destination image reference.
            insecure: Skip TLS verification of the destination registry.

        Raises:
            RegistryError: On failure.
        """
        src = Path(src)
        if self.kind == "skopeo":
            args = ["copy"]
            if insecure:
                args.append("--dest-tls-verify=false")
            args += [f"oci-archive:{src}", f"docker://{ref}"]
        else:  # crane
            args = ["push", str(src), ref]
            if insecure:
                args.append("--insecure")
        self._run(args, timeout=_IMAGE_TIMEOUT)

    def image_exists(
        self, ref: str, *, insecure: bool = _INSECURE_DEFAULT
    ) -> bool:
        """Return True if image *ref* can be inspected in its registry.

        Args:
            ref: Image reference.
            insecure: Skip TLS verification.

        Returns:
            True when the image manifest is retrievable, False otherwise.
        """
        if self.kind == "skopeo":
            args = ["inspect"]
            if insecure:
                args.append("--tls-verify=false")
            args.append(f"docker://{ref}")
        else:  # crane
            args = ["manifest", ref]
            if insecure:
                args.append("--insecure")
        try:
            self._run(args, timeout=120)
            return True
        except RegistryError:
            return False


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

class RegistryBackup:
    """Backs up a registry namespace: manifests plus in-use OCI images.

    All Kubernetes reads go through the injected :class:`~lib.kubectl.Kubectl`
    instance (bound to the registry namespace).  Image transfers go through
    :class:`RegistryTool`.
    """

    def __init__(self, kubectl: Kubectl, tool: RegistryTool) -> None:
        """Initialise with a registry-namespace Kubectl and an image tool.

        Args:
            kubectl: Kubectl wrapper bound to the registry namespace.
            tool: Detected image tool wrapper.
        """
        self._kubectl = kubectl
        self._tool = tool

    # -- manifests -----------------------------------------------------

    def _skip_resource(self, kind: str, item: dict[str, Any]) -> bool:
        """Return True for auto-generated resources that must not be exported."""
        name = item.get("metadata", {}).get("name", "")
        if kind == "secret" and item.get("type") == _SKIP_SECRET_TYPE:
            return True
        if kind == "configmap" and name == _SKIP_CONFIGMAP_NAME:
            return True
        return False

    def _clean_resource(self, item: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of *item* safe to apply on a fresh cluster.

        Strips volatile metadata, ``status``, and cluster-assigned fields
        (Service ``clusterIP``, PVC ``volumeName``) while preserving values a
        migration must keep, such as a Service ``nodePort``.

        Args:
            item: Raw Kubernetes resource dict.

        Returns:
            Cleaned deep copy.
        """
        cleaned = copy.deepcopy(item)
        meta = {
            k: v for k, v in cleaned.get("metadata", {}).items()
            if k not in _METADATA_STRIP
        }
        annotations = {
            k: v for k, v in meta.get("annotations", {}).items()
            if k not in _ANNOTATION_STRIP
        }
        if annotations:
            meta["annotations"] = annotations
        else:
            meta.pop("annotations", None)
        # Namespace is re-applied via the target Kubectl context; drop it so a
        # renamed target namespace cannot conflict.
        meta.pop("namespace", None)
        cleaned["metadata"] = meta
        cleaned.pop("status", None)

        kind = cleaned.get("kind", "").lower()
        spec = cleaned.get("spec")
        if isinstance(spec, dict):
            if kind == "service":
                for key in ("clusterIP", "clusterIPs"):
                    spec.pop(key, None)
            elif kind == "persistentvolumeclaim":
                spec.pop("volumeName", None)
        return cleaned

    def _export_manifests(self, dest: Path) -> list[str]:
        """Export registry manifests to ``<dest>/manifests`` as JSON.

        Args:
            dest: Registry backup directory root.

        Returns:
            Sorted list of backup-relative manifest paths.

        Raises:
            RegistryError: On any kubectl failure.
        """
        rel_paths: list[str] = []
        for kind in _RESOURCE_KINDS:
            try:
                data = self._kubectl.json(["get", kind, "-o", "json"])
            except KubectlError as exc:
                raise RegistryError(
                    f"Failed to list '{kind}' in namespace "
                    f"'{self._kubectl.namespace}': {exc}"
                ) from exc
            for item in data.get("items", []):
                if self._skip_resource(kind, item):
                    continue
                cleaned = self._clean_resource(item)
                name = cleaned["metadata"]["name"]
                rel = f"manifests/{kind}-{name}.json"
                write_json(dest / rel, cleaned)
                rel_paths.append(rel)
                log.info("Backed up %s/%s", kind, name)
        return sorted(rel_paths)

    # -- images --------------------------------------------------------

    def _registry_ports(self) -> set[str]:
        """Return the set of port and nodePort values of registry services."""
        ports: set[str] = set()
        try:
            services = self._kubectl.list_services()
        except KubectlError as exc:
            log.warning("Could not list registry services: %s", exc)
            return ports
        for svc in services:
            for port in svc.get("spec", {}).get("ports", []):
                for key in ("port", "nodePort"):
                    if key in port and port[key] is not None:
                        ports.add(str(port[key]))
        return ports

    def _select_images(self, candidate_images: list[str]) -> list[str]:
        """Select the candidate images that belong to this registry.

        Matches by the port of the image's registry component against the
        registry's service ports.  If nothing matches (e.g. the ports cannot
        be determined), all candidates are kept so no in-use image is missed.

        Args:
            candidate_images: Distinct EE image references from the database.

        Returns:
            The images to save.
        """
        ports = self._registry_ports()
        if not ports:
            log.warning(
                "Registry service ports unknown — saving all %d candidate "
                "image(s).", len(candidate_images),
            )
            return sorted(candidate_images)
        matched = [
            img for img in candidate_images if _image_port(img) in ports
        ]
        if not matched:
            log.warning(
                "No EE image matched registry ports %s — saving all %d "
                "candidate image(s).", sorted(ports), len(candidate_images),
            )
            return sorted(candidate_images)
        log.info(
            "Selected %d of %d EE image(s) matching registry ports %s",
            len(matched), len(candidate_images), sorted(ports),
        )
        return sorted(matched)

    def _save_images(
        self, dest: Path, images: list[str]
    ) -> list[dict[str, str]]:
        """Save *images* as OCI archives under ``<dest>/images``.

        A failure to save an individual image is logged as a warning and the
        image is skipped, so one unreachable image cannot abort the backup.

        Args:
            dest: Registry backup directory root.
            images: Image references to save.

        Returns:
            List of ``{"file": <relative path>, "image": <reference>}`` for
            every successfully saved image.
        """
        saved: list[dict[str, str]] = []
        for ref in images:
            rel = f"images/{_sanitize_image_filename(ref)}"
            log.info("Saving image '%s' -> '%s'", ref, rel)
            try:
                self._tool.save_image(ref, dest / rel)
            except RegistryError as exc:
                log.warning("Skipping image '%s': %s", ref, exc)
                continue
            saved.append({"file": rel, "image": ref})
        return saved

    # -- public --------------------------------------------------------

    def export(
        self, directory: str | Path, candidate_images: list[str]
    ) -> dict[str, Any]:
        """Back up the registry namespace to *directory*.

        Exports manifests and the in-use images (selected from
        *candidate_images*), and writes an ``images/index.json`` mapping.

        Args:
            directory: Destination directory (created if absent).
            candidate_images: Distinct EE image references from the AWX
                database; the images actually served by this registry are
                selected automatically.

        Returns:
            Summary dict with ``namespace``, ``manifests`` (paths),
            ``images`` (index entries), and ``tool`` (tool name), suitable for
            :meth:`lib.manifest.Manifest.set_registry`.

        Raises:
            RegistryError: On manifest export failure.
        """
        dest = Path(directory)
        dest.mkdir(parents=True, exist_ok=True)
        log.info(
            "Backing up registry namespace '%s'", self._kubectl.namespace
        )

        manifests = self._export_manifests(dest)
        images = self._save_images(dest, self._select_images(candidate_images))
        write_json(dest / "images" / "index.json", images)

        log.info(
            "Registry backup complete: %d manifest(s), %d image(s)",
            len(manifests), len(images),
        )
        return {
            "namespace": self._kubectl.namespace,
            "manifests": manifests,
            "images": images,
            "tool": self._tool.kind,
        }


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

class RegistryRestore:
    """Restores a registry namespace and its images into the target cluster."""

    #: Timeout (seconds) for each registry deployment rollout.
    ROLLOUT_TIMEOUT: int = 300

    def __init__(self, kubectl: Kubectl, tool: RegistryTool) -> None:
        """Initialise with a registry-namespace Kubectl and an image tool.

        Args:
            kubectl: Kubectl wrapper bound to the registry namespace.
            tool: Detected image tool wrapper.
        """
        self._kubectl = kubectl
        self._tool = tool

    # -- manifests -----------------------------------------------------

    def _load_manifests(self, directory: Path) -> list[dict[str, Any]]:
        """Load and order the exported manifest dicts from *directory*.

        Args:
            directory: Registry backup directory root.

        Returns:
            Manifest dicts sorted into a safe apply order.

        Raises:
            RegistryError: If the manifests directory is missing or unreadable.
        """
        manifests_dir = directory / "manifests"
        if not manifests_dir.is_dir():
            raise RegistryError(
                f"No manifests directory found at '{manifests_dir}'"
            )
        docs: list[dict[str, Any]] = []
        for path in sorted(manifests_dir.glob("*.json")):
            try:
                docs.append(read_json(path))
            except Exception as exc:  # noqa: BLE001 - reported as RegistryError
                raise RegistryError(
                    f"Cannot read manifest '{path}': {exc}"
                ) from exc
        docs.sort(key=lambda d: _APPLY_ORDER.get(d.get("kind", "").lower(), 99))
        return docs

    def _apply_manifests(self, docs: list[dict[str, Any]]) -> list[str]:
        """Apply manifest dicts via kubectl and return deployment names.

        Args:
            docs: Ordered manifest dicts.

        Returns:
            Names of the applied Deployments (to wait on later).

        Raises:
            RegistryError: On any apply failure.
        """
        deployments: list[str] = []
        for doc in docs:
            kind = doc.get("kind", "?")
            name = doc.get("metadata", {}).get("name", "?")
            log.info("Applying %s/%s", kind, name)
            try:
                self._kubectl.apply(json.dumps(doc))
            except KubectlError as exc:
                raise RegistryError(
                    f"Failed to apply {kind}/{name}: {exc}"
                ) from exc
            if kind.lower() == "deployment":
                deployments.append(name)
        return deployments

    def _wait_running(self, deployments: list[str]) -> None:
        """Wait until each registry deployment is rolled out and Ready.

        For every deployment this confirms two things:

        * the rollout completed (``kubectl rollout status``), and
        * the deployment reports ``Available=True`` — which only happens once
          its pod has passed the readiness probe (``Ready=True``), not merely
          reached the ``Running`` phase.

        The subsequent ``GET /v2/`` gate then confirms the registry actually
        serves requests.
        """
        for name in deployments:
            try:
                self._kubectl.wait_for_deployment(
                    name, timeout=self.ROLLOUT_TIMEOUT
                )
                self._kubectl.wait_for_deployment_available(
                    name, timeout=self.ROLLOUT_TIMEOUT
                )
            except KubectlError as exc:
                raise RegistryError(
                    f"Registry deployment '{name}' did not become ready: {exc}"
                ) from exc

    # -- images --------------------------------------------------------

    def _target_ref(self, ref: str, config: RegistryRewriteConfig) -> str:
        """Rewrite *ref* from source to target registry (same rule as the EE rewrite)."""
        if ref.startswith(config.source):
            return config.target + ref[len(config.source):]
        return ref

    def _push_images(
        self,
        directory: Path,
        index: list[dict[str, str]],
        config: RegistryRewriteConfig,
    ) -> list[str]:
        """Push saved OCI archives to the target registry.

        Args:
            directory: Registry backup directory root.
            index: Entries from ``images/index.json``.
            config: Source/target registry configuration (reused from the EE
                rewrite) determining the push destination.

        Returns:
            List of target image references that were pushed.

        Raises:
            RegistryError: If an image archive is missing or a push fails.
        """
        pushed: list[str] = []
        for entry in index:
            src = directory / entry["file"]
            if not src.is_file():
                raise RegistryError(f"Image archive missing: '{src}'")
            target = self._target_ref(entry["image"], config)
            log.info("Pushing image '%s' -> '%s'", entry["image"], target)
            self._push_one(src, target)
            pushed.append(target)
        return pushed

    def _push_one(self, src: Path, target: str) -> None:
        """Push a single OCI archive, retrying transient HTTP 503 responses.

        A registry that has just started may answer ``skopeo copy`` with
        ``HTTP 503 Service Unavailable`` while still warming up.  Such failures
        are retried with exponential backoff (:data:`_PUSH_BACKOFF_SECONDS`),
        up to :data:`_PUSH_MAX_ATTEMPTS` attempts.  Any non-503 failure is
        raised immediately.

        Args:
            src: Source ``.tar`` path (OCI archive).
            target: Destination image reference.

        Raises:
            RegistryError: On a non-503 failure, or if 503 persists after all
                attempts.
        """
        for attempt in range(1, _PUSH_MAX_ATTEMPTS + 1):
            try:
                self._tool.push_image(src, target)
                return
            except RegistryError as exc:
                if "503" not in str(exc):
                    raise
                if attempt >= _PUSH_MAX_ATTEMPTS:
                    raise RegistryError(
                        f"Push of '{target}' failed after {attempt} attempts "
                        f"(HTTP 503): {exc}"
                    ) from exc
                delay = _PUSH_BACKOFF_SECONDS[
                    min(attempt - 1, len(_PUSH_BACKOFF_SECONDS) - 1)
                ]
                log.warning(
                    "Push of '%s' got HTTP 503; retrying in %ds "
                    "(attempt %d/%d)",
                    target, delay, attempt, _PUSH_MAX_ATTEMPTS,
                )
                time.sleep(delay)

    # -- verification --------------------------------------------------

    def _verify_reachable(self, address: str) -> bool:
        """Return True if the registry answers ``GET /v2/`` at *address*.

        Tries HTTP then HTTPS.  HTTP 200 and 401 both indicate a live
        registry.  Never raises — reachability problems are warnings only.

        Args:
            address: Registry ``host:port``.

        Returns:
            True if reachable, False otherwise.
        """
        for scheme in ("http", "https"):
            url = f"{scheme}://{address}/v2/"
            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status in (200, 401):
                        log.info("Registry reachable at %s", url)
                        return True
            except urllib.error.HTTPError as exc:
                if exc.code in (200, 401):
                    log.info("Registry reachable at %s (HTTP %d)", url, exc.code)
                    return True
                log.debug("Registry %s returned HTTP %d", url, exc.code)
            except (urllib.error.URLError, OSError) as exc:
                log.debug("Registry %s not reachable: %s", url, exc)
        log.warning(
            "Registry at '%s' did not answer GET /v2/ — images may not be "
            "pullable yet.", address,
        )
        return False

    def _probe_v2_host(self, address: str) -> tuple[int | None, str]:
        """Probe ``GET /v2/`` from the restore host, proxy-free.

        This exercises the *same* network path that ``skopeo copy`` will later
        use to push images, so it is the authoritative readiness signal.

        Proxy environment variables (``HTTP_PROXY``/``HTTPS_PROXY``/
        ``NO_PROXY``) are deliberately ignored: the registry is a direct
        NodePort address that must not be routed through a corporate proxy
        (which otherwise answers instead of the registry).

        Args:
            address: Registry ``host:port``.

        Returns:
            A ``(status, description)`` tuple.  ``status`` is the HTTP status
            code (including error codes such as 503) or ``None`` when no
            connection could be made.  ``description`` is a short human-readable
            reason (e.g. ``"HTTP 200"``, ``"timeout"``, ``"connection refused"``)
            for use in diagnostics.
        """
        # Empty ProxyHandler => no proxies, env proxy settings are ignored.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        last_desc = "no response"
        for scheme in ("http", "https"):
            url = f"{scheme}://{address}/v2/"
            try:
                req = urllib.request.Request(url, method="GET")
                with opener.open(req, timeout=10) as resp:
                    return resp.status, f"HTTP {resp.status}"
            except urllib.error.HTTPError as exc:
                return exc.code, f"HTTP {exc.code}"
            except urllib.error.URLError as exc:
                reason = exc.reason
                if isinstance(reason, TimeoutError):
                    last_desc = "timeout"
                elif isinstance(reason, ConnectionRefusedError):
                    last_desc = "connection refused"
                else:
                    last_desc = f"unreachable ({reason})"
            except TimeoutError:
                last_desc = "timeout"
            except OSError as exc:
                last_desc = f"unreachable ({exc})"
        return None, last_desc

    def _probe_v2_in_pod(self) -> int | None:
        """Return the HTTP status of ``GET /v2/`` probed *inside* the pod.

        Runs ``wget`` against ``http://localhost:5000/v2/`` in a Running
        registry pod via ``kubectl exec``.  This is the most robust readiness
        signal: it bypasses any host-side proxy and external routing entirely,
        checking the registry from within the cluster.

        Returns:
            The HTTP status code parsed from the response, or ``None`` if no
            Running pod is found, the probe tool is unavailable, or no HTTP
            status could be read (caller then falls back to a direct probe).
        """
        try:
            pods = self._kubectl.list_pods()
        except KubectlError:
            return None
        running = [
            p for p in pods
            if p.get("status", {}).get("phase") == "Running"
        ]
        if not running:
            return None
        pod = running[0]["metadata"]["name"]
        # busybox wget prints the response headers (incl. status line) to
        # stderr with -S; merge into stdout and never fail the exec itself.
        cmd = [
            "sh", "-c",
            "wget -q -S -O /dev/null http://localhost:5000/v2/ 2>&1 || true",
        ]
        try:
            out = self._kubectl.exec(pod, cmd, timeout=10)
        except KubectlError:
            return None
        match = re.search(r"HTTP/\S+\s+(\d{3})", out)
        return int(match.group(1)) if match else None

    def _wait_registry_ready(
        self,
        address: str,
        *,
        timeout: int = _READY_TIMEOUT,
        poll_interval: float = _READY_POLL,
    ) -> None:
        """Block until the registry actively serves ``GET /v2/``.

        Readiness is decided by the **host** probe, because that is the path
        ``skopeo copy`` will use to push images: a registry that answers only
        from inside the cluster is not yet usable for the restore.  HTTP 200 or
        401 from the host means ready; HTTP 503/404 and connection failures
        merely lead to further waiting, up to *timeout* seconds.

        On timeout, an in-pod probe is run purely for diagnostics so the error
        message can distinguish "registry up, but unreachable from this host"
        from "registry container not responding".

        Args:
            address: Registry ``host:port``.
            timeout: Maximum total wait time in seconds.
            poll_interval: Seconds between polls.

        Raises:
            RegistryError: If the registry is not ready within *timeout*.
        """
        log.info(
            "Waiting for registry at '%s' to become ready (GET /v2/)", address
        )
        deadline = time.monotonic() + timeout
        host_desc = "no response"
        while True:
            # Host probe first: it exercises the same path as the image push.
            status, host_desc = self._probe_v2_host(address)
            if status in (200, 401):
                log.info("Registry '%s' is ready (HTTP %d)", address, status)
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sleep_for = min(poll_interval, remaining)
            log.debug(
                "Registry not ready yet (host probe: %s); retrying in %.0fs "
                "(%.0fs remaining)", host_desc, sleep_for, remaining,
            )
            time.sleep(sleep_for)

        # Timed out — probe the registry from inside the pod for diagnostics.
        in_pod_status = self._probe_v2_in_pod()
        in_pod_desc = (
            f"HTTP {in_pod_status}" if in_pod_status is not None
            else "no response"
        )
        if in_pod_status in (200, 401):
            hint = (
                "Registry is running inside Kubernetes but is not reachable "
                "from the restore host. Check NodePort exposure, firewall "
                "rules, and routing between this host and the cluster nodes."
            )
        elif in_pod_status is not None:
            hint = (
                f"Registry answers in-cluster with HTTP {in_pod_status}; the "
                "container may still be starting up."
            )
        else:
            hint = "Registry container itself is not responding."
        raise RegistryError(
            f"Registry at '{address}' did not become ready within {timeout}s.\n"
            f"  Host probe (proxy-free): {host_desc}\n"
            f"  In-pod probe:            {in_pod_desc}\n"
            f"  Hint: {hint}"
        )

    def _verify_images(self, target_refs: list[str]) -> None:
        """Warn (never raise) for any target image that cannot be inspected."""
        missing = [ref for ref in target_refs if not self._tool.image_exists(ref)]
        if missing:
            log.warning(
                "%d image(s) could not be verified in the target registry: %s",
                len(missing), missing,
            )
        else:
            log.info("Verified %d image(s) in the target registry", len(target_refs))

    # -- publish synchronisation ---------------------------------------

    @staticmethod
    def _split_repo_tag(ref: str) -> tuple[str, str]:
        """Split a target image reference into ``(repository, tag)``.

        The registry host[:port] prefix is dropped, leaving the repository path
        as used by the registry HTTP API (``/v2/<repository>/tags/list``).  A
        ``@sha256:...`` digest or trailing ``:tag`` is returned as the tag; a
        reference without either defaults to ``"latest"``.

        Args:
            ref: Full target image reference, e.g.
                ``"192.168.121.185:30500/awx-ee-custom:24.6.1-5"``.

        Returns:
            ``(repository, tag)``, e.g. ``("awx-ee-custom", "24.6.1-5")``.
        """
        registry = _image_registry(ref)
        remainder = ref[len(registry) + 1:] if registry else ref
        if "@" in remainder:
            repo, _, tag = remainder.partition("@")
            return repo, tag
        if ":" in remainder:
            repo, _, tag = remainder.rpartition(":")
            return repo, tag
        return remainder, "latest"

    def _registry_get_json(self, address: str, path: str) -> Any | None:
        """GET *path* from the registry HTTP API (proxy-free) and parse JSON.

        Tries HTTP then HTTPS, ignoring proxy environment variables for the
        same reason as :meth:`_probe_v2_host` (the registry is a direct
        NodePort address that must not be routed through a corporate proxy).

        Args:
            address: Registry ``host:port``.
            path: Request path, e.g. ``"/v2/_catalog"``.

        Returns:
            The decoded JSON body on HTTP 200, or ``None`` on any non-200
            status, connection error, or unparseable body.
        """
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for scheme in ("http", "https"):
            url = f"{scheme}://{address}{path}"
            try:
                req = urllib.request.Request(url, method="GET")
                with opener.open(req, timeout=10) as resp:
                    if resp.status == 200:
                        return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                log.debug("Registry GET %s returned HTTP %d", url, exc.code)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log.debug("Registry GET %s failed: %s", url, exc)
        return None

    def _registry_serves_tag(
        self, address: str, repository: str, tag: str
    ) -> bool:
        """Return True if the registry API confirms *repository* and *tag*.

        Performs both checks required for an image to count as fully restored:

        * the repository appears in ``GET /v2/_catalog``, and
        * the tag appears in ``GET /v2/<repository>/tags/list``.

        Never raises — a missing repository/tag or transient error simply
        yields ``False`` so the caller keeps polling.
        """
        catalog = self._registry_get_json(address, "/v2/_catalog")
        if not isinstance(catalog, dict):
            return False
        if repository not in (catalog.get("repositories") or []):
            return False
        tags = self._registry_get_json(
            address, f"/v2/{repository}/tags/list"
        )
        if not isinstance(tags, dict):
            return False
        return tag in (tags.get("tags") or [])

    def _wait_images_published(
        self,
        address: str,
        target_refs: list[str],
        *,
        timeout: int = _PUBLISH_TIMEOUT,
        poll_interval: float = _PUBLISH_POLL,
    ) -> None:
        """Block until the registry HTTP API serves every pushed image.

        For each reference this polls the registry API (``_catalog`` and
        ``tags/list``) until both confirm the tag, then reads the image once
        via ``skopeo inspect docker://...`` — the exact path Kubernetes uses
        at image-pull time.  Only when all images pass does this return.

        This is a pure synchronisation phase: it neither pushes images nor
        alters any existing retry behaviour.

        Args:
            address: Target registry ``host:port``.
            target_refs: Target references that were successfully pushed.
            timeout: Maximum wait per image, in seconds.
            poll_interval: Seconds between polls.

        Raises:
            RegistryError: If the registry does not publish an image's tag
                within *timeout*, or if a published image cannot be read via
                ``skopeo inspect``.
        """
        for ref in target_refs:
            repository, tag = self._split_repo_tag(ref)
            log.info(
                "Waiting until registry publishes image:\n"
                "  Repository: %s\n  Tag: %s",
                repository, tag,
            )
            deadline = time.monotonic() + timeout
            while not self._registry_serves_tag(address, repository, tag):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.error(
                        "Registry did not publish image within %ds:\n"
                        "  Repository: %s\n  Tag: %s",
                        timeout, repository, tag,
                    )
                    raise RegistryError(
                        f"Registry at '{address}' did not publish image "
                        f"within {timeout}s:\n"
                        f"  Repository: {repository}\n  Tag: {tag}"
                    )
                time.sleep(min(poll_interval, remaining))

            # Stronger, Kubernetes-equivalent read of the published image.
            if not self._tool.image_exists(ref):
                log.error(
                    "Registry did not publish image within %ds:\n"
                    "  Repository: %s\n  Tag: %s",
                    timeout, repository, tag,
                )
                raise RegistryError(
                    f"Image '{ref}' is listed by the registry API but could "
                    f"not be read via '{self._tool.kind} inspect'; the "
                    "registry is not yet serving it as Kubernetes expects."
                )
            log.info(
                "Registry confirmed image:\n  Repository: %s\n  Tag: %s",
                repository, tag,
            )

    # -- address discovery ---------------------------------------------

    def _discover_nodeport(self) -> int:
        """Return the nodePort of the registry's NodePort service.

        Reads the services in the registry namespace and returns the first
        ``nodePort`` of the first ``NodePort``-type service found.

        Returns:
            The nodePort number.

        Raises:
            RegistryError: If no NodePort service with a nodePort is present.
        """
        try:
            services = self._kubectl.list_services()
        except KubectlError as exc:
            raise RegistryError(
                f"Cannot list services in namespace "
                f"'{self._kubectl.namespace}': {exc}"
            ) from exc
        for svc in services:
            spec = svc.get("spec", {})
            if spec.get("type") != "NodePort":
                continue
            for port in spec.get("ports", []):
                if port.get("nodePort"):
                    return int(port["nodePort"])
        raise RegistryError(
            "No NodePort service with a nodePort found in namespace "
            f"'{self._kubectl.namespace}'; cannot derive the registry address. "
            "Pass --registry-to explicitly."
        )

    def _discover_target_address(self) -> str:
        """Compose the registry address ``<node-ip>:<nodePort>`` dynamically.

        The nodePort comes from the restored NodePort service; the node IP is
        resolved from the cluster.  Neither value is hard-coded or supplied by
        the user.

        Returns:
            The reachable registry address, e.g. ``"192.168.121.185:30500"``.

        Raises:
            RegistryError: If the nodePort or a node IP cannot be determined.
        """
        nodeport = self._discover_nodeport()
        try:
            node_ip = self._kubectl.node_ip()
        except KubectlError as exc:
            raise RegistryError(
                f"Cannot determine a node IP for the registry address: {exc}"
            ) from exc
        address = f"{node_ip}:{nodeport}"
        log.info("Derived registry address from cluster: %s", address)
        return address

    # -- public --------------------------------------------------------

    def restore(
        self,
        directory: str | Path,
        *,
        source: str | None = None,
        target: str | None = None,
    ) -> RegistryRewriteConfig:
        """Restore the registry namespace, its manifests, and its images.

        Steps: create namespace → apply manifests → wait for rollout → derive
        the effective source/target registry addresses → push images to the
        target registry → verify reachability and image presence (warnings
        only).  The caller performs the Execution Environment rewrite
        afterwards, reusing the returned configuration.

        Address resolution (no hard-coding, no manual IP required):
            * ``target`` — if omitted, composed as ``<node-ip>:<nodePort>`` by
              reading the restored NodePort service and a cluster node IP.
            * ``source`` — if omitted, derived from the registry prefix shared
              by the backed-up images.

        Args:
            directory: Registry backup directory root (``<extracted>/registry``).
            source: Optional source registry prefix override (``--registry-from``).
            target: Optional target registry prefix override (``--registry-to``).

        Returns:
            The effective :class:`RegistryRewriteConfig` actually used, so the
            caller can reuse it for the Execution Environment rewrite.

        Raises:
            RegistryError: On namespace, manifest, rollout, push, or address
                resolution failure.
        """
        directory = Path(directory)
        if not directory.is_dir():
            raise RegistryError(
                f"Registry backup directory not found: '{directory}'"
            )
        log.info(
            "Restoring registry into namespace '%s'", self._kubectl.namespace
        )

        # 1 — namespace
        try:
            self._kubectl.create_namespace()
        except KubectlError as exc:
            raise RegistryError(
                f"Cannot create namespace '{self._kubectl.namespace}': {exc}"
            ) from exc

        # 2 — manifests
        docs = self._load_manifests(directory)
        deployments = self._apply_manifests(docs)

        # 3 — wait for running
        self._wait_running(deployments)

        # 4 — load image index and resolve the effective addresses.
        # The target is derived only now, after the NodePort service exists.
        index_path = directory / "images" / "index.json"
        index: list[dict[str, str]] = (
            read_json(index_path) if index_path.is_file() else []
        )
        effective_target = target or self._discover_target_address()
        effective_source = source or registry_prefix_from_images(index)
        config = RegistryRewriteConfig(
            source=effective_source, target=effective_target
        )
        log.info(
            "Registry rewrite mapping: '%s' -> '%s'",
            config.source, config.target,
        )

        # 5 — wait until the registry actually serves before the first push.
        # Rollout completion does not guarantee the HTTP endpoint is ready;
        # pushing too early yields transient HTTP 503 responses.
        self._wait_registry_ready(config.target)

        # 6 — push images (each push retries transient HTTP 503 with backoff)
        pushed = self._push_images(directory, index, config)

        # 7 — synchronisation: block until the registry HTTP API actually
        # serves every pushed tag (GET /v2/_catalog + /v2/<repo>/tags/list),
        # polling up to _PUBLISH_TIMEOUT.  This does not touch the push or its
        # retry mechanism; it only confirms the state Kubernetes needs at pull
        # time, so the restore completes only once all images are published.
        self._wait_images_published(config.target, pushed)

        # 8 — verify (warnings only)
        self._verify_reachable(config.target)
        self._verify_images(pushed)

        log.info(
            "Registry restore complete: %d manifest(s), %d image(s) pushed",
            len(docs), len(pushed),
        )
        return config
