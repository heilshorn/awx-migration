# k3s Registry Mirror for HTTP Execution Environment Registries

## Overview

When AWX is restored together with its Execution Environment (EE) registry using
`--restore-registry`, the EE images are pushed into a registry running inside the
target cluster and exposed via a `NodePort`. If that registry serves **plain
HTTP** (the common case for a local NodePort registry), k3s/containerd refuses to
pull from it unless it is told to — the images are healthy, but every AWX pod that
needs an EE image ends up in `ImagePullBackOff`.

k3s reads per-registry pull settings from `/etc/rancher/k3s/registries.yaml`.
Containerd only pulls over HTTP (or accepts a self-signed certificate) from a host
that is declared there as a **mirror** with an **insecure TLS** config.

To make the restore self-contained, the restore tool detects this situation and
configures the mirror automatically **before** AWX is scaled back up, so the pods
can pull their EE image on first start.

This document describes what the feature does, when it activates, and how to
operate and troubleshoot it. Implementation lives in
[`lib/k3s_registry.py`](../lib/k3s_registry.py).

## When it activates

All of the following must be true:

1. The restore is run with **`--restore-registry`**.
2. The target registry serves **plain HTTP** — verified at runtime with a
   proxy-free `GET http://<host:port>/v2/` that returns HTTP `200` or `401`.

If either condition is not met, `registries.yaml` is left completely untouched and
k3s is **not** restarted. In particular:

- A restore **without** `--restore-registry` never touches k3s.
- A restore against an **HTTPS** target registry never touches k3s.

## Where it runs in the restore flow

The registry restore and the k3s configuration run while AWX is still scaled to
zero, *before* AWX is scaled back up. The relevant ordering in
[`awx_restore.py`](../awx_restore.py):

1. Restore PostgreSQL and synchronise the role password.
2. **Restore the registry** (apply manifests, push EE images) and derive the
   target address — either `--registry-to` or `<node-ip>:<nodePort>` read from the
   restored NodePort service.
3. **Ensure the k3s mirror** for the target address (this feature).
4. **Scale AWX back up** and wait for the `awx-web` pod.
5. Rewrite the EE image registry prefixes in the AWX database.

Because the mirror is in place before step 4, `awx-web` / `awx-task` can pull their
control-plane EE image immediately, and later jobs can pull their job EE images.

> The EE rewrite (step 5) intentionally stays *after* scale-up because it needs the
> AWX API (`awx-manage shell`) inside the running web pod.

## What gets written

The feature ensures the following two blocks exist in
`/etc/rancher/k3s/registries.yaml`, keyed by the target registry `host:port`:

```yaml
mirrors:
  "<registry-host:port>":
    endpoint:
      - "http://<registry-host:port>"

configs:
  "<registry-host:port>":
    tls:
      insecure_skip_verify: true
```

- **`mirrors.<host>.endpoint`** tells containerd to pull from the given HTTP
  endpoint for that host.
- **`configs.<host>.tls.insecure_skip_verify`** allows the HTTP / self-signed
  endpoint to be used without certificate verification.

## Non-destructive merge

Existing entries are **never overwritten**. The merge only ever *adds* what is
missing:

| Situation                                             | Action                                                        |
| ----------------------------------------------------- | ------------------------------------------------------------- |
| File missing or empty                                 | Created with the two blocks above.                            |
| Host not present in `mirrors`                         | Host added with the HTTP endpoint.                            |
| Host present, but the HTTP endpoint not listed        | Endpoint **appended**; existing endpoints kept.               |
| Host present, endpoint already listed                 | No change.                                                    |
| Host not present in `configs`                         | Host added with `tls.insecure_skip_verify: true`.             |
| `configs.<host>.tls.insecure_skip_verify` already set | **Left as-is** — even if it is `false` (operator's choice).   |
| Other hosts / keys (e.g. `docker.io`, `auth`)         | Preserved untouched.                                          |

If, after merging, nothing changed, the file is not rewritten and k3s is **not**
restarted.

## Restart and readiness

When (and only when) the file changed:

1. The current file is backed up to `registries.yaml.bak-<UTC-timestamp>` in the
   same directory.
2. The new file is written.
3. **`systemctl restart k3s`** is executed so k3s re-reads the configuration.
4. The tool waits (up to 300 s) until the cluster node(s) report `Ready` again,
   tolerating the brief API-server outage during the restart.

AWX is then scaled back up (it was at zero during the restart) and the restore
waits for the `awx-web` / `awx-task` pods to reach `Running`.

## Prerequisites

- **Run as root.** Writing under `/etc/rancher/k3s` and running
  `systemctl restart k3s` require privileges. Insufficient privileges produce a
  clear error instructing you to re-run as root.
- **PyYAML** must be installed (`pip install -r requirements.txt`). It is only
  needed for this HTTP-registry path; the rest of the toolchain has no external
  Python dependencies.
- **`skopeo` or `crane`** must be in `PATH` for the registry image transfer (this
  is a requirement of `--restore-registry` in general, not of this feature).

## Idempotency

Re-running the restore is safe. If the mirror is already present and correct, the
merge reports no change, the file is not rewritten, and k3s is not restarted.

## Troubleshooting

**AWX pods stay in `ImagePullBackOff` after the restore.**
Confirm the mirror was written:

```bash
sudo cat /etc/rancher/k3s/registries.yaml
```

The target `host:port` should appear under both `mirrors` and `configs`. Check the
restore log for `Ensuring k3s registry mirror ...`. If it logged
`does not serve plain HTTP`, the registry answered on HTTPS (or was unreachable) at
probe time — the mirror is not needed / not applicable for HTTP.

**"Insufficient privileges to write ..." or "systemctl restart k3s failed".**
Re-run the restore as root (or via `sudo`).

**Node does not become `Ready` after the restart.**
Inspect k3s directly on the node:

```bash
sudo systemctl status k3s
sudo journalctl -u k3s -e
sudo kubectl get nodes
```

**PyYAML missing.**
`pip install -r requirements.txt` (or `pip install PyYAML`).

## Manual equivalent

If you prefer to configure this by hand (or on nodes the tool does not run on),
the equivalent manual steps are:

```bash
sudo cp /etc/rancher/k3s/registries.yaml \
        /etc/rancher/k3s/registries.yaml.bak 2>/dev/null || true

sudo tee -a /etc/rancher/k3s/registries.yaml >/dev/null <<'EOF'
mirrors:
  "REGISTRY_HOST:PORT":
    endpoint:
      - "http://REGISTRY_HOST:PORT"
configs:
  "REGISTRY_HOST:PORT":
    tls:
      insecure_skip_verify: true
EOF

sudo systemctl restart k3s
sudo kubectl wait --for=condition=Ready nodes --all --timeout=300s
```

> Note: appending with `tee -a` only works cleanly when the file has no existing
> `mirrors:` / `configs:` blocks. When it does, merge the entries into the existing
> blocks by hand — YAML does not allow a top-level key to appear twice. The restore
> tool performs exactly this structured, non-destructive merge for you.
