# ClipStream — Management Commands Reference

All commands are run from the project root with the virtualenv active:

```bash
python manage.py <command> [options]
```

---

## `assign_role`

Assign or view roles (`viewer` / `editor` / `superadmin`) for ClipStream users.

| Option | Description |
|---|---|
| `--username USERNAME` | Username to update (repeatable for multiple users) |
| `--role viewer\|editor\|superadmin` | Role to assign |
| `--list` | Print all users and their current roles, then exit |

**Notes:**
- `superadmin` also grants `is_staff` + `is_superuser` (Django admin access).
- Demoting from `superadmin` revokes Django admin access automatically.

```bash
python manage.py assign_role --username soham --role editor
python manage.py assign_role --username soham --username priya --role viewer
python manage.py assign_role --list
```

---

## `ingest_videos`

Scans `media/originals/` for video files not yet in the database, creates `Video` records, and kicks off HLS processing via Celery.

| Option | Description |
|---|---|
| `--channel "Name"` | Target channel by name (case-insensitive) |
| `--channel-slug SLUG` | Target channel by slug (mutually exclusive with `--channel`) |
| `--subdir DIR` | Subdirectory inside `media/originals/` to scan |
| `--visibility public\|private\|subscribers` | Visibility for new videos (default: `public`) |
| `--dry-run` | List files that would be ingested without creating records |
| `--reprocess` | Also re-queue processing for files already in the DB |
| `--sync` | Process in a thread instead of Celery (no worker needed) |

**Supported extensions:** `.mp4 .mkv .mov .avi .wmv .flv .webm .m4v .ts .mts`

```bash
python manage.py ingest_videos --channel "Essar Essentials"
python manage.py ingest_videos --channel "Essar Essentials" --dry-run
python manage.py ingest_videos --channel "Essar Essentials" --reprocess --sync
```

---

## `reanalyse_videos`

Re-queues the `analyze_video_frames_task` Celery task for ready videos. Does **not** re-encode — only re-runs YOLO, BLIP/Florence-2, CLIP, and InsightFace on already-saved frames.

Use when you:
- Add a new AI model and want embeddings/captions for existing uploads
- Change `FRAME_INTERVAL_SECONDS` or other analysis settings
- Fix a bug in face clustering or scene captioning

| Option | Description |
|---|---|
| `--video-id UUID` | Re-analyse a single video (omit to process all ready videos) |
| `--include-status STATUS` | Comma-separated statuses to include (default: `ready`) |
| `--dry-run` | Show what would be queued without queuing |
| `--sync` | Run tasks in this process synchronously (no Celery needed) |

```bash
python manage.py reanalyse_videos
python manage.py reanalyse_videos --video-id <uuid>
python manage.py reanalyse_videos --dry-run
python manage.py reanalyse_videos --sync
```

---

## `regenerate_captions`

Queues `generate_captions_task` for videos missing auto-generated captions (or all videos with `--force`).

| Option | Description |
|---|---|
| `--video-id UUID` | Target a single video |
| `--force` | Delete existing auto-captions first, then regenerate |
| `--language CODE` | Language code to pass to Whisper (default: `en`) |
| `--dry-run` | Preview without queuing |
| `--sync` | Run synchronously in this process |

```bash
python manage.py regenerate_captions
python manage.py regenerate_captions --force
python manage.py regenerate_captions --video-id <uuid> --language fr
```

---

## `propagate_identities`

After manually naming a person on `/faces/`, run this to merge any auto-named identities (`Person #N`) that are the same person based on face embedding similarity.

**Workflow:**
1. Upload videos → face recognition creates `Person #42`, `Person #137`, etc.
2. Go to `/faces/` → find the right person → rename to "Kashish".
3. Reject bad crops on `/faces/<id>/`.
4. Run `propagate_identities` → other auto-named identities that look like Kashish get merged.

| Option | Description |
|---|---|
| `--threshold FLOAT` | Cosine similarity threshold for a match (default: `0.45`). Higher = stricter. |
| `--dry-run` | Preview merges without writing changes |

```bash
python manage.py propagate_identities
python manage.py propagate_identities --threshold 0.50
python manage.py propagate_identities --dry-run
```

---

## `auto_confirm_similar`

Retroactively auto-confirms `UNREVIEWED` face crops that are already very close to their identity's reference embedding (cosine similarity ≥ threshold). Handles crops ingested before auto-confirm logic was in place.

