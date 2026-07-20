# AWX Migration

Tools for backing up and restoring AWX installations on Kubernetes, plus
exporting and importing individual AWX objects for migration and version
control.

## AWX Object Export / Import

`awx_export.py` and `awx_import.py` export and import **individual AWX objects**
as versioned JSON bundles. They currently support the implemented AWX object
types — **Organizations, Inventories, Projects and Job Templates** — and the set
can be extended by adding a registry entry. Unlike the full backup/restore, this
is meant for **migration and version control**, not disaster recovery — the
bundles contain **no secrets**.

Objects and their references are expressed through **natural keys** (names),
never internal database IDs, so a bundle is portable across AWX instances and
readable in git.

### Exporting

```bash
# export everything (all supported types)
./awx_export.py --all --output my-export/

# export selected types, or a single named object
./awx_export.py --type job_templates --type inventories
./awx_export.py --type job_templates --name "Deploy"

# restrict to one organization; or just list organizations
./awx_export.py --all --organization Default
./awx_export.py --organization ls
```

The connection is derived from the AWX NodePort service by default, or given
explicitly with `--awx-host` plus `--awx-token` (or `--awx-username` /
`--awx-password`). Add `--archive` to also produce a `.tar.gz`.

### Importing

```bash
./awx_import.py my-export/                       # import a bundle directory
./awx_import.py my-export/ --type job_templates  # only one type
./awx_import.py my-export/ --on-conflict skip    # update (default) | skip | fail
```

The importer validates the bundle first and imports types in dependency order
(organizations → projects/inventories → job templates).

### End-to-end tests (opt-in)

Unit tests run without AWX. The end-to-end tests under `tests/e2e/` drive a real
AWX instance and are skipped unless `AWX_E2E_HOST` (and the `awx` CLI) are
available:

```bash
AWX_E2E_HOST=https://awx.example:30080 AWX_E2E_TOKEN=… pytest tests/e2e -v
```

They self-provision and clean up their test data. The job-template test needs
AWX to sync a git project — see the proxy note under Limitations.

## Limitations

- The object export format is intended for **migrating AWX objects** and is
  **not a full backup**. For disaster recovery, use the backup/restore tools.
- **Secrets, passwords and other sensitive data are deliberately not exported.**
  Credential secrets must be re-entered after import.
- References between objects are resolved via **AWX natural keys** (names), not
  internal database IDs.
- For SCM-backed projects, the **AWX execution environment must be able to reach
  the repository**. When AWX is deployed behind a corporate proxy, a proxy on
  the host is not enough — the execution environment itself must receive the
  required proxy variables (for example via **`AWX_TASK_ENV`**: Settings → Job
  Settings → "Extra Environment Variables" with `HTTP_PROXY` / `HTTPS_PROXY` /
  `NO_PROXY`).

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

