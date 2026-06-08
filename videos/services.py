"""
FFmpeg HLS conversion service — single & multi-resolution (adaptive bitrate).

Multi-quality output structure:
    media/hls/<video_id>/
        master.m3u8          ← master playlist (ABR entry point)
        1080p/
            playlist.m3u8
            segment_0000.ts …
        720p/
            playlist.m3u8
            segment_0000.ts …
        480p/ …
        360p/ …

Single-quality output (HLS_MULTI_QUALITY=false):
    media/hls/<video_id>/
        playlist.m3u8
        segment_0000.ts …
"""
import json
import logging
import shutil
import subprocess
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)


def _media_root() -> Path:
    """Tenant-aware media root (falls back to settings.MEDIA_ROOT in single-tenant mode)."""
    try:
        from tenants.storage import get_media_root
        return Path(get_media_root())
    except ImportError:
        return _media_root()


def _probe_duration(path: str) -> float:
    """Return duration in seconds of any media file/playlist via ffprobe. Returns 0 on error."""
    cmd = [
        settings.FFPROBE_PATH,
        '-v', 'quiet',
        '-print_format', 'json',
        '-show_format',
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout)
        dur = data.get('format', {}).get('duration')
        return float(dur) if dur else 0.0
    except Exception:
        return 0.0


# ── Quality ladder ────────────────────────────────────────────────────────────
# (label, max_height, video_bitrate_kbps, audio_bitrate_kbps)
# Only renditions whose height ≤ source video height are encoded.
_FULL_LADDER = [
    ('2160p', 2160, 16000, 192),
    ('1440p', 1440,  8000, 192),
    ('1080p', 1080,  4000, 192),
    ('720p',   720,  2500, 128),
    ('480p',   480,  1200,  96),
    ('360p',   360,   600,  64),
    ('240p',   240,   300,  48),
    ('144p',   144,   150,  32),
]


def _active_ladder(tenant_heights: set = None):
    """
    Return ladder rows whose heights are enabled.
    Precedence:
      1. tenant_heights (per-tenant override, when caller passes it)
      2. global settings.HLS_QUALITIES (env)
      3. fallback [1080, 720, 480, 360]
    """
    if tenant_heights:
        enabled = set(int(h) for h in tenant_heights)
    else:
        enabled = set(getattr(settings, 'HLS_QUALITIES', [1080, 720, 480, 360]))
    return [r for r in _FULL_LADDER if r[1] in enabled]


def _tenant_hls_heights() -> set:
    """
    Resolve current tenant's enabled HLS heights from the thread-local context.
    Returns empty set when no tenant or no override → caller falls back to global.
    """
    try:
        from tenants.storage import get_media_root
        from tenants.models import Tenant
        # MEDIA_ROOT thread-local points at the tenant dir; derive slug from path
        from django.conf import settings as _s
        from pathlib import Path
        root = Path(get_media_root())
        media_root = Path(str(_s.MEDIA_ROOT))
        try:
            suffix = root.relative_to(media_root)
            slug = suffix.parts[1] if suffix.parts and suffix.parts[0] == 'tenants' else None
        except ValueError:
            slug = None
        if not slug:
            return set()
        t = Tenant.objects.using('control').only('hls_enabled_qualities').get(slug=slug)
        return t.get_enabled_hls_heights()
    except Exception:
        return set()


def _tenant_hls_multi_enabled() -> bool:
    """
    Per-tenant override of HLS_MULTI_QUALITY. True if tenant says multi or no tenant context.
    """
    try:
        from tenants.storage import get_media_root
        from tenants.models import Tenant
        from django.conf import settings as _s
        from pathlib import Path
        root = Path(get_media_root())
        media_root = Path(str(_s.MEDIA_ROOT))
        try:
            suffix = root.relative_to(media_root)
            slug = suffix.parts[1] if suffix.parts and suffix.parts[0] == 'tenants' else None
        except ValueError:
            slug = None
        if not slug:
            return bool(getattr(settings, 'HLS_MULTI_QUALITY', True))
        t = Tenant.objects.using('control').only('hls_multi_quality').get(slug=slug)
        return t.hls_multi_quality and bool(getattr(settings, 'HLS_MULTI_QUALITY', True))
    except Exception:
        return bool(getattr(settings, 'HLS_MULTI_QUALITY', True))


