# Storage admin guide

ClipLens stores tenant media on the local filesystem by default, but each tenant
can be pointed at any absolute path the operating system can reach: a network
share, a remote DAM via SSHFS, or a cloud bucket mounted with FUSE. This doc
covers the architecture, the migration flow, and recipes for the common
remote-storage scenarios.

---

## 1. Architecture in one paragraph

Each tenant has a `Tenant.media_root_absolute` field (control DB). When empty,
files live at `MEDIA_ROOT/tenants/<slug>/`. When set, files live at that
absolute path — no other code change needed because both `TenantMiddleware` and
the Celery context handler set a thread-local that the storage backend, URL
generator, and tenant-aware path helpers all consult.

The URL prefix (`/media/tenants/<slug>/...`) stays **the same regardless of
where files physically live**. `protected_media` translates the URL to the real
on-disk location at serve time, so links never break when storage moves.

Two helpers do the path translation in Python:

- `tenants.storage.to_storage_path(absolute_path)` — convert an absolute path
  that a Celery task just wrote to the storage-form value used in CharFields
  (`hls_path`, `seek_sprite`, `crop_path`).
- `tenants.storage.from_storage_path(stored)` — the inverse, used by readers
  that need the absolute path on disk.

`FileField`/`ImageField` paths use `TenantFileSystemStorage` automatically —
`.path` and `.url` both resolve correctly without any extra plumbing.

---

## 2. The relocation flow

The platform-owner UI on the tenant detail page handles end-to-end migration:

1. **Pre-flight checks** — target must be an absolute path; parent directory
   exists; target itself is empty or doesn't exist; target is writable; free
   space is at least `source_size × 1.10`.
2. **Maintenance mode** — `tenant.media_relocating=True` makes
   `TenantMiddleware` return a 503 maintenance page for every request on that
   tenant's subdomain. The control plane (admin subdomain) is unaffected.
3. **Copy + verify** — `shutil.copy2` per file (preserves mtime), throttled
   progress saves once per second. After the copy, the byte count and file
   count are re-walked at the target and compared to the source.
4. **Atomic swap** — old root is renamed to `<old>.delete_after_<timestamp>`,
   then `Tenant.media_root_absolute` is updated in one transaction.
5. **Grace period** — the renamed old directory sits untouched for
   `MEDIA_RELOCATE_GRACE_HOURS` (default 24). The daily Celery beat task
   `purge_expired_media_relocations` deletes it after expiry. An admin can
   purge immediately from the UI if confident.

The `MediaRelocation` table audits every attempt: who started it, when, source
and target paths, status (queued/running/verifying/succeeded/failed/cancelled),
byte and file counters, errors, and grace-period timestamps.

### Cancellation

- **Graceful cancel** sets `media_relocation_cancel_requested=True`. The
  running task notices between files, aborts, deletes the partial copy at the
  target, and clears the relocating flag. Use this whenever the worker is
  still responsive.
- **Force cancel** kills the Celery task via `app.control.revoke` and clears
  the flag immediately. The target directory is left in whatever state it was
  in — an admin must inspect and clean up manually. Use only when the worker
  is wedged.

### What's NOT migrated

- The PostgreSQL tenant database — that's separate infrastructure. The
  relocation only moves media files.
- Anything outside `tenant.media_folder` — global `live/` HLS segments, model
  caches, app code.

---

## 3. Storage recipes

### Local SSD (default)

Nothing to configure. Files land under `<BASE_DIR>/media/tenants/<slug>/`.

### Mounted NAS or shared volume

Mount the share at the OS level using standard tooling — `mount`, `/etc/fstab`,
autofs, whatever your distro prefers. Then paste the mount point into the
tenant's custom media path field.

```bash
# Example: NFS share
sudo mkdir -p /mnt/cliplens-acme
sudo mount -t nfs nas.internal:/exports/cliplens/acme /mnt/cliplens-acme

# Make it survive reboots — /etc/fstab
nas.internal:/exports/cliplens/acme  /mnt/cliplens-acme  nfs  defaults,_netdev  0  0
```

For SMB/CIFS, swap `nfs` for `cifs` and add `username=`, `password=` (better:
`credentials=/etc/cifs-cred-file`) options.

### Remote DAM via SSHFS

Use when you have an existing media server reachable over SSH but no
file-share protocol.

```bash
# 1. Install (Linux)
sudo apt-get install sshfs
# Install (macOS)
brew install --cask macfuse
brew install gromgit/fuse/sshfs-mac

# 2. Set up SSH key auth — credentials NEVER live in ClipLens
ssh-keygen -t ed25519
ssh-copy-id clip@dam.example.com

# 3. Create the remote target
ssh clip@dam.example.com 'mkdir -p /srv/dam/cliplens/acme'

# 4. Create local mount point + mount
sudo mkdir -p /mnt/dam-acme
sudo chown $(whoami) /mnt/dam-acme

sshfs clip@dam.example.com:/srv/dam/cliplens/acme /mnt/dam-acme \
  -o reconnect,ServerAliveInterval=15,ServerAliveCountMax=3,allow_other,IdentityFile=$HOME/.ssh/id_ed25519

# 5. Plug /mnt/dam-acme into the tenant detail page → custom media path
```

Persist across reboots — `/etc/fstab`:

