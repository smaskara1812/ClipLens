"""
Manual upscale helpers — Lanczos upscale to common broadcast / display long-edge sizes.

Video upscale strategy (per user spec):
  1. Upscale original file → new MP4 stored at  media/upscaled/<video_id>/<preset>.mp4
  2. Encode that MP4 as a new HLS quality track in  media/hls/<video_id>/<preset>/
  3. Append the new STREAM-INF entry to master.m3u8
  4. NO AI re-analysis (YOLO/BLIP/CLIP/faces don't change with a resolution change)
  5. Original file is NEVER deleted

Photo upscale strategy:
  1. Upscale original → saved at  media/upscaled/photos/<photo_id>.<ext>
  2. Photo.file record is updated to point to the new upscaled file
  3. Original dimensions are preserved in Photo.original_width / original_height if needed
  4. Original file is NEVER deleted — we keep it at its original path
     (the DB record now points to the upscaled path, original on disk is orphaned but safe)

Industry-style tiers use the longer edge (e.g. 1920 for 1080p 16:9, portrait-friendly).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile

logger = logging.getLogger(__name__)


def _media_root() -> Path:
    """Tenant-aware media root — mirrors the helper in tasks.py / services.py."""
    try:
        from tenants.storage import get_media_root
        return Path(get_media_root())
    except ImportError:
        return Path(settings.MEDIA_ROOT)

# (id, label, long_edge_px) — long edge is max(width, height) after upscale for that tier
UPSCALE_PRESETS: list[dict] = [
    {"id": "480p",   "label": "480p (SD)",      "long_edge": 854},
    {"id": "720p",   "label": "720p (HD)",       "long_edge": 1280},
    {"id": "1080p",  "label": "1080p (Full HD)", "long_edge": 1920},
    {"id": "1440p",  "label": "1440p (QHD)",     "long_edge": 2560},
    {"id": "2160p",  "label": "2160p (4K UHD)",  "long_edge": 3840},
]


def preset_by_id(preset_id: str) -> dict | None:
    for p in UPSCALE_PRESETS:
        if p["id"] == preset_id:
            return p
    return None


def long_edge_from_dimensions(width: int, height: int) -> int:
    return max(int(width), int(height))


def compute_upscale_dims(width: int, height: int, target_long_edge: int) -> tuple[int, int]:
    w, h = int(width), int(height)
    if w < 2 or h < 2:
        raise ValueError("Invalid dimensions")
    cur = max(w, h)
    if target_long_edge <= cur:
        raise ValueError("Target must be greater than the current resolution")
    scale = target_long_edge / cur
    # Round to even numbers (required by libx264)
    nw = max(2, int(round(w * scale / 2) * 2))
    nh = max(2, int(round(h * scale / 2) * 2))
    return nw, nh


# ─────────────────────────────────────────────────────────────────────────────
#  Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ffmpeg_upscale_to_mp4(src: Path, dst: Path, tw: int, th: int) -> None:
    """Lanczos-upscale src → dst as H.264 MP4.  Audio is stream-copied if possible.

    Forces a keyframe every 6 seconds (matching HLS segment length) so that the
    subsequent stream-copy HLS segmentation step produces clean, decodable segments
    without any re-encoding quality loss.
    """
    vf = f"scale={tw}:{th}:flags=lanczos"
    # Try audio copy first; some containers need re-encode
    for audio_opts in [["-c:a", "copy"], ["-c:a", "aac", "-b:a", "192k"]]:
        cmd = [
            settings.FFMPEG_PATH, "-y",
            "-fflags", "+genpts",
            "-i", str(src),
            "-vf", vf,
            "-c:v", "libx264", "-crf", "18", "-preset", "medium",
            # Force a keyframe every 6 s so stream-copy HLS segmentation is clean
            "-force_key_frames", "expr:gte(t,n_forced*6)",
            *audio_opts,
            str(dst),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=4 * 3600)
        if r.returncode == 0:
            return
        last_err = (r.stderr or "")[-1800:]
    raise RuntimeError(f"FFmpeg upscale failed\n{last_err}")


def _encode_hls_from_mp4(src_mp4: Path, out_dir: Path, tw: int, th: int) -> None:
    """Package src_mp4 → HLS .ts segments + playlist.m3u8 in out_dir.  No re-scaling,
    no re-encoding — the input is already a high-quality H.264 MP4 from _ffmpeg_upscale_to_mp4
    so we stream-copy both tracks.  This is much faster and avoids a second quality loss.
    Uses the same filenames as services.py so the master playlist is consistent."""
    out_dir.mkdir(parents=True, exist_ok=True)
    playlist = str(out_dir / "playlist.m3u8")   # match services.py naming
    seg_pat  = str(out_dir / "seg_%03d.ts")
    cmd = [
        settings.FFMPEG_PATH, "-y",
        "-i", str(src_mp4),
        "-c", "copy",           # stream-copy — no re-encode
        "-hls_time", "6",
        "-hls_playlist_type", "vod",
        "-hls_segment_filename", seg_pat,
        playlist,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=4 * 3600)
    if r.returncode != 0:
        raise RuntimeError(f"HLS encode failed:\n{(r.stderr or '')[-1200:]}")


# Realistic bandwidth values that match services.py's quality ladder.
# These are what hls.js uses for ABR decisions — must not be inflated or
# the player will never select the high-quality track even on fast connections.
# (video_kbps + audio_kbps) * 1000  →  bps
_PRESET_BANDWIDTH: dict[str, int] = {
    "144p":  182_000,    # 150 + 32  kbps
    "240p":  348_000,    # 300 + 48
    "360p":  664_000,    # 600 + 64
    "480p":  1_296_000,  # 1200 + 96
    "720p":  2_628_000,  # 2500 + 128
    "1080p": 4_192_000,  # 4000 + 192
    "1440p": 8_192_000,  # 8000 + 192
    "2160p": 16_192_000, # 16000 + 192
}


def _bandwidth_for_preset(preset_id: str, tw: int, th: int) -> int:
    """Return a realistic BANDWIDTH value (bps) for a given preset / resolution."""
    if preset_id in _PRESET_BANDWIDTH:
        return _PRESET_BANDWIDTH[preset_id]
    # Fallback: scale linearly from 1080p reference
    px_ratio = (tw * th) / (1920 * 1080)
    return max(500_000, int(_PRESET_BANDWIDTH["1080p"] * px_ratio))


def _parse_bandwidth(inf_line: str) -> int:
    """Extract the BANDWIDTH integer from an #EXT-X-STREAM-INF line.

    The line looks like:
        #EXT-X-STREAM-INF:BANDWIDTH=4192000,RESOLUTION=...
    Split on ':' first to drop the tag name, then walk the comma-separated attrs.
    """
    if ":" in inf_line:
        attrs = inf_line.split(":", 1)[1]
    else:
        attrs = inf_line
    for attr in attrs.split(","):
        kv = attr.strip()
        if kv.startswith("BANDWIDTH="):
            try:
                return int(kv.split("=", 1)[1])
            except ValueError:
                pass
    return 0


def _rewrite_master_sorted(master_path: Path) -> None:
    """
    Re-write master.m3u8 with STREAM-INF entries sorted by BANDWIDTH descending.
    hls.js in "Auto" mode picks the highest quality the bandwidth allows — sorting
    highest-first means it can ramp up quickly on fast connections.
    """
    text = master_path.read_text()
    lines = text.splitlines()

    header: list[str] = []
    entries: list[tuple[int, list[str]]] = []  # (bandwidth, [inf_line, uri_line])
    i = 0
    while i < len(lines) and not lines[i].startswith("#EXT-X-STREAM-INF"):
        header.append(lines[i])
        i += 1
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXT-X-STREAM-INF"):
            inf_line = line
            uri_line = lines[i + 1] if i + 1 < len(lines) else ""
            bw = _parse_bandwidth(inf_line)
            entries.append((bw, [inf_line, uri_line]))
            i += 2
        else:
            i += 1  # skip blank lines between entries

    entries.sort(key=lambda e: e[0], reverse=True)

    out_lines = [l for l in header if l]  # drop blank lines in header block
    for _, pair in entries:
        out_lines.extend(pair)

    master_path.write_text("\n".join(out_lines) + "\n")


def _append_quality_to_master(master_path: Path, preset_id: str, tw: int, th: int) -> None:
    """
    Add a new STREAM-INF entry pointing to  <preset_id>/index.m3u8 in the master playlist.
    If the entry already exists it is not duplicated.
    After appending, re-sorts entries by BANDWIDTH descending so hls.js ABR works correctly.
    """
    rel_playlist = f"{preset_id}/playlist.m3u8"  # match services.py naming
    if not master_path.exists():
        logger.warning("master.m3u8 not found at %s; skipping append", master_path)
        return

    content = master_path.read_text()
    if rel_playlist in content:
        logger.info("Quality %s already present in master.m3u8 — skipping append", preset_id)
        return

    bandwidth = _bandwidth_for_preset(preset_id, tw, th)
    new_block = (
        f"\n#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},"
        f"RESOLUTION={tw}x{th},"
        f'NAME="{preset_id}"\n'
        f"{rel_playlist}\n"
    )
    with open(master_path, "a") as fh:
        fh.write(new_block)
    logger.info(
        "Appended quality %s (%dx%d, %d bps) to %s",
        preset_id, tw, th, bandwidth, master_path,
    )

    # Re-sort so highest quality is listed first — helps hls.js ABR ramp up faster
    try:
        _rewrite_master_sorted(master_path)
    except Exception as exc:
        logger.warning("Could not re-sort master.m3u8 after append: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
#  Public pipelines
# ─────────────────────────────────────────────────────────────────────────────

def run_video_upscale_pipeline(video_id: str, target_long_edge: int) -> None:
    """
    Full video upscale:
      1. Lanczos-upscale original → temp MP4
      2. Store at  media/upscaled/<video_id>/<preset_id>.mp4   (original untouched)
      3. Encode HLS quality track at  media/hls/<video_id>/<preset_id>/
      4. Append new STREAM-INF to master.m3u8
      5. Update Video.available_qualities
      NO AI re-analysis.  Original file is NEVER deleted.
    """
    from .models import Video
    from .services import get_video_metadata

    video = Video.objects.get(id=video_id)
    if not video.original_file or not video.original_file.name:
        raise ValueError("No original video file attached to this video record.")

    src = Path(video.original_file.path)
    if not src.is_file():
        raise ValueError(f"Original file missing on disk: {src}")

    meta = get_video_metadata(src)
    w, h = int(meta.get("width", 0)), int(meta.get("height", 0))
    if w <= 0 or h <= 0:
        raise ValueError("Could not read video dimensions from original file.")

    cur_max = long_edge_from_dimensions(w, h)
    if target_long_edge <= cur_max:
        raise ValueError(
            f"Target ({target_long_edge}px) must be larger than the current "
            f"long edge ({cur_max}px)."
        )

    tw, th = compute_upscale_dims(w, h, target_long_edge)

    # Find the preset label for this long edge — always use the "<height>p" form
    # so it matches the quality labels produced by services._FULL_LADDER.
    preset_id = next(
        (p["id"] for p in UPSCALE_PRESETS if p["long_edge"] == target_long_edge),
        f"{th}p",   # fallback: derive from output height, e.g. "2160p"
    )

    # ── Mark video as processing ──────────────────────────────────────────────
    Video.objects.filter(id=video_id).update(
        status=Video.STATUS_PROCESSING,
        processing_error="",
    )

    # ── Work in a temp file; never touch original ─────────────────────────────
    tmp_fd, tmp_str = tempfile.mkstemp(suffix=".mp4")
    os.close(tmp_fd)
    tmp_mp4 = Path(tmp_str)

    try:
        logger.info("Upscaling %s → %dx%d (preset=%s)", video_id, tw, th, preset_id)
        _ffmpeg_upscale_to_mp4(src, tmp_mp4, tw, th)

        # ── Store upscaled MP4 separately ─────────────────────────────────────
        upscale_dir = _media_root() / "upscaled" / str(video_id)
        upscale_dir.mkdir(parents=True, exist_ok=True)
        upscaled_mp4 = upscale_dir / f"{preset_id}.mp4"
        shutil.move(str(tmp_mp4), str(upscaled_mp4))
        logger.info("Stored upscaled MP4 at %s", upscaled_mp4)

        # ── Encode new HLS quality track ──────────────────────────────────────
        if video.hls_path:
            hls_dir = _media_root() / "hls" / str(video_id)
            quality_dir = hls_dir / preset_id
            _encode_hls_from_mp4(upscaled_mp4, quality_dir, tw, th)
            logger.info("HLS track created at %s", quality_dir)

            # Append to master.m3u8
            master_m3u8 = hls_dir / "master.m3u8"
            _append_quality_to_master(master_m3u8, preset_id, tw, th)

        # ── Update available_qualities on the Video record ────────────────────
        video.refresh_from_db()
        existing = [q.strip() for q in (video.available_qualities or "").split(",") if q.strip()]
        if preset_id not in existing:
            existing.append(preset_id)

        # ── Tally total upscaled_size across all presets in media/upscaled/<id>/ ─
        total_upscaled = 0
        try:
            for f in upscale_dir.iterdir():
                if f.is_file():
                    total_upscaled += f.stat().st_size
        except Exception:
            pass

        Video.objects.filter(id=video_id).update(
            available_qualities=",".join(existing),
            upscaled_size=total_upscaled or None,
        )

        # ── Mark ready ────────────────────────────────────────────────────────
        Video.objects.filter(id=video_id).update(status=Video.STATUS_READY)
        logger.info("Video upscale complete for %s (preset=%s)", video_id, preset_id)

    except Exception as exc:
        logger.exception("Video upscale failed for %s", video_id)
        try:
            Video.objects.filter(id=video_id).update(
                status=Video.STATUS_FAILED,
                processing_error=str(exc)[:2000],
            )
        except Exception:
            pass
        raise
    finally:
        if tmp_mp4.is_file():
            try:
                tmp_mp4.unlink()
            except OSError:
                pass


def run_photo_upscale_pipeline(photo_id: str, target_long_edge: int) -> None:
    """
    Photo upscale:
      1. Lanczos-resize in memory
      2. Save upscaled file to  media/upscaled/photos/<photo_id>.<ext>
      3. Update Photo.file to point to the new upscaled path
         (original on disk is left intact; it simply becomes unlisted in the DB)
      4. Update width / height / file_size
      NO AI re-analysis (same content, higher resolution — labels/embeddings remain valid).
    """
    from PIL import Image

    try:
        _resample = Image.Resampling.LANCZOS
    except AttributeError:
        _resample = Image.LANCZOS  # Pillow < 9

    from .models import Photo

    photo = Photo.objects.get(id=photo_id)
    if not photo.file or not photo.file.name:
        raise ValueError("No photo file attached to this record.")

    orig_path = Path(photo.file.path)
    if not orig_path.is_file():
        raise ValueError(f"Photo file missing on disk: {orig_path}")

    w, h = photo.width, photo.height
    if not w or not h:
        with Image.open(orig_path) as _im:
            w, h = _im.size
    if w < 2 or h < 2:
        raise ValueError("Invalid image dimensions.")

    cur_max = long_edge_from_dimensions(w, h)
    if target_long_edge <= cur_max:
        raise ValueError(
            f"Target ({target_long_edge}px) must be larger than the current "
            f"long edge ({cur_max}px)."
        )

    tw, th = compute_upscale_dims(w, h, target_long_edge)

    Photo.objects.filter(id=photo_id).update(
        status=Photo.STATUS_PROCESSING,
        processing_error="",
    )

    try:
        from .tasks import _open_any_photo

        ext = orig_path.suffix.lower().lstrip(".") or "jpg"
        pil_img = _open_any_photo(orig_path, ext)
        upscaled = pil_img.resize((tw, th), _resample)

        buf = BytesIO()
        if ext in ("jpg", "jpeg"):
            upscaled.convert("RGB").save(buf, format="JPEG", quality=92, optimize=True)
            save_ext = "jpg"
        elif ext == "png":
            upscaled.save(buf, format="PNG", optimize=True)
            save_ext = "png"
        elif ext == "webp":
            upscaled.save(buf, format="WEBP", quality=90, method=4)
            save_ext = "webp"
        else:
            upscaled.convert("RGB").save(buf, format="JPEG", quality=92, optimize=True)
            save_ext = "jpg"

        raw_bytes = buf.getvalue()

        # ── Save upscaled file to media/upscaled/photos/<id>.<ext>  ──────────
        # We use a path that does NOT include 'photos/originals/' because
        # Photo.file already has upload_to='photos/originals/'.  Saving via
        # photo.file.save() with just the filename means Django stores it at:
        #   MEDIA_ROOT / photos/originals / <filename>
        # which is correct.  We give it a distinct name so the original is not
        # overwritten (both live in the same upload_to bucket but different names).
        upscale_fname = f"{photo_id}_upscaled_{tw}x{th}.{save_ext}"
        photo.file.save(upscale_fname, ContentFile(raw_bytes), save=False)
        photo.width    = tw
        photo.height   = th
        photo.file_size = len(raw_bytes)
        # Clear the old thumbnail so it is regenerated against the new dimensions
        if photo.thumbnail:
            try:
                photo.thumbnail.delete(save=False)
            except Exception:
                pass
            photo.thumbnail = None
        photo.status = Photo.STATUS_READY
        photo.processing_error = ""
        photo.save(update_fields=["file", "width", "height", "file_size", "thumbnail", "status", "processing_error"])

        logger.info(
            "Photo upscale complete for %s → %dx%d saved at %s",
            photo_id, tw, th, photo.file.name,
        )

        # Regenerate thumbnail only (no AI re-analysis needed — content is identical)
        # Inline thumbnail generation — mirrors the logic inside analyze_photo_task.
        try:
            from PIL import Image as _PILImage
            thumb_path = _media_root() / "photos" / "thumbnails" / f"{photo_id}.jpg"
            thumb_path.parent.mkdir(parents=True, exist_ok=True)
            with _PILImage.open(Path(photo.file.path)) as _im:
                _thumb = _im.copy()
            _thumb.thumbnail((480, 480), _PILImage.LANCZOS if hasattr(_PILImage, "LANCZOS") else _PILImage.Resampling.LANCZOS)
            _thumb.convert("RGB").save(str(thumb_path), "JPEG", quality=82)
            thumb_rel = str(thumb_path.relative_to(_media_root()))
            Photo.objects.filter(id=photo_id).update(thumbnail=thumb_rel)
            logger.info("Thumbnail regenerated for %s after upscale", photo_id)
        except Exception as exc:
            logger.warning("Thumbnail regen failed for %s after upscale: %s", photo_id, exc)

    except Exception as exc:
        logger.exception("Photo upscale failed for %s", photo_id)
        try:
            Photo.objects.filter(id=photo_id).update(
                status=Photo.STATUS_FAILED,
                processing_error=str(exc)[:2000],
            )
        except Exception:
            pass
        raise