# ── Metadata ─────────────────────────────────────────────────────────────────

def get_video_metadata(file_path: str) -> dict:
    """Use ffprobe to extract duration, width, height, resolution string."""
    cmd = [
        settings.FFPROBE_PATH,
        '-v', 'quiet',
        '-print_format', 'json',
        '-show_streams',
        '-show_format',
        str(file_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout)

        duration = None
        width = height = 0
        resolution = ''

        fmt = data.get('format', {})
        if fmt.get('duration'):
            duration = float(fmt['duration'])

        fps = 25.0   # safe default
        for stream in data.get('streams', []):
            if stream.get('codec_type') == 'video':
                width  = stream.get('width', 0)
                height = stream.get('height', 0)
                if width and height:
                    resolution = f'{width}x{height}'
                if not duration and stream.get('duration'):
                    duration = float(stream['duration'])
                # Parse r_frame_rate e.g. "30/1", "2997/100", "25/1"
                rfr = stream.get('r_frame_rate', '25/1')
                try:
                    num, den = rfr.split('/')
                    fps = float(num) / float(den)
                    if fps > 120 or fps < 1:   # sanity-check; fall back if nonsensical
                        fps = 25.0
                except Exception:
                    fps = 25.0
                break

        return {
            'duration':   duration,
            'resolution': resolution,
            'width':      width,
            'height':     height,
            'fps':        fps,
        }
    except Exception as e:
        logger.warning(f'ffprobe failed for {file_path}: {e}')
        return {'duration': None, 'resolution': '', 'width': 0, 'height': 0, 'fps': 25.0}


# ── Single-quality HLS ───────────────────────────────────────────────────────

def _convert_single(video_id: str, source_path: str,
                    fps: float = 25.0, source_duration: float = 0.0) -> tuple[str, list]:
    """
    Encode a single HLS stream (no quality switching).
    Returns (hls_relative_path, []).
    """
    out_dir = _media_root() / 'hls' / str(video_id)

    # Wipe stale segments from any previous run
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    playlist = out_dir / 'playlist.m3u8'
    segment_pat = str(out_dir / 'segment_%04d.ts')

    seg = settings.HLS_SEGMENT_DURATION
    # GOP = frames per segment. Ensures consistent segment boundaries across
    # all renditions without relying on the error-prone n_forced expression.
    gop = max(1, int(round(fps * seg)))
    cmd = [
        settings.FFMPEG_PATH,
        '-fflags', '+genpts',       # regenerate PTS — fixes VFR / timestamp issues in source
        '-i', str(source_path),
        '-c:v', 'libx264',
        '-c:a', 'aac',
        '-profile:v', 'baseline',
        '-level', '3.0',
        '-g', str(gop),             # keyframe every <seg> seconds (in frames)
        '-keyint_min', str(gop),    # minimum distance between keyframes
        '-sc_threshold', '0',       # no extra keyframes on scene changes
        '-start_number', '0',
        '-hls_time', str(seg),
        '-hls_list_size', '0',
        '-hls_segment_filename', segment_pat,
        '-hls_flags', 'independent_segments',
        '-f', 'hls',
        str(playlist),
        '-y',
    ]

    logger.info(
        f'[{video_id}] Single-quality HLS conversion started — '
        f'gop={gop} seg={seg}s fps={fps:.2f}'
    )
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    stderr_tail = result.stderr[-2000:] if result.stderr else ''

    if result.returncode != 0:
        raise RuntimeError(f'FFmpeg error: {stderr_tail}')

    ts_files = sorted(out_dir.glob('segment_*.ts'))
    logger.info(f'[{video_id}] Single-quality HLS complete — {len(ts_files)} segments written')
    logger.debug(f'[{video_id}] FFmpeg stderr tail:\n{stderr_tail}')

    # Duration sanity-check
    out_duration = _probe_duration(str(playlist))
    if source_duration > 0 and out_duration > 0:
        ratio = out_duration / source_duration
        if ratio < 0.95:
            logger.warning(
                f'[{video_id}] TRUNCATION DETECTED (single): '
                f'output={out_duration:.1f}s source={source_duration:.1f}s '
                f'({ratio*100:.1f}%). FFmpeg stderr:\n{stderr_tail}'
            )
        else:
            logger.info(
                f'[{video_id}] Duration OK: output={out_duration:.1f}s / source={source_duration:.1f}s'
            )

    # Return storage-form path (tenants/<slug>/...) so /media/<path> resolves correctly
    from tenants.storage import to_storage_path as _tsp_h
    hls_dir = _media_root() / 'hls' / str(video_id)
    return _tsp_h(hls_dir / 'playlist.m3u8'), []


# ── Multi-quality HLS ────────────────────────────────────────────────────────

def _encode_rendition(video_id: str, source_path: str,
                      label: str, height: int,
                      v_kbps: int, a_kbps: int, fps: float = 25.0,
                      source_duration: float = 0.0) -> bool:
    """
    Encode one rendition.  Returns True on success.
    Output: media/hls/<video_id>/<label>/playlist.m3u8
    """
    out_dir = _media_root() / 'hls' / str(video_id) / label

    # Wipe any stale segments/playlist from a previous (possibly truncated) run
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    playlist    = out_dir / 'playlist.m3u8'
    segment_pat = str(out_dir / 'segment_%04d.ts')

    seg = settings.HLS_SEGMENT_DURATION
    gop = max(1, int(round(fps * seg)))   # keyframe every <seg> seconds in frames
    cmd = [
        settings.FFMPEG_PATH,
        '-fflags', '+genpts',       # fix VFR / missing PTS in source
        '-i', str(source_path),
        # Video
        '-c:v', 'libx264',
        '-profile:v', 'baseline',
        '-level', '3.1',
        '-vf', f'scale=-2:{height}',
        '-b:v', f'{v_kbps}k',
        '-maxrate', f'{v_kbps}k',
        '-bufsize', f'{v_kbps * 2}k',
        '-g', str(gop),             # keyframe interval = seg seconds (in frames)
        '-keyint_min', str(gop),
        '-sc_threshold', '0',       # disable scene-cut keyframes
        # Audio
        '-c:a', 'aac',
        '-b:a', f'{a_kbps}k',
        '-ar', '44100',
        # HLS
        '-start_number', '0',
        '-hls_time', str(seg),
        '-hls_list_size', '0',
        '-hls_segment_filename', segment_pat,
        '-hls_flags', 'independent_segments',
        '-f', 'hls',
        str(playlist),
        '-y',
    ]

    logger.info(
        f'[{video_id}] Encoding rendition {label} ({height}p @ {v_kbps}kbps) '
        f'gop={gop} seg={seg}s fps={fps:.2f}'
    )
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)

    # Always log the tail of stderr — shows where FFmpeg stopped even on success
    stderr_tail = result.stderr[-2000:] if result.stderr else ''
    if result.returncode != 0:
        logger.error(f'[{video_id}] Rendition {label} FAILED (rc={result.returncode}):\n{stderr_tail}')
        return False

    # Count segments produced
    ts_files = sorted(out_dir.glob('segment_*.ts'))
    logger.info(
        f'[{video_id}] Rendition {label} complete — {len(ts_files)} segments written'
    )
    logger.debug(f'[{video_id}] FFmpeg stderr tail:\n{stderr_tail}')

    # Duration sanity-check: ffprobe the output playlist
    out_duration = _probe_duration(str(playlist))
    if source_duration > 0 and out_duration > 0:
        ratio = out_duration / source_duration
        if ratio < 0.95:
            logger.warning(
                f'[{video_id}] TRUNCATION DETECTED on {label}: '
                f'output={out_duration:.1f}s source={source_duration:.1f}s '
                f'({ratio*100:.1f}%). FFmpeg stderr:\n{stderr_tail}'
            )
        else:
            logger.info(
                f'[{video_id}] Duration OK for {label}: '
                f'output={out_duration:.1f}s / source={source_duration:.1f}s'
            )

    return True