| Option | Description |
|---|---|
| `--threshold FLOAT` | Cosine similarity threshold (default: `FACE_AUTO_CONFIRM_THRESHOLD` from settings, currently `0.82`) |
| `--video-id UUID` | Scope to a single video |
| `--identity-id INT` | Scope to a single `FaceIdentity` |
| `--dry-run` | Preview without writing changes |

```bash
python manage.py auto_confirm_similar
python manage.py auto_confirm_similar --threshold 0.85 --dry-run
python manage.py auto_confirm_similar --video-id <uuid>
```

---

## `rename_auto_identities`

One-time migration: renames all auto-named `FaceIdentity` rows that use the old per-video `Person N` scheme (e.g. "Person 1", "Person 2") to the globally unique `Person #<pk>` scheme.

Only touches rows where `is_auto_named=True` and name matches `Person <digits>`. Named identities are never changed.

| Option | Description |
|---|---|
| `--dry-run` | Preview changes without writing to the database |

```bash
python manage.py rename_auto_identities --dry-run   # preview
python manage.py rename_auto_identities             # apply
```

---

## `fill_blip_descriptions`

Finds videos whose `VideoFrame` rows all have an empty BLIP/Florence-2 scene description and re-queues the full frame-analysis task for those videos only.

**Root cause it fixes:** An old filter in `tasks.py` only saved frames where YOLO or InsightFace detected something — frames with no detections were silently dropped, taking their BLIP captions and CLIP embeddings with them. That filter has been patched, but this command backfills any videos already affected.

Because frame images are stored in a temporary directory during analysis (not persisted), re-running the full task is required. This means YOLO, BLIP/Florence-2, CLIP, and InsightFace all re-run — not just the caption model.

| Option | Description |
|---|---|
| `--dry-run` | Show which videos would be re-queued without doing anything |
| `--sync` | Run tasks synchronously in this process (no Celery needed) |

```bash
python manage.py fill_blip_descriptions --dry-run   # preview affected videos
python manage.py fill_blip_descriptions             # queue to Celery worker
python manage.py fill_blip_descriptions --sync      # run inline, no Celery
```

---

## `patch_master_playlists`

Rewrites `master.m3u8` for every ready multi-quality video to include the `RESOLUTION` attribute — needed so hls.js can populate `level.height` and display correct quality labels in the player. **No re-encoding.**

| Option | Description |
|---|---|
| `--video-id UUID` | Patch a single video (omit to patch all ready videos) |

```bash
python manage.py patch_master_playlists
python manage.py patch_master_playlists --video-id <uuid>
```

---

## `generate_seek_sprites`

Backfills seek-bar thumbnail sprite sheets for existing videos. New videos get sprites automatically during processing — use this command for older uploads or after changing sprite settings.

See [SEEK_SPRITES.md](SEEK_SPRITES.md) for full documentation on how sprites work, storage, and configuration.

| Option | Description |
|---|---|
| `--video-id UUID` | Generate sprite for a single video |
| `--channel SLUG` | Generate sprites for all ready videos in a channel |
| `--force` | Regenerate even if a sprite already exists on disk |
| `--dry-run` | Preview which videos would be processed without running FFmpeg |
| `--sync` | Run FFmpeg in this process synchronously (no Celery worker needed) |

Without any scope flag, targets all ready videos that are missing a sprite.

```bash
python manage.py generate_seek_sprites                          # backfill all missing
python manage.py generate_seek_sprites --channel my-channel    # one channel
python manage.py generate_seek_sprites --video-id <uuid>       # one video
python manage.py generate_seek_sprites --force                 # regenerate all
python manage.py generate_seek_sprites --dry-run               # preview only
python manage.py generate_seek_sprites --sync                  # no Celery needed
```

---

## Fix missing BLIP/CLIP descriptions

```bash
# See which videos have no scene descriptions
python manage.py fill_blip_descriptions --dry-run

# Re-run full analysis (YOLO + BLIP + CLIP + faces) for affected videos
python manage.py fill_blip_descriptions --sync
```

---

## Face Recognition Workflow (recommended order)

```bash
# 1. After uploading new videos — re-run analysis if needed
python manage.py reanalyse_videos --video-id <uuid>

# 2. On /faces/ — rename people, reject bad crops

# 3. Confirm high-confidence unreviewed crops in bulk
python manage.py auto_confirm_similar --dry-run
python manage.py auto_confirm_similar

# 4. Propagate named identities to remaining auto-named ones
python manage.py propagate_identities --dry-run
python manage.py propagate_identities

# One-time: fix old "Person N" names to globally unique "Person #<pk>"
python manage.py rename_auto_identities
```
