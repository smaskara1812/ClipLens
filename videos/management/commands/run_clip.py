"""
Management command: run_clip
=============================
Re-runs CLIP image embedding on existing videos and updates
VideoFrame.clip_embedding.  Frames are re-extracted, CLIP runs, and
results are written back to existing VideoFrame rows matched by
video + timestamp.

This command does NOT touch YOLO labels, BLIP descriptions, or face data.

Usage:
    # Fill missing CLIP embeddings on all ready videos
    python manage.py run_clip --sync

    # Single video
    python manage.py run_clip --video-id <uuid>

    # Scope to channel
    python manage.py run_clip --channel <slug>

    # Force-overwrite even existing embeddings
    python manage.py run_clip --force --sync

    # Preview only
    python manage.py run_clip --dry-run
"""

import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


def _run_clip_for_video(video_id: str, missing_only: bool = True):
    """
    Extract frames, compute CLIP embeddings, update VideoFrame.clip_embedding.
    """
    from videos.models import Video, VideoFrame

    try:
        video = Video.objects.get(id=video_id)
    except Video.DoesNotExist:
        logger.warning(f'run_clip: video {video_id} not found')
        return

    if not video.original_file or not video.original_file.name:
        logger.warning(f'run_clip: video {video_id} has no original file')
        return

    source_path = Path(settings.MEDIA_ROOT) / video.original_file.name
    if not source_path.exists():
        logger.warning(f'run_clip: source file missing for {video_id}')
        return

    interval = int(getattr(settings, 'FRAME_INTERVAL_SECONDS', 5))
    scene_change_enabled   = getattr(settings, 'SCENE_CHANGE_ENABLED',   True)
    scene_change_threshold = getattr(settings, 'SCENE_CHANGE_THRESHOLD', 0.35)
    scene_change_min_gap   = getattr(settings, 'SCENE_CHANGE_MIN_GAP',   0.5)

    tmp_dir = tempfile.mkdtemp(prefix='fs_clip_')
    try:
        frame_pattern = os.path.join(tmp_dir, 'frame_%05d.jpg')

        if scene_change_enabled:
            vf_filter = (
                f"select='eq(n,0)+gt(scene,{scene_change_threshold})"
                f"+gte(t-prev_selected_t,{interval})',"
                f"showinfo"
            )
        else:
            vf_filter = f"fps=1/{interval},showinfo"

        cmd = [
            settings.FFMPEG_PATH,
            '-i', str(source_path),
            '-vf', vf_filter,
            '-vsync', 'vfr',
            '-q:v', '3',
            '-f', 'image2',
            frame_pattern,
            '-y',
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            logger.error(f'run_clip: ffmpeg failed for {video_id}: {result.stderr[-300:]}')
            return

        frame_files = sorted(Path(tmp_dir).glob('frame_*.jpg'))
        if not frame_files:
            return

        raw_timestamps = [
            float(m.group(1))
            for m in (re.search(r'pts_time:(\S+)', ln) for ln in result.stderr.splitlines())
            if m
        ]
        if len(raw_timestamps) == len(frame_files):
            frame_timestamps = raw_timestamps
        else:
            frame_timestamps = [float(i * interval) for i in range(len(frame_files))]

        # Dedup near-adjacent frames
        kept_files, kept_timestamps = [], []
        last_t = -999.0
        for f, t in zip(frame_files, frame_timestamps):
            if t - last_t >= scene_change_min_gap:
                kept_files.append(f)
                kept_timestamps.append(t)
                last_t = t
            else:
                Path(f).unlink(missing_ok=True)
        frame_files, frame_timestamps = kept_files, kept_timestamps

        # Load CLIP
        try:
            import torch
            from transformers import CLIPProcessor, CLIPModel
            clip_processor = CLIPProcessor.from_pretrained('openai/clip-vit-base-patch32')
            clip_model = CLIPModel.from_pretrained('openai/clip-vit-base-patch32')
            clip_model.eval()
            logger.info(f'run_clip: CLIP loaded for {video_id}')
        except Exception as exc:
            logger.error(f'run_clip: CLIP unavailable: {exc}')
            return

        # Build lookup: round(ts) → (pk, has_embedding)
        existing = {
            round(ts): (pk, emb is not None)
            for pk, ts, emb in VideoFrame.objects
                .filter(video=video)
                .values_list('pk', 'timestamp', 'clip_embedding')
        }

        updated = 0
        import torch
        from PIL import Image as _PIL

        for frame_path, timestamp in zip(frame_files, frame_timestamps):
            ts_key = round(timestamp)
            if ts_key not in existing:
                continue
            pk, has_emb = existing[ts_key]
            if missing_only and has_emb:
                continue  # already has an embedding

            try:
                pil_img = _PIL.open(str(frame_path)).convert('RGB')
                inputs = clip_processor(images=pil_img, return_tensors='pt')
                with torch.no_grad():
                    feats = clip_model.get_image_features(**inputs)
                    feats = feats / feats.norm(dim=-1, keepdim=True)
                embedding = feats[0].tolist()
                VideoFrame.objects.filter(pk=pk).update(clip_embedding=embedding)
                updated += 1
            except Exception as exc:
                logger.warning(f'run_clip: error frame t={timestamp}: {exc}')

        logger.info(f'run_clip: {video_id} → {updated} embeddings written')

    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


class Command(BaseCommand):
    help = 'Re-run CLIP image embedding on videos and update VideoFrame.clip_embedding'

    def add_arguments(self, parser):
        parser.add_argument('--video-id', type=str, default=None, metavar='UUID')
        parser.add_argument('--channel', type=str, default=None, metavar='SLUG')
        parser.add_argument('--dry-run', action='store_true', default=False)
        parser.add_argument('--sync', action='store_true', default=False)
        parser.add_argument('--force', action='store_true', default=False,
                            help='Overwrite existing embeddings (default: missing-only)')

    def handle(self, *args, **options):
        from videos.models import Video

        video_id     = options['video_id']
        channel_slug = options['channel']
        dry_run      = options['dry_run']
        missing_only = not options['force']

        if video_id:
            qs = Video.objects.filter(id=video_id, status=Video.STATUS_READY)
        else:
            qs = Video.objects.filter(status=Video.STATUS_READY).order_by('created_at')
            if channel_slug:
                qs = qs.filter(channel__slug=channel_slug)

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.WARNING('No matching ready videos found.'))
            return

        mode = 'DRY RUN' if dry_run else 'SYNC'
        self.stdout.write(f'\n[{mode}] CLIP embedding for {total} video(s)…\n')
        if missing_only:
            self.stdout.write('  (missing-only: skipping frames that already have embeddings)\n')

        done = 0
        for video in qs:
            title = (video.title or str(video.id))[:60]
            self.stdout.write(f'  {video.id}  {title}')
            if dry_run:
                self.stdout.write(self.style.WARNING('    → skipped (dry-run)\n'))
                continue
            try:
                _run_clip_for_video(str(video.id), missing_only=missing_only)
                self.stdout.write(self.style.SUCCESS('    → done\n'))
                done += 1
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f'    → ERROR: {exc}\n'))

        if dry_run:
            self.stdout.write(self.style.WARNING(f'\nDry run — {total} video(s) would be processed.'))
        else:
            self.stdout.write(self.style.SUCCESS(f'\nDone — {done}/{total} video(s) processed.'))