def _write_master_playlist(video_id: str, encoded: list,
                           src_width: int = 0, src_height: int = 0) -> str:
    """
    Write master.m3u8 referencing all encoded renditions.
    encoded = [(label, height, v_kbps, a_kbps), …]

    Includes RESOLUTION so hls.js can populate level.height correctly,
    and NAME so level.name returns the human-readable label (e.g. "720p").
    Returns relative path from MEDIA_ROOT.
    """
    out_dir = _media_root() / 'hls' / str(video_id)
    master  = out_dir / 'master.m3u8'

    lines = ['#EXTM3U', '#EXT-X-VERSION:3', '']
    for label, height, v_kbps, a_kbps in encoded:
        bandwidth = (v_kbps + a_kbps) * 1000   # bits per second

        # Calculate encoded width from source aspect ratio, ensure divisible by 2
        if src_width and src_height:
            enc_width = int(round(src_width / src_height * height / 2) * 2)
        else:
            # Fall back to standard 16:9 widths
            _fallback = {2160: 3840, 1440: 2560, 1080: 1920, 720: 1280, 480: 854, 360: 640, 240: 426, 144: 256}
            enc_width = _fallback.get(height, height * 16 // 9)

        lines.append(
            f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},'
            f'RESOLUTION={enc_width}x{height},'
            f'NAME="{label}"'
        )
        lines.append(f'{label}/playlist.m3u8')

    master.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    logger.info(f'[{video_id}] Master playlist written with {len(encoded)} rendition(s)')
    # Return storage-form path (tenants/<slug>/...) so /media/<path> resolves correctly
    from tenants.storage import to_storage_path as _tsp_m
    hls_dir = _media_root() / 'hls' / str(video_id)
    return _tsp_m(hls_dir / 'master.m3u8')


