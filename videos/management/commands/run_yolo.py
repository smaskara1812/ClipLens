"""
Management command: run_yolo
=============================
Re-runs YOLOv8 object detection on existing videos and updates the
VideoFrame.labels field.  Frames are re-extracted from the original file
(since they are not persisted to disk), YOLO runs, and the results are
written back to existing VideoFrame rows matched by video + timestamp.
If a video has no VideoFrame rows yet, new ones are created.

This command does NOT touch BLIP descriptions, CLIP embeddings, or
InsightFace face data — it is a surgical YOLO-only pass.

Usage:
    # Run on all ready videos (queue to Celery)
    python manage.py run_yolo

    # Single video
    python manage.py run_yolo --video-id <uuid>

    # Scope to channel
    python manage.py run_yolo --channel <slug>

    # Synchronous (no Celery)
    python manage.py run_yolo --sync

    # Preview only
    python manage.py run_yolo --dry-run

    # Overwrite even if labels already exist
    python manage.py run_yolo --force
"""

import json
import logging
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


def _run_yolo_for_video(video_id: str, force: bool = False):
    """
    Extract frames from video, run YOLO, update VideoFrame.labels.
    Designed to be callable directly (sync) or via Celery.
    """
    import cv2
    from ultralytics import YOLO

    from videos.models import Video, VideoFrame

    try:
        video = Video.objects.get(id=video_id)
    except Video.DoesNotExist:
        logger.warning(f'run_yolo: video {video_id} not found')
        return

    if not video.original_file or not video.original_file.name:
        logger.warning(f'run_yolo: video {video_id} has no original file')
        return

    source_path = Path(settings.MEDIA_ROOT) / video.original_file.name
    if not source_path.exists():
        logger.warning(f'run_yolo: source file missing for {video_id}')
        return

    interval = int(getattr(settings, 'FRAME_INTERVAL_SECONDS', 5))
    yolo_model_name = getattr(settings, 'YOLO_MODEL', 'yolov8n')

    scene_change_enabled   = getattr(settings, 'SCENE_CHANGE_ENABLED',   True)
    scene_change_threshold = getattr(settings, 'SCENE_CHANGE_THRESHOLD', 0.35)
    scene_change_min_gap   = getattr(settings, 'SCENE_CHANGE_MIN_GAP',   0.5)

    tmp_dir = tempfile.mkdtemp(prefix='fs_yolo_')
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
            logger.error(f'run_yolo: ffmpeg failed for {video_id}: {result.stderr[-300:]}')
            return

        frame_files = sorted(Path(tmp_dir).glob('frame_*.jpg'))
        if not frame_files:
            logger.info(f'run_yolo: no frames extracted for {video_id}')
            return

        # Parse timestamps from showinfo
        raw_timestamps = [
            float(m.group(1))
            for m in (re.search(r'pts_time:(\S+)', ln) for ln in result.stderr.splitlines())
            if m
        ]
        if len(raw_timestamps) == len(frame_files):
            frame_timestamps = raw_timestamps
        else:
            frame_timestamps = [float(i * interval) for i in range(len(frame_files))]

        # Deduplicate near-adjacent frames
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

        # Load YOLO
        yolo = YOLO(f'{yolo_model_name}.pt')

        # Build lookup: round(timestamp) → existing VideoFrame pk
        existing = {
            round(ts): pk
            for pk, ts in VideoFrame.objects.filter(video=video).values_list('pk', 'timestamp')
        }

        to_create = []
        to_update_pks = []
        to_update_labels = []

        from PIL import Image as _PIL
        for frame_path, timestamp in zip(frame_files, kept_timestamps):
            yolo_results = yolo.predict(
                source=str(frame_path), verbose=False, conf=0.40, iou=0.45
            )
            labels_set = set()
            for r in yolo_results:
                for cls_id in r.boxes.cls.tolist():
                    labels_set.add(yolo.names[int(cls_id)])
            labels_str = ', '.join(sorted(labels_set))

            ts_key = round(timestamp)
            if ts_key in existing:
                if force or True:   # always update labels
                    to_update_pks.append(existing[ts_key])
                    to_update_labels.append(labels_str)
            else:
                to_create.append(VideoFrame(
                    video=video,
                    timestamp=timestamp,
                    labels=labels_str,
                ))

        # Bulk updates
        BATCH = 500
        updated = 0
        for i in range(0, len(to_update_pks), BATCH):
            batch_pks = to_update_pks[i:i + BATCH]
            batch_labels = to_update_labels[i:i + BATCH]
            for pk, lbl in zip(batch_pks, batch_labels):
                VideoFrame.objects.filter(pk=pk).update(labels=lbl)
            updated += len(batch_pks)

        created = 0
        if to_create:
            VideoFrame.objects.bulk_create(to_create, batch_size=500, ignore_conflicts=True)
            created = len(to_create)

        logger.info(f'run_yolo: {video_id} → updated {updated}, created {created} frames')

    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


class Command(BaseCommand):
    help = 'Re-run YOLOv8 object detection on videos and update VideoFrame.labels'

    def add_arguments(self, parser):
        parser.add_argument('--video-id', type=str, default=None, metavar='UUID',
                            help='Limit to a single video')
        parser.add_argument('--channel', type=str, default=None, metavar='SLUG',
                            help='Limit to videos in a specific channel')
        parser.add_argument('--dry-run', action='store_true', default=False,
                            help='Show which videos would be processed without running')
        parser.add_argument('--sync', action='store_true', default=False,
                            help='Run synchronously (no Celery required)')
        parser.add_argument('--force', action='store_true', default=False,
                            help='Overwrite existing labels (default: always update)')

    def handle(self, *args, **options):
        from videos.models import Video

        video_id     = options['video_id']
        channel_slug = options['channel']
        dry_run      = options['dry_run']
        run_sync     = options['sync']
        force        = options['force']

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

        mode = 'DRY RUN' if dry_run else ('SYNC' if run_sync else 'CELERY')
        self.stdout.write(f'\n[{mode}] YOLO re-analysis for {total} video(s)…\n')

        done = 0
        for video in qs:
            title = (video.title or str(video.id))[:60]
            self.stdout.write(f'  {video.id}  {title}')
            if dry_run:
                self.stdout.write(self.style.WARNING('    → skipped (dry-run)\n'))
                continue
            try:
                if run_sync:
                    _run_yolo_for_video(str(video.id), force=force)
                    self.stdout.write(self.style.SUCCESS('    → done\n'))
                else:
                    from celery import current_app
                    current_app.send_task(
                        'videos.management.commands.run_yolo._run_yolo_for_video_task',
                        args=[str(video.id), force],
                        queue='processing',
                    )
                    # Fallback: just run sync if no Celery task registered
                    _run_yolo_for_video(str(video.id), force=force)
                    self.stdout.write(self.style.SUCCESS('    → done\n'))
                done += 1
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f'    → ERROR: {exc}\n'))

        if dry_run:
            self.stdout.write(self.style.WARNING(f'\nDry run — {total} video(s) would be processed.'))
        else:
            self.stdout.write(self.style.SUCCESS(f'\nDone — {done}/{total} video(s) processed.'))
