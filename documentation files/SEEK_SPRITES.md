# Seek Sprite Thumbnails

Seek sprites provide YouTube-style hover/scrub preview thumbnails on the video seek bar. When a viewer hovers over or drags the playhead, a small thumbnail card appears above the bar showing the frame at that point in time.

---

## How it works

A **sprite sheet** is a single JPEG file containing many video frames tiled in a grid. Instead of making dozens of individual image requests (one per frame), the browser loads one image and uses CSS `background-position` to crop the correct tile.

```
┌────────────────────────────────────────────────────────────────┐
│  frame 0s  │  frame 5s  │  frame 10s │  ...  │  frame 120s   │  ← row 0
├────────────────────────────────────────────────────────────────┤
│  frame 125s│  frame 130s│  ...                                 │  ← row 1
└────────────────────────────────────────────────────────────────┘
```

The JS player reads the tile dimensions and grid layout, then calculates which tile to show based on the current cursor/drag position.

---

## Generation

Sprites are generated using **FFmpeg's `tile` filter** in a single pass — no intermediate frames are saved to disk.

**FFmpeg command used:**
```bash
ffmpeg -i <original_file> \
  -vf "fps=1/5,scale=160:90,tile=25x3" \
  -frames:v 1 -q:v 4 -y \
  media/seek_sprites/<video_id>.jpg
```

| Filter part | What it does |
|---|---|
| `fps=1/5` | Samples one frame every 5 seconds |
| `scale=160:90` | Resizes each tile to 160×90 px (16:9) |
| `tile=COLSxROWS` | Stitches tiles into a grid |
| `-frames:v 1` | Outputs exactly one image (the completed sprite) |
| `-q:v 4` | JPEG quality (1=best, 31=worst) |

Row count is calculated automatically from `ceil(duration ÷ interval ÷ cols)` — e.g. a 5-minute video at 5-second intervals and 25 columns produces `tile=25x3` (60 frames ÷ 25 = 3 rows).

---

## Storage

| Path | Description |
|---|---|
| `media/seek_sprites/<video_id>.jpg` | The sprite sheet file |
| `Video.seek_sprite` | Relative media path stored in the database (e.g. `seek_sprites/<uuid>.jpg`) |

Sprites are **not** regenerated automatically when you change settings — use the management command to backfill.

---

## Configuration (`.env`)

All settings have sensible defaults and are fully optional.

| Variable | Default | Description |
|---|---|---|
| `SEEK_THUMBNAILS_ENABLED` | `true` | Master on/off switch. Set to `false` to disable entirely. |
| `SEEK_THUMBNAIL_INTERVAL` | `5` | Seconds between tiles. Lower = smoother scrub, larger file. |
| `SEEK_THUMBNAIL_WIDTH` | `160` | Width of each tile in pixels. |
| `SEEK_THUMBNAIL_HEIGHT` | `90` | Height of each tile in pixels. Should be `width × 9/16` for 16:9. |
| `SEEK_THUMBNAIL_COLS` | `25` | Number of tile columns in the sprite grid. |
| `SEEK_THUMBNAIL_QUALITY` | `4` | FFmpeg JPEG quality (1=best/largest, 31=worst/smallest). |

### Tuning guide

**For fast scrubbing (smoother preview):** lower `SEEK_THUMBNAIL_INTERVAL` to `2` or `3`. File size grows proportionally.

**For lower storage cost:** raise `SEEK_THUMBNAIL_INTERVAL` to `10`. Less precise scrub.

**For higher-resolution tiles:** increase `SEEK_THUMBNAIL_WIDTH`/`HEIGHT`. Remember to also update `SEEK_THUMBNAIL_COLS` if you want the sprite to stay within a sensible file size.

**Typical file sizes at defaults (160×90, 5s interval, q=4):**

| Video length | Approx. sprite size |
|---|---|
| 5 minutes | ~40 KB |
| 30 minutes | ~240 KB |
| 1 hour | ~480 KB |
| 3 hours | ~1.4 MB |

---

## New video pipeline

Sprites are generated automatically for every new video as part of the standard processing pipeline:

```
process_video_task
  → FFmpeg HLS encode
  → frame analysis (YOLO, BLIP, CLIP, InsightFace)
  → generate_seek_thumbnails_task   ← runs 15s after processing completes
```

The 15-second delay ensures the original file is fully written before FFmpeg reads it.

If `SEEK_THUMBNAILS_ENABLED=false` at the time of processing, the task exits immediately and no sprite is generated. You can backfill later with the management command.

---

## Backfilling existing videos

Use the `generate_seek_sprites` management command to generate sprites for videos that were ingested before this feature was added, or to regenerate sprites after changing settings.

```bash
# Backfill all ready videos that don't have a sprite yet
python manage.py generate_seek_sprites

# Single video
python manage.py generate_seek_sprites --video-id <uuid>

# All videos in a channel
python manage.py generate_seek_sprites --channel <slug>

# Force regenerate even if a sprite already exists
python manage.py generate_seek_sprites --force

# Preview what would run without executing FFmpeg
python manage.py generate_seek_sprites --dry-run

# Run synchronously in this process (no Celery worker needed)
python manage.py generate_seek_sprites --sync
```

The command skips videos whose `original_file` is missing from disk and reports them separately. Without `--force`, it also skips any video that already has a valid sprite file on disk.

### Frontend (Admin Commands panel)

Superadmins can also trigger sprite generation from the browser at `/admin-panel/commands/` using the **Generate Seek Sprites** card. It supports the same scope options (all / by channel / single video) plus Force and Dry Run toggles.

---

## Player integration

The watch page reads four template variables injected by `watch_page` in `views.py`:

| Variable | Source |
|---|---|
| `seek_sprite_url` | `MEDIA_URL + video.seek_sprite` (or `None` if disabled/missing) |
| `seek_thumb_interval` | `SEEK_THUMBNAIL_INTERVAL` setting |
| `seek_thumb_w` | `SEEK_THUMBNAIL_WIDTH` setting |
| `seek_thumb_h` | `SEEK_THUMBNAIL_HEIGHT` setting |
| `seek_thumb_cols` | `SEEK_THUMBNAIL_COLS` setting |

If `seek_sprite_url` is `None`, the preview block is not rendered and the hover/drag handlers fall back to no-ops — no JavaScript errors.

The preview card is positioned above the seek bar, clamped to the bar edges so it never overflows the left or right side of the player.

---

## Troubleshooting

**Sprite not showing on the watch page**
- Check `Video.seek_sprite` is set: `Video.objects.get(id=<uuid>).seek_sprite`
- Check the file exists at `media/seek_sprites/<uuid>.jpg`
- Check `SEEK_THUMBNAILS_ENABLED=true` in `.env`
- Check the Celery worker was running when the video was processed

**FFmpeg error: `Unable to parse "layout" option value`**
- You have an old version of the task that used `tile=25x` (no row count). Restart Celery after updating the code.

**Sprite exists but preview looks offset**
- The `SEEK_THUMBNAIL_COLS` value in `.env` must match the number of columns in the actual sprite. If you change `SEEK_THUMBNAIL_COLS`, regenerate all sprites with `--force`.

**Sprite generated but very large file**
- Increase `SEEK_THUMBNAIL_INTERVAL` (fewer tiles) or `SEEK_THUMBNAIL_QUALITY` (lower quality = smaller file).