def _convert_multi(video_id: str, source_path: str,
                   source_height: int, source_width: int = 0,
                   fps: float = 25.0, source_duration: float = 0.0) -> tuple[str, list]:
    """
    Encode multiple renditions and produce a master.m3u8.
    Skips any rendition whose target height > source height.
    Honours per-tenant HLS quality restrictions (set via the admin UI).
    Returns (master_hls_relative_path, [list of quality labels]).
    """
    tenant_heights = _tenant_hls_heights()
    ladder = _active_ladder(tenant_heights=tenant_heights)
    if tenant_heights:
        logger.info(
            f'[{video_id}] Using tenant-restricted HLS ladder: {sorted(tenant_heights, reverse=True)}'
        )

    # Filter to renditions ≤ source height
    applicable = [row for row in ladder if row[1] <= source_height]

    # If the source is below the smallest rung, encode it as-is at 360p settings
    if not applicable:
        label, height, v_kbps, a_kbps = ladder[-1]  # lowest rung
        applicable = [(label, source_height, v_kbps, a_kbps)]

    encoded = []
    for label, height, v_kbps, a_kbps in applicable:
        ok = _encode_rendition(
            video_id, source_path, label, height, v_kbps, a_kbps,
            fps=fps, source_duration=source_duration,
        )
        if ok:
            encoded.append((label, height, v_kbps, a_kbps))

    if not encoded:
        raise RuntimeError('All renditions failed during multi-quality HLS encoding.')

    master_rel = _write_master_playlist(
        video_id, encoded,
        src_width=source_width,
        src_height=source_height,
    )
    quality_labels = [e[0] for e in encoded]
    return master_rel, quality_labels


