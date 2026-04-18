"""
Management command: run_blip
=============================
Re-runs scene captioning (BLIP or Florence-2) on existing videos and updates
VideoFrame.description.  Frames are re-extracted from the original file,
the caption model runs, and results are written back to existing VideoFrame
rows matched by video + timestamp.

This command does NOT touch YOLO labels, CLIP embeddings, or face data.

Usage:
    # Run on all ready videos (sync — captions are fast per frame)
    python manage.py run_blip --sync

    # Preview only
    python manage.py run_blip --dry-run

    # Single video
    python manage.py run_blip --video-id <uuid>

    # Scope to channel
    python manage.py run_blip --channel <slug>

    # Only fill frames that currently have an empty description
    python manage.py run_blip --missing-only

    # Use Florence-2 instead of BLIP (overrides SCENE_CAPTION_MODEL setting)
    python manage.py run_blip --model florence2
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


def _run_blip_for_video(video_id: str, missing_only: bool = True, model_override: str = None):
    """
    Extract frames, run BLIP/Florence-2, update VideoFrame.description.
    """
    from videos.models import Video, VideoFrame

    try:
        video = Video.objects.get(id=video_id)
    except Video.DoesNotExist:
        logger.warning(f'run_blip: video {video_id} not found')
        return

    if not video.original_file or not video.original_file.name:
        logger.warning(f'run_blip: video {video_id} has no original file')
        return

    source_path = Path(settings.MEDIA_ROOT) / video.original_file.name
    if not source_path.exists():
        logger.warning(f'run_blip: source file missing for {video_id}')
        return

    interval = int(getattr(settings, 'FRAME_INTERVAL_SECONDS', 5))
    scene_change_enabled   = getattr(settings, 'SCENE_CHANGE_ENABLED',   True)
    scene_change_threshold = getattr(settings, 'SCENE_CHANGE_THRESHOLD', 0.35)
    scene_change_min_gap   = getattr(settings, 'SCENE_CHANGE_MIN_GAP',   0.5)

    caption_model = (model_override or getattr(settings, 'SCENE_CAPTION_MODEL', 'blip')).lower()

    tmp_dir = tempfile.mkdtemp(prefix='fs_blip_')
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
            logger.error(f'run_blip: ffmpeg failed for {video_id}: {result.stderr[-300:]}')
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

        # Dedup
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

        # Load caption model
        blip_model = blip_processor = None
        florence_model = florence_processor = None

        if caption_model == 'florence2':
            try:
                import sys, types as _types, importlib.util as _ilu
                if 'flash_attn' not in sys.modules:
                    _stub = _types.ModuleType('flash_attn')
                    _stub.__spec__ = _ilu.spec_from_loader('flash_attn', loader=None)
                    _stub.__version__ = '0.0.0'
                    sys.modules['flash_attn'] = _stub
                from transformers import AutoProcessor, AutoModelForCausalLM
                florence_processor = AutoProcessor.from_pretrained(
                    'microsoft/Florence-2-base', trust_remote_code=True
                )
                florence_model = AutoModelForCausalLM.from_pretrained(
                    'microsoft/Florence-2-base', trust_remote_code=True
                )
                florence_model.eval()
                logger.info(f'run_blip: Florence-2 loaded for {video_id}')
            except Exception as exc:
                logger.error(f'run_blip: Florence-2 unavailable: {exc}')
                return
        else:
            try:
                import torch
                from transformers import BlipProcessor, BlipForConditionalGeneration
                blip_processor = BlipProcessor.from_pretrained('Salesforce/blip-image-captioning-base')
                blip_model = BlipForConditionalGeneration.from_pretrained(
                    'Salesforce/blip-image-captioning-base', torch_dtype=torch.float32
                )
                blip_model.eval()
                logger.info(f'run_blip: BLIP loaded for {video_id}')
            except Exception as exc:
                logger.error(f'run_blip: BLIP unavailable: {exc}')
                return

        # Build lookup: round(ts) → (pk, existing_description)
        existing = {
            round(ts): (pk, desc)
            for pk, ts, desc in VideoFrame.objects
                .filter(video=video)
                .values_list('pk', 'timestamp', 'description')
        }

        updated = 0
        import torch
        from PIL import Image as _PIL

        for frame_path, timestamp in zip(frame_files, frame_timestamps):
            ts_key = round(timestamp)
            if ts_key in existing:
                pk, existing_desc = existing[ts_key]
                if missing_only and existing_desc.strip():
                    continue   # already has a description
            else:
                continue   # no matching VideoFrame row — skip (use run_yolo first)

            description = ''
            try:
                pil_img = _PIL.open(str(frame_path)).convert('RGB')
                if blip_model is not None and blip_processor is not None:
                    inputs = blip_processor(pil_img, return_tensors='pt')
                    with torch.no_grad():
                        out = blip_model.generate(**inputs, max_new_tokens=50)
                    description = blip_processor.decode(out[0], skip_special_tokens=True)
                elif florence_model is not None and florence_processor is not None:
                    inputs = florence_processor(
                        text='<DETAILED_CAPTION>', images=pil_img, return_tensors='pt'
                    )
                    with torch.no_grad():
                        out = florence_model.generate(
                            input_ids=inputs['input_ids'],
                            pixel_values=inputs['pixel_values'],
                            max_new_tokens=60,
                            num_beams=2,
                        )
                    raw = florence_processor.batch_decode(out, skip_special_tokens=False)[0]
                    parsed = florence_processor.post_process_generation(
                        raw, task='<DETAILED_CAPTION>', image_size=(pil_img.width, pil_img.height)
                    )
                    description = parsed.get('<DETAILED_CAPTION>', '')
            except Exception as exc:
                logger.warning(f'run_blip: caption error frame t={timestamp}: {exc}')
                continue

            if description.strip():
                VideoFrame.objects.filter(pk=pk).update(description=description)
                updated += 1

        logger.info(f'run_blip: {video_id} → {updated} descriptions written')

    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


class Command(BaseCommand):
    help = 'Re-run BLIP/Florence-2 scene captioning on videos and update VideoFrame.description'

    def add_arguments(self, parser):
        parser.add_argument('--video-id', type=str, default=None, metavar='UUID')
        parser.add_argument('--channel', type=str, default=None, metavar='SLUG')
        parser.add_argument('--dry-run', action='store_true', default=False)
        parser.add_argument('--sync', action='store_true', default=False,
                            help='Run synchronously (default behaviour — always sync for captions)')
        parser.add_argument('--missing-only', action='store_true', default=True,
                            help='Only fill frames with an empty description (default: on)')
        parser.add_argument('--force', action='store_true', default=False,
                            help='Overwrite existing descriptions (sets --missing-only=False)')
        parser.add_argument('--model', type=str, default=None, metavar='blip|florence2',
                            help='Override the caption model (default: read from settings)')

    def handle(self, *args, **options):
        from videos.models import Video

        video_id     = options['video_id']
        channel_slug = options['channel']
        dry_run      = options['dry_run']
        missing_only = not options['force']
        model        = options['model']

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
        self.stdout.write(f'\n[{mode}] BLIP scene captioning for {total} video(s)…\n')
        if missing_only:
            self.stdout.write('  (missing-only: skipping frames that already have descriptions)\n')

        done = 0
        for video in qs:
            title = (video.title or str(video.id))[:60]
            self.stdout.write(f'  {video.id}  {title}')
            if dry_run:
                self.stdout.write(self.style.WARNING('    → skipped (dry-run)\n'))
                continue
            try:
                _run_blip_for_video(str(video.id), missing_only=missing_only, model_override=model)
                self.stdout.write(self.style.SUCCESS('    → done\n'))
                done += 1
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f'    → ERROR: {exc}\n'))

        if dry_run:
            self.stdout.write(self.style.WARNING(f'\nDry run — {total} video(s) would be processed.'))
        else:
            self.stdout.write(self.style.SUCCESS(f'\nDone — {done}/{total} video(s) processed.'))
