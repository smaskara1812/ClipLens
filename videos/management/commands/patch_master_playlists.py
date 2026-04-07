"""
Management command: patch_master_playlists
==========================================
Rewrites master.m3u8 for every ready multi-quality video to include the
RESOLUTION attribute — needed so hls.js can populate level.height and
display correct quality labels.

No re-encoding. Reads the resolution already stored in the Video model
(e.g. "1280x720"), scans the HLS folder for rendition sub-directories,
and reconstructs the BANDWIDTH + RESOLUTION lines.

Usage:
    python manage.py patch_master_playlists
    python manage.py patch_master_playlists --video-id <uuid>   # single video
"""

import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from videos.models import Video


# Rendition ladder — must match services.py QUALITY_LADDER defaults
_BITRATE_MAP = {
    '1080p': (4000, 192),
    '720p':  (2500, 128),
    '480p':  (1000,  96),
    '360p':  ( 500,  64),
}


def _parse_resolution(res_str: str):
    """Parse '1280x720' → (width, height). Returns (0, 0) on failure."""
    if not res_str:
        return 0, 0
    m = re.match(r'(\d+)\s*[xX×]\s*(\d+)', res_str.strip())
    if m:
        return int(m.group(1)), int(m.group(2))
    return 0, 0


def _patch_video(video, verbosity):
    hls_dir = Path(settings.MEDIA_ROOT) / 'hls' / str(video.id)
    master  = hls_dir / 'master.m3u8'

    if not hls_dir.exists():
        if verbosity:
            print(f'  [{video.id}] SKIP — no HLS directory')
        return False

    # Collect rendition sub-dirs that contain a playlist.m3u8
    renditions = sorted(
        [d.name for d in hls_dir.iterdir()
         if d.is_dir() and (d / 'playlist.m3u8').exists()]
    )

    if not renditions:
        if verbosity:
            print(f'  [{video.id}] SKIP — no rendition sub-dirs found')
        return False

    if len(renditions) == 1 and renditions[0] not in _BITRATE_MAP:
        # Single-quality encode — no master playlist needed
        if verbosity:
            print(f'  [{video.id}] SKIP — single-quality, no master')
        return False

    src_w, src_h = _parse_resolution(video.resolution)

    lines = ['#EXTM3U', '#EXT-X-VERSION:3', '']

    # Sort renditions highest-to-lowest (1080p → 360p)
    order = list(_BITRATE_MAP.keys())
    renditions_sorted = sorted(
        renditions,
        key=lambda r: order.index(r) if r in order else 999
    )

    for label in renditions_sorted:
        v_kbps, a_kbps = _BITRATE_MAP.get(label, (1000, 128))
        bandwidth = (v_kbps + a_kbps) * 1000

        # Derive encoded height from label (e.g. '720p' → 720)
        height_match = re.match(r'(\d+)p', label)
        enc_height = int(height_match.group(1)) if height_match else 0

        # Calculate width from source aspect ratio
        if src_w and src_h and enc_height:
            enc_width = int(round(src_w / src_h * enc_height / 2) * 2)
        else:
            _fallback = {1080: 1920, 720: 1280, 480: 854, 360: 640}
            enc_width = _fallback.get(enc_height, enc_height * 16 // 9)

        res_attr = f'RESOLUTION={enc_width}x{enc_height},' if enc_height else ''

        lines.append(
            f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},'
            f'{res_attr}'
            f'NAME="{label}"'
        )
        lines.append(f'{label}/playlist.m3u8')

    master.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    if verbosity:
        print(f'  [{video.id}] PATCHED — {len(renditions_sorted)} rendition(s): '
              f'{", ".join(renditions_sorted)}  src={src_w}x{src_h}')
    return True


class Command(BaseCommand):
    help = 'Patch master.m3u8 files to include RESOLUTION attribute (no re-encoding)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--video-id',
            type=str,
            default=None,
            help='UUID of a single video to patch (omit to patch all ready videos)',
        )

    def handle(self, *args, **options):
        vid_id   = options.get('video_id')
        verbosity = options.get('verbosity', 1)

        if vid_id:
            qs = Video.objects.filter(id=vid_id)
        else:
            qs = Video.objects.filter(status='ready')

        total   = qs.count()
        patched = 0

        self.stdout.write(f'Processing {total} video(s)…\n')

        for video in qs:
            ok = _patch_video(video, verbosity)
            if ok:
                patched += 1

        self.stdout.write(
            self.style.SUCCESS(f'\nDone — {patched}/{total} master playlist(s) patched.')
        )