# ── Public entry point ────────────────────────────────────────────────────────

def convert_to_hls(video_id: str, source_path: str,
                   source_height: int = 0, source_width: int = 0,
                   fps: float = 25.0, source_duration: float = 0.0) -> tuple[str, list]:
    """
    Convert source video to HLS.

    If HLS_MULTI_QUALITY=true (default):
        Encodes multiple renditions and produces a master.m3u8.
        Returns (master_m3u8_relative_path, ['1080p', '720p', …])

    If HLS_MULTI_QUALITY=false:
        Single-quality encode.
        Returns (playlist_m3u8_relative_path, [])
    """
    multi = _tenant_hls_multi_enabled()

    if multi and source_height > 0:
        return _convert_multi(
            video_id, source_path, source_height, source_width,
            fps=fps, source_duration=source_duration,
        )
    else:
        return _convert_single(video_id, source_path, fps=fps, source_duration=source_duration)


# ── Thumbnail ─────────────────────────────────────────────────────────────────

def extract_thumbnail(video_id: str, source_path: str, timestamp: float = 5.0) -> str:
    """
    Extract a thumbnail frame from the video.
    Returns relative path from MEDIA_ROOT, or empty string on failure.
    """
    thumb_dir  = _media_root() / 'thumbnails'
    thumb_dir.mkdir(parents=True, exist_ok=True)
    thumb_path = thumb_dir / f'{video_id}.jpg'

    cmd = [
        settings.FFMPEG_PATH,
        '-ss', str(timestamp),
        '-i', str(source_path),
        '-vframes', '1',
        '-q:v', '2',
        str(thumb_path),
        '-y',
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode == 0 and thumb_path.exists():
        # Return path relative to the tenant media root (not global MEDIA_ROOT) so that
        # ImageField.url = base_url + name resolves correctly with the tenant-aware base_url.
        return str(thumb_path.relative_to(_media_root()))
    return ''


# ── Full pipeline ─────────────────────────────────────────────────────────────

def process_video(video_id: str) -> None:
    """
    Full processing pipeline:
    1. Extract metadata (duration, resolution, source height)
    2. Convert to HLS — single or multi-quality based on settings
    3. Extract thumbnail
    4. Update DB record
    """
    from .models import Video

    video = Video.objects.get(id=video_id)
    video.status = Video.STATUS_PROCESSING
    video.save(update_fields=['status'])

    source_path = Path(video.original_file.path)

    try:
        # Step 1: metadata
        meta = get_video_metadata(source_path)
        video.duration   = meta['duration']
        video.resolution = meta['resolution']
        source_height    = meta.get('height', 0)
        source_width     = meta.get('width', 0)
        source_fps       = meta.get('fps', 25.0)

        logger.info(
            f'[{video_id}] source: {source_width}x{source_height} '
            f'@ {source_fps:.2f}fps, duration={meta["duration"]}s'
        )

        # Step 2: HLS conversion (single or multi)
        hls_rel, qualities = convert_to_hls(
            str(video_id), source_path, source_height, source_width,
            fps=source_fps, source_duration=meta.get('duration') or 0.0,
        )
        video.hls_path           = hls_rel
        video.available_qualities = ','.join(qualities)   # e.g. "1080p,720p,480p,360p"

        # Step 3: thumbnail
        thumb_ts  = min(5.0, (meta['duration'] or 10) * 0.1)
        thumb_rel = extract_thumbnail(str(video_id), source_path, thumb_ts)
        if thumb_rel:
            video.thumbnail = thumb_rel

        video.status           = Video.STATUS_READY
        video.processing_error = ''

    except Exception as e:
        logger.error(f'Processing failed for video {video_id}: {e}')
        video.status           = Video.STATUS_FAILED
        video.processing_error = str(e)

    video.save()
