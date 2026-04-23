"""
Management command: run_audio_events
====================================
Runs (or queues) non-speech audio event detection without diarization.

Usage:
    # Queue for all matching videos
    python manage.py run_audio_events

    # Single video by UUID
    python manage.py run_audio_events --video-id <uuid>

    # Include multiple statuses
    python manage.py run_audio_events --include-status ready,processing

    # Dry-run: print candidates without queueing
    python manage.py run_audio_events --dry-run

    # Run synchronously in this process (no Celery required)
    python manage.py run_audio_events --sync
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from videos.models import Video
from videos.tasks import detect_audio_events_task


class Command(BaseCommand):
    help = 'Run audio event detection for videos (without diarization)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--video-id',
            type=str,
            default=None,
            metavar='UUID',
            help='Run audio event detection for a single video by UUID',
        )
        parser.add_argument(
            '--include-status',
            type=str,
            default='ready',
            metavar='STATUS',
            help='Comma-separated statuses to include (default: "ready")',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Print candidate videos without queueing tasks',
        )
        parser.add_argument(
            '--sync',
            action='store_true',
            default=False,
            help='Run in-process instead of queueing Celery tasks',
        )

    def handle(self, *args, **options):
        if not getattr(settings, 'AUDIO_EVENTS_ENABLED', True):
            self.stdout.write(self.style.WARNING(
                'AUDIO_EVENTS_ENABLED is false — nothing to run.'
            ))
            return

        video_id = options['video_id']
        dry_run = options['dry_run']
        run_sync = options['sync']
        statuses = [s.strip() for s in options['include_status'].split(',') if s.strip()]

        if video_id:
            qs = Video.objects.filter(id=video_id)
            if not qs.exists():
                self.stderr.write(self.style.ERROR(f'No video found with id={video_id}'))
                return
        else:
            qs = Video.objects.filter(status__in=statuses).order_by('created_at')

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.WARNING('No videos matched — nothing to do.'))
            return

        mode = 'DRY RUN' if dry_run else ('SYNC' if run_sync else 'CELERY')
        self.stdout.write(f'\n[{mode}] Running audio event detection for {total} video(s)…\n')

        processed = 0
        queue = getattr(settings, 'AUDIO_EVENTS_QUEUE', 'audio')
        for video in qs:
            title_display = (video.title or str(video.id))[:60]
            self.stdout.write(f'  {video.id}  {title_display}  (status={video.status})')

            if dry_run:
                self.stdout.write(self.style.WARNING('    -> skipped (dry-run)\n'))
                continue

            try:
                if run_sync:
                    detect_audio_events_task(str(video.id))
                    self.stdout.write(self.style.SUCCESS('    -> done\n'))
                else:
                    detect_audio_events_task.apply_async(args=[str(video.id)], queue=queue)
                    self.stdout.write(self.style.SUCCESS(f'    -> queued ({queue})\n'))
                processed += 1
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f'    -> ERROR: {exc}\n'))

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'\nDry run complete — {total} video(s) would be processed.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'\nDone — {processed}/{total} video(s) processed.'
            ))
            if not run_sync:
                self.stdout.write(
                    'Make sure your Celery worker is running:\n'
                    f'  celery -A cliplens worker -l info -Q {queue}\n'
                )