```
clip@dam.example.com:/srv/dam/cliplens/acme  /mnt/dam-acme  fuse.sshfs  defaults,_netdev,reconnect,ServerAliveInterval=15,IdentityFile=/home/clip/.ssh/id_ed25519,allow_other  0  0
```

### Cloud object store (S3, GCS, Azure, R2, Backblaze, etc.)

Mount the bucket as a local path using `rclone mount` (any provider) or
`s3fs-fuse` (S3 only). Treat it the same as any other absolute path.

```bash
# rclone — supports every major provider
rclone config             # one-time: configure remote credentials
rclone mount mycloud:cliplens-acme /mnt/cliplens-acme \
  --vfs-cache-mode full \
  --vfs-cache-max-size 50G \
  --dir-cache-time 1h \
  --daemon
```

A local cache (`--vfs-cache-mode full`) is **required** for usable
performance — HLS playback issues many small random reads that round-trip too
slowly over cold cloud storage.

---

## 4. Performance characteristics

| Storage | Random read latency | Throughput | HLS playback | Best for |
|---|---|---|---|---|
| Local SSD | < 1 ms | 1+ GB/s | Excellent | Hot working set, active editing |
| NFS / SMB on LAN | 1–5 ms | 100 MB/s+ | Good | On-prem multi-server deployments |
| SSHFS (LAN) | 5–20 ms | 50–200 MB/s | Acceptable | Off-site DAM, hybrid setups |
| Cloud + FUSE cache | 1–50 ms (cached); 200+ ms (cold) | Variable | Acceptable with cache, poor without | Archive, elastic scaling |
| Cloud + FUSE no cache | 200+ ms | Provider-dependent | **Avoid** | Bulk storage only, not playback |

Two practical rules:

- **Originals can live anywhere.** They're written once, read in bulk during
  processing — slow storage is acceptable.
- **HLS segments need fast reads.** Hundreds of tiny `.ts` files per video,
  random access during seek. Keep these on fast storage or with a generous
  local cache.

For high-traffic deployments consider a **hybrid layout**: process on local
disk, then move cold HLS to cloud after N days via lifecycle policy. ClipLens
doesn't manage this automatically yet — it's a deployment-level concern.

---

## 5. Operational notes

### Pre-flight check failures

The relocate UI surfaces the failure inline. Common causes:

- **Path is empty / Target must be absolute** — paste a full `/foo/bar` path,
  not a relative one.
- **Parent directory does not exist** — create or mount the parent first.
- **Target directory exists but is not empty** — relocations refuse to overwrite.
- **Target is not writable** — usually a permission or mount issue. Test with
  `sudo -u <user-running-cliplens> touch /target/.t && rm /target/.t`.
- **Insufficient free space** — relocation requires `source_size × 1.10`.

### What the maintenance page looks like

During a relocation the tenant subdomain returns a 503 with a friendly auto-
refreshing page. Logged-in users get bounced, ongoing requests get cleanly
terminated. The control plane (admin subdomain) is unaffected — you can keep
monitoring progress, cancel, or force-cancel.

### Recovering from a failed relocation

Failed relocations leave the tenant's `media_root_absolute` unchanged (the swap
only happens after verification succeeds). The `MediaRelocation` row records
the failure with the error message. Partial files at the target are
auto-cleaned on most failures — force-cancel is the only path that leaves
partial data behind.

Re-run the relocation after fixing the underlying issue.

### Cross-volume rename

If source and target sit on different filesystems, the post-copy `os.rename`
of the old directory to its soft-delete name fails with `EXDEV`. The
relocation handles this by leaving the source in place and treating it as
already-renamed — purge-now will then delete the source directory directly
after the grace period.

### What credentials live where

| Credential | Where |
|---|---|
| SSH private key (SSHFS) | Filesystem, OS-level, e.g. `~/.ssh/id_ed25519` |
| SMB username/password | `/etc/cifs-creds` referenced from `/etc/fstab` |
| Cloud bucket creds (rclone, s3fs) | `rclone.conf` or `~/.passwd-s3fs` |
| ClipLens app | **Never** holds any of the above. |

The strict separation means: if ClipLens is compromised, the attacker still
needs OS-level access to read your storage credentials. And rotating
credentials is an OS task, not a ClipLens deploy.

---

## 6. Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `MEDIA_RELOCATE_GRACE_HOURS` | `24` | How long the soft-deleted old data is kept after a successful relocation before the daily beat task purges it. Increase if you want a longer rollback window. |

---

## 7. Code map

| File | What it does |
|---|---|
| `tenants/models.py` (Tenant fields, MediaRelocation) | Schema for custom path + audit log |
| `tenants/storage.py` | `TenantFileSystemStorage`, thread-local root, `to_storage_path` / `from_storage_path` |
| `tenants/middleware.py` | Reads `media_root_absolute`, sets thread-local, serves maintenance page |
| `tenants/celery_utils.py` | Same setup for Celery tasks via `tenant_slug` kwarg |
| `tenants/media_serve.py` | `protected_media` URL handler — serves from custom root |
| `tenants/media_relocate.py` | Pre-flight, copy + verify, atomic swap, grace purge |
| `tenants/tasks.py` | Celery wrappers: `relocate_tenant_media`, `purge_expired_media_relocations` |
| `tenants/views.py` (queue/status/cancel/force-cancel/purge) | HTTP endpoints |
| `tenants/templates/tenants/tenant_detail.html` | UI card with live progress polling |
| `cliplens/celery.py` | Beat schedule (daily 03:17 UTC) |
