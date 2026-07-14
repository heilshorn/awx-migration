# AWX Migration

Tools for backing up and restoring AWX installations on Kubernetes.

## Restoring an HTTP Execution Environment registry (`--restore-registry`)

When restoring with `--restore-registry` and the target registry serves plain
**HTTP** (e.g. a local NodePort registry), the restore configures k3s/containerd
so it can pull the Execution Environment images. Before AWX is scaled back up it:

1. restores the registry and derives the target address (`--registry-to`, or the
   restored NodePort service);
2. ensures `/etc/rancher/k3s/registries.yaml` contains a mirror and an
   insecure-TLS config for that host:

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

   Existing entries are never overwritten — a missing mirror is created or an
   absent endpoint appended;
3. if the file changed, backs it up (`registries.yaml.bak-<timestamp>`), restarts
   k3s (`systemctl restart k3s`), and waits for the node to become `Ready` again
   before scaling AWX up.

This step is skipped entirely without `--restore-registry` and for HTTPS target
registries. Because it writes under `/etc/rancher/k3s` and restarts k3s, the
restore must run with sufficient privileges (root); `PyYAML` is required for this
path (see `requirements.txt`).

See [docs/k3s-registry-mirror.md](docs/k3s-registry-mirror.md) for the full
behavior, non-destructive merge rules, prerequisites, and troubleshooting.

