"""Kubernetes access layer for awx-migration.

All kubectl interactions must go through this module.
Backup and restore scripts must never invoke subprocess with kubectl directly.
Pods are located exclusively via Kubernetes label selectors — never by name.
"""

from __future__ import annotations

import base64
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from .config import NAMESPACE

log: logging.Logger = logging.getLogger("awx")


class KubectlError(RuntimeError):
    """Raised on any kubectl command failure."""


class Kubectl:
    """Encapsulates all kubectl interactions for the awx-migration toolchain.

    Every public method retries on transient failures, logs its actions, and
    raises KubectlError on terminal failure rather than propagating raw
    subprocess exceptions.  Pods are resolved exclusively via label selectors.
    """

    DEFAULT_TIMEOUT: int = 30
    RETRY_COUNT: int = 3
    RETRY_DELAY: float = 2.0

    def __init__(self, namespace: str = NAMESPACE) -> None:
        """Initialise a Kubectl wrapper bound to *namespace*.

        Args:
            namespace: Kubernetes namespace. Defaults to config.NAMESPACE.

        Raises:
            KubectlError: If the kubectl binary is not found in PATH.
        """
        self.namespace: str = namespace
        self._kubectl: str = self._find_kubectl()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _find_kubectl(self) -> str:
        binary = shutil.which("kubectl")
        if binary is None:
            raise KubectlError("kubectl binary not found in PATH")
        return binary

    def _build_cmd(
        self, args: list[str], *, namespaced: bool = True
    ) -> list[str]:
        cmd: list[str] = [self._kubectl]
        if namespaced:
            cmd += ["-n", self.namespace]
        cmd += args
        return cmd

    def _running_pods(self, label_selector: str) -> list[dict[str, Any]]:
        """Return all Running pods matching *label_selector*, oldest first."""
        pods = self.list_pods(label_selector=label_selector)
        running = [
            p for p in pods
            if p.get("status", {}).get("phase", "") == "Running"
        ]
        running.sort(
            key=lambda p: p.get("metadata", {}).get("creationTimestamp", "")
        )
        return running

    def _running_pods_by_prefix(
        self, prefixes: list[str]
    ) -> list[dict[str, Any]]:
        """Return all Running pods whose name starts with any of *prefixes*, oldest first."""
        all_pods = self.list_pods()
        running = [
            p for p in all_pods
            if p.get("status", {}).get("phase", "") == "Running"
            and any(
                p.get("metadata", {}).get("name", "").startswith(pfx)
                for pfx in prefixes
            )
        ]
        running.sort(
            key=lambda p: p.get("metadata", {}).get("creationTimestamp", "")
        )
        return running

    def _find_pod(
        self,
        selectors: list[str],
        prefixes: list[str],
        description: str,
    ) -> str:
        """Resolve the oldest Running pod via label selectors, then name prefixes.

        Tries each entry in *selectors* in order; if none yields a Running pod,
        falls back to matching pods whose name starts with any entry in
        *prefixes*.

        Args:
            selectors: Label selector expressions to try in order.
            prefixes: Pod-name prefixes used as last-resort fallback.
            description: Human-readable component name used in the error message.

        Returns:
            Name of the oldest Running pod found.

        Raises:
            KubectlError: If no Running pod is found by any strategy.
        """
        for selector in selectors:
            pods = self._running_pods(selector)
            if pods:
                name: str = pods[0]["metadata"]["name"]
                log.debug(
                    "Resolved %s pod '%s' via selector '%s'",
                    description,
                    name,
                    selector,
                )
                return name

        pods = self._running_pods_by_prefix(prefixes)
        if pods:
            name = pods[0]["metadata"]["name"]
            log.debug(
                "Resolved %s pod '%s' via name prefix %s",
                description,
                name,
                prefixes,
            )
            return name

        raise KubectlError(
            f"No Running {description} pod found in namespace '{self.namespace}' "
            f"(tried selectors {selectors} and prefixes {prefixes})"
        )

    # ------------------------------------------------------------------
    # Core command execution
    # ------------------------------------------------------------------

    def run(
        self,
        args: list[str],
        *,
        namespaced: bool = True,
        timeout: int = DEFAULT_TIMEOUT,
        retries: int = RETRY_COUNT,
        stdin: Optional[str] = None,
    ) -> str:
        """Execute a kubectl command with automatic retry.

        Args:
            args: kubectl sub-command and flags,
                  e.g. ``["get", "pods", "-o", "json"]``.
            namespaced: Prepend ``-n <namespace>`` when True (default).
            timeout: Per-attempt timeout in seconds.
            retries: Maximum number of attempts before raising.
            stdin: Optional text to pipe into stdin.

        Returns:
            Stripped stdout of the successful command.

        Raises:
            KubectlError: After all retries are exhausted.
        """
        cmd = self._build_cmd(args, namespaced=namespaced)
        last_error: KubectlError = KubectlError("kubectl: no attempt made")

        for attempt in range(1, retries + 1):
            log.debug(
                "kubectl [%d/%d]: %s", attempt, retries, " ".join(cmd)
            )
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    input=stdin,
                )
                if result.returncode == 0:
                    return result.stdout.strip()
                stderr = result.stderr.strip()
                log.warning(
                    "kubectl failed (attempt %d/%d, rc=%d): %s",
                    attempt,
                    retries,
                    result.returncode,
                    stderr,
                )
                last_error = KubectlError(
                    f"kubectl {' '.join(args[:2])} "
                    f"returned rc={result.returncode}: {stderr}"
                )
            except subprocess.TimeoutExpired:
                log.warning(
                    "kubectl timed out after %ds (attempt %d/%d)",
                    timeout,
                    attempt,
                    retries,
                )
                last_error = KubectlError(
                    f"kubectl {' '.join(args[:2])} timed out after {timeout}s"
                )
            except OSError as exc:
                raise KubectlError(
                    f"Failed to launch kubectl: {exc}"
                ) from exc

            if attempt < retries:
                delay = self.RETRY_DELAY * attempt
                log.debug("Retrying in %.1fs …", delay)
                time.sleep(delay)

        raise last_error

    # ------------------------------------------------------------------
    # Pod / container interactions
    # ------------------------------------------------------------------

    def exec(
        self,
        pod: str,
        command: list[str],
        *,
        container: Optional[str] = None,
        timeout: int = 120,
        stdin: Optional[str] = None,
    ) -> str:
        """Execute a command inside a running pod.

        Args:
            pod: Pod name.
            command: Command and arguments to run inside the pod.
            container: Target container (for multi-container pods).
            timeout: Command timeout in seconds.
            stdin: Optional stdin data.

        Returns:
            Command stdout.

        Raises:
            KubectlError: On execution failure.
        """
        args: list[str] = ["exec", pod]
        if container:
            args += ["-c", container]
        args += ["--"] + command
        log.debug("exec %s: %s", pod, " ".join(command))
        return self.run(args, timeout=timeout, stdin=stdin)

    def cp_to_pod(
        self,
        local_path: str | Path,
        pod: str,
        pod_path: str,
        *,
        container: Optional[str] = None,
        timeout: int = 300,
    ) -> None:
        """Copy a local path into a pod.

        Args:
            local_path: Source path on the local filesystem.
            pod: Destination pod name.
            pod_path: Destination path inside the pod.
            container: Optional container name.
            timeout: Transfer timeout in seconds.

        Raises:
            KubectlError: On failure.
        """
        args: list[str] = ["cp", str(local_path), f"{pod}:{pod_path}"]
        if container:
            args += ["-c", container]
        log.info("cp %s -> pod/%s:%s", local_path, pod, pod_path)
        self.run(args, timeout=timeout, retries=1)

    def cp_from_pod(
        self,
        pod: str,
        pod_path: str,
        local_path: str | Path,
        *,
        container: Optional[str] = None,
        timeout: int = 300,
    ) -> None:
        """Copy a path from a pod to the local filesystem.

        Args:
            pod: Source pod name.
            pod_path: Source path inside the pod.
            local_path: Destination path on the local filesystem.
            container: Optional container name.
            timeout: Transfer timeout in seconds.

        Raises:
            KubectlError: On failure.
        """
        args: list[str] = ["cp", f"{pod}:{pod_path}", str(local_path)]
        if container:
            args += ["-c", container]
        log.info("cp pod/%s:%s -> %s", pod, pod_path, local_path)
        self.run(args, timeout=timeout, retries=1)

    # ------------------------------------------------------------------
    # Resource listing
    # ------------------------------------------------------------------

    def list_pods(
        self, label_selector: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Return pods in the namespace, optionally filtered by label.

        Args:
            label_selector: Kubernetes label selector expression.

        Returns:
            List of pod resource dicts from the API.
        """
        args: list[str] = ["get", "pods", "-o", "json"]
        if label_selector:
            args += ["-l", label_selector]
        return self.json(args).get("items", [])

    def list_deployments(
        self, label_selector: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Return deployments in the namespace, optionally filtered by label.

        Args:
            label_selector: Kubernetes label selector expression.

        Returns:
            List of deployment resource dicts from the API.
        """
        args: list[str] = ["get", "deployments", "-o", "json"]
        if label_selector:
            args += ["-l", label_selector]
        return self.json(args).get("items", [])

    def list_services(
        self, label_selector: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Return services in the namespace, optionally filtered by label.

        Args:
            label_selector: Kubernetes label selector expression.

        Returns:
            List of service resource dicts from the API.
        """
        args: list[str] = ["get", "services", "-o", "json"]
        if label_selector:
            args += ["-l", label_selector]
        return self.json(args).get("items", [])

    # ------------------------------------------------------------------
    # AWX component pod resolution  (label-based, never by name)
    # ------------------------------------------------------------------

    def postgres_pod(self) -> str:
        """Return the name of the oldest Running PostgreSQL pod.

        Tries label selectors in order, then falls back to the pod-name prefix.

        Returns:
            Pod name.

        Raises:
            KubectlError: If no Running pod is found.
        """
        return self._find_pod(
            selectors=[
                "app.kubernetes.io/component=database",
                "app.kubernetes.io/name=postgres-15",
                "app.kubernetes.io/name=postgres",
            ],
            prefixes=["awx-postgres-"],
            description="PostgreSQL",
        )

    def web_pod(self) -> str:
        """Return the name of the oldest Running AWX web pod.

        Returns:
            Pod name.

        Raises:
            KubectlError: If no Running pod is found.
        """
        return self._find_pod(
            selectors=["app.kubernetes.io/name=awx-web"],
            prefixes=["awx-web-"],
            description="AWX web",
        )

    def task_pod(self) -> str:
        """Return the name of the oldest Running AWX task pod.

        Returns:
            Pod name.

        Raises:
            KubectlError: If no Running pod is found.
        """
        return self._find_pod(
            selectors=["app.kubernetes.io/name=awx-task"],
            prefixes=["awx-task-"],
            description="AWX task",
        )

    def operator_pod(self) -> str:
        """Return the name of the oldest Running AWX operator pod.

        Returns:
            Pod name.

        Raises:
            KubectlError: If no Running pod is found.
        """
        return self._find_pod(
            selectors=["control-plane=controller-manager"],
            prefixes=["awx-operator-controller-manager-"],
            description="AWX operator",
        )

    # ------------------------------------------------------------------
    # Secrets
    # ------------------------------------------------------------------

    def get_secret(self, name: str) -> dict[str, str]:
        """Retrieve and base64-decode a Kubernetes secret's data entries.

        Args:
            name: Secret name in the configured namespace.

        Returns:
            Dict mapping each data key to its decoded UTF-8 string value.

        Raises:
            KubectlError: If the secret does not exist or decoding fails.
        """
        resource = self.json(["get", "secret", name, "-o", "json"])
        raw: dict[str, str] = resource.get("data") or {}
        decoded: dict[str, str] = {}
        for key, b64value in raw.items():
            try:
                decoded[key] = base64.b64decode(b64value).decode("utf-8")
            except Exception as exc:
                raise KubectlError(
                    f"Failed to decode secret '{name}' key '{key}': {exc}"
                ) from exc
        return decoded

    # ------------------------------------------------------------------
    # Manifest management
    # ------------------------------------------------------------------

    def apply(self, manifest: str, *, timeout: int = 60) -> str:
        """Apply a YAML or JSON manifest passed as a string via stdin.

        Args:
            manifest: Manifest content (YAML or JSON text).
            timeout: Command timeout in seconds.

        Returns:
            kubectl apply output.

        Raises:
            KubectlError: On failure.
        """
        log.info("Applying manifest (%d bytes)", len(manifest))
        return self.run(
            ["apply", "-f", "-"],
            timeout=timeout,
            stdin=manifest,
            retries=1,
        )

    def delete(
        self,
        resource_type: str,
        name: str,
        *,
        ignore_not_found: bool = True,
        timeout: int = 60,
    ) -> str:
        """Delete a Kubernetes resource by type and name.

        Args:
            resource_type: Resource type, e.g. ``"secret"`` or ``"pod"``.
            name: Resource name.
            ignore_not_found: Suppress errors when the resource is absent.
            timeout: Command timeout in seconds.

        Returns:
            kubectl delete output.

        Raises:
            KubectlError: On failure (unless *ignore_not_found* suppresses it).
        """
        args: list[str] = ["delete", resource_type, name]
        if ignore_not_found:
            args.append("--ignore-not-found=true")
        log.info("Deleting %s/%s", resource_type, name)
        return self.run(args, timeout=timeout, retries=1)

    # ------------------------------------------------------------------
    # Scaling and rollouts
    # ------------------------------------------------------------------

    def scale(
        self,
        resource_type: str,
        name: str,
        replicas: int,
        *,
        timeout: int = 60,
    ) -> str:
        """Scale a workload to the desired replica count.

        Args:
            resource_type: Resource type, e.g. ``"deployment"``.
            name: Resource name.
            replicas: Target replica count.
            timeout: Command timeout in seconds.

        Returns:
            kubectl scale output.

        Raises:
            KubectlError: On failure.
        """
        log.info(
            "Scaling %s/%s to %d replica(s)", resource_type, name, replicas
        )
        return self.run(
            ["scale", resource_type, name, f"--replicas={replicas}"],
            timeout=timeout,
        )

    def rollout_restart(
        self,
        resource_type: str,
        name: str,
        *,
        timeout: int = 60,
    ) -> str:
        """Trigger a rolling restart of a deployment or statefulset.

        Args:
            resource_type: Resource type, e.g. ``"deployment"``.
            name: Resource name.
            timeout: Command timeout in seconds.

        Returns:
            kubectl rollout restart output.

        Raises:
            KubectlError: On failure.
        """
        log.info("Rolling restart of %s/%s", resource_type, name)
        return self.run(
            ["rollout", "restart", f"{resource_type}/{name}"],
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # Wait helpers
    # ------------------------------------------------------------------

    def wait_for_pod(
        self,
        label_selector: str,
        *,
        poll_interval: float = 5.0,
        timeout: int = 300,
    ) -> str:
        """Block until at least one pod matching the selector is Running.

        Args:
            label_selector: Kubernetes label selector expression.
            poll_interval: Seconds between polling attempts.
            timeout: Maximum total wait time in seconds.

        Returns:
            Name of the first Running pod found.

        Raises:
            KubectlError: If no pod becomes Running within *timeout* seconds.
        """
        log.info(
            "Waiting for Running pod (selector='%s', timeout=%ds)",
            label_selector,
            timeout,
        )
        deadline = time.monotonic() + timeout
        while True:
            pods = self._running_pods(label_selector)
            if pods:
                pod_name: str = pods[0]["metadata"]["name"]
                log.info("Pod '%s' is Running", pod_name)
                return pod_name
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise KubectlError(
                    f"Timed out after {timeout}s waiting for a Running pod "
                    f"with selector '{label_selector}' "
                    f"in namespace '{self.namespace}'"
                )
            sleep_for = min(poll_interval, remaining)
            log.debug(
                "Pod not Running yet; retrying in %.1fs (%.0fs remaining)",
                sleep_for,
                remaining,
            )
            time.sleep(sleep_for)

    def wait_until_gone(
        self,
        label_selector: str,
        *,
        poll_interval: float = 5.0,
        timeout: int = 300,
    ) -> None:
        """Block until no pods matching the selector remain.

        Args:
            label_selector: Kubernetes label selector expression.
            poll_interval: Seconds between polling attempts.
            timeout: Maximum total wait time in seconds.

        Raises:
            KubectlError: If pods are still present after *timeout* seconds.
        """
        log.info(
            "Waiting for pods to terminate (selector='%s', timeout=%ds)",
            label_selector,
            timeout,
        )
        deadline = time.monotonic() + timeout
        while True:
            pods = self.list_pods(label_selector=label_selector)
            if not pods:
                log.info(
                    "All pods with selector '%s' are gone", label_selector
                )
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise KubectlError(
                    f"Timed out after {timeout}s waiting for pods with selector "
                    f"'{label_selector}' to terminate "
                    f"in namespace '{self.namespace}'"
                )
            sleep_for = min(poll_interval, remaining)
            log.debug(
                "%d pod(s) still present; retrying in %.1fs (%.0fs remaining)",
                len(pods),
                sleep_for,
                remaining,
            )
            time.sleep(sleep_for)

    def wait_for_deployment(
        self,
        name: str,
        *,
        timeout: int = 300,
    ) -> None:
        """Block until a deployment rollout completes successfully.

        Args:
            name: Deployment name.
            timeout: Maximum total wait time in seconds.

        Raises:
            KubectlError: If the rollout does not complete within *timeout*.
        """
        log.info(
            "Waiting for deployment '%s' rollout (timeout=%ds)", name, timeout
        )
        self.run(
            [
                "rollout",
                "status",
                f"deployment/{name}",
                f"--timeout={timeout}s",
            ],
            timeout=timeout + 15,
            retries=1,
        )
        log.info("Deployment '%s' rollout complete", name)

    # ------------------------------------------------------------------
    # Namespace utilities
    # ------------------------------------------------------------------

    def namespace_exists(self, namespace: Optional[str] = None) -> bool:
        """Return True if a Kubernetes namespace exists.

        Args:
            namespace: Namespace to query. Defaults to the instance namespace.

        Returns:
            True if the namespace is present, False otherwise.
        """
        target = namespace if namespace is not None else self.namespace
        try:
            self.run(
                ["get", "namespace", target],
                namespaced=False,
                timeout=10,
                retries=1,
            )
            return True
        except KubectlError:
            return False

    # ------------------------------------------------------------------
    # Structured output helpers
    # ------------------------------------------------------------------

    def json(self, args: list[str], **kwargs: Any) -> dict[str, Any]:
        """Run kubectl and parse stdout as JSON.

        Args:
            args: kubectl arguments.
            **kwargs: Forwarded to :meth:`run`.

        Returns:
            Parsed JSON document as a dict.

        Raises:
            KubectlError: On kubectl failure or JSON parse error.
        """
        output = self.run(args, **kwargs)
        try:
            parsed: dict[str, Any] = json.loads(output)
            return parsed
        except json.JSONDecodeError as exc:
            raise KubectlError(
                f"kubectl JSON parse error: {exc}. "
                f"Output snippet: {output[:200]!r}"
            ) from exc

    def yaml(self, args: list[str], **kwargs: Any) -> str:
        """Run kubectl and return the raw YAML output as a string.

        Appends ``-o yaml`` to *args* when no output flag is already present.
        Intended for serialising resource state to disk during backups.

        Args:
            args: kubectl arguments.
            **kwargs: Forwarded to :meth:`run`.

        Returns:
            Raw YAML string produced by kubectl.

        Raises:
            KubectlError: On kubectl failure.
        """
        args = list(args)
        if "-o" not in args and "--output" not in args:
            args += ["-o", "yaml"]
        return self.run(args, **kwargs)
