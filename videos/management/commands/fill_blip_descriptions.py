"""
Management command: fill_blip_descriptions
============================================
Finds videos whose VideoFrame rows all have an empty description (BLIP/Florence-2
caption) and re-queues the full frame-analysis task for those videos only.

Because frames are stored in a temporary directory during analysis (not persisted
to disk), re-running the full task is the only way to fill in missing data.  This
means the task also re-runs YOLO object detection, CLIP image embeddings, and
InsightFace face recognition — not just BLIP captions.

The most common cause is the old bulk_create filter that only saved frames where
YOLO or InsightFace detected something — frames with no detections were dropped
entirely, taking their BLIP/CLIP results with them.  That filter has since been
fixed to also keep frames that have a description or CLIP embedding.

Usage:
    # Preview which videos are missing descriptions (no changes made)
    python manage.py fill_blip_descriptions --dry-run

    # Queue affected videos to the Celery 'processing' worker
    python manage.py fill_blip_descriptions

    # Run synchronously in this process (no Celery required)
    python manage.py fill_blip_descriptions --sync
"""

from django.core.management.base import BaseCommand
from django.db.models import Count, Q

from videos.models import Video, VideoFrame
from videos.tasks import analyze_video_frames_task


class Command(BaseCommand):
    help = 'Re-queue frame analysis for videos missing BLIP scene descriptions'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Show which videos would be re-queued without doing anything',
        )
        parser.add_argument(
            '--sync',
            action='store_true',
            default=False,
            help='Run tasks synchronously in this process instead of via Celery',
        )
        parser.add_argument(
            '--video-id',
            type=str,
            default=None,
            metavar='UUID',
            help='Limit to a single video by UUID',
        )
        parser.add_argument(
            '--channel',
            type=str,
            default=None,
            metavar='SLUG',
            help='Limit to videos in a specific channel (by slug)',
        )

    def handle(self, *args, **options):
        dry_run      = options['dry_run']
        run_sync     = options['sync']
        video_id     = options.get('video_id')
        channel_slug = options.get('channel')

        ready_videos = Video.objects.filter(status=Video.STATUS_READY)
        if video_id:
            ready_videos = ready_videos.filter(id=video_id)
        if channel_slug:
            ready_videos = ready_videos.filter(channel__slug=channel_slug)

        # A video is "missing descriptions" if either:
        #   (a) it has no VideoFrame rows at all, OR
        #   (b) every VideoFrame row has description == ''
        missing = []
        for video in ready_videos:
            frames = VideoFrame.objects.filter(video=video)
            if not frames.exists():
                missing.append((video, 'no frames at all'))
            elif not frames.filter(~Q(description='')).exists():
                missing.append((video, f'{frames.count()} frames, all blank descriptions'))

        if not missing:
            self.stdout.write(self.style.SUCCESS(
                'All ready videos already have BLIP descriptions — nothing to do.'
            ))
            return

        mode = 'DRY RUN' if dry_run else ('SYNC' if run_sync else 'CELERY')
        self.stdout.write(f'\n[{mode}] {len(missing)} video(s) missing BLIP descriptions:\n')

        queued = 0
        for video, reason in missing:
            title = (video.title or str(video.id))[:60]
            self.stdout.write(f'  {video.id}  {title}')
            self.stdout.write(f'    reason: {reason}')

            if dry_run:
                self.stdout.write(self.style.WARNING('    → skipped (dry-run)\n'))
                continue

            try:
                if run_sync:
                    analyze_video_frames_task(str(video.id))
                    self.stdout.write(self.style.SUCCESS('    → done\n'))
                else:
                    analyze_video_frames_task.apply_async(
                        args=[str(video.id)],
                        queue='processing',
                    )
                    self.stdout.write(self.style.SUCCESS('    → queued\n'))
                queued += 1
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f'    → ERROR: {exc}\n'))

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'\nDry run complete — {len(missing)} video(s) would be re-queued.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(f'\nDone — {queued}/{len(missing)} video(s) queued.'))
            if not run_sync:
                self.stdout.write(
                    'Make sure your Celery worker is running:\n'
                    '  celery -A ClipStream worker -l info -Q processing,captions,default\n'
                )
