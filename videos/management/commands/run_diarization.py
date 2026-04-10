"""
Management command: run_diarization
===================================
Runs (or queues) speaker diarization for one or more videos.

Usage:
    # Queue diarization for all matching videos
    python manage.py run_diarization

    # Single video by UUID
    python manage.py run_diarization --video-id <uuid>

    # Include multiple statuses
    python manage.py run_diarization --include-status ready,processing

    # Dry-run: print candidates without queueing
    python manage.py run_diarization --dry-run

    # Re-run diarization even for already-labeled videos
    python manage.py run_diarization --force

    # Run synchronously in this process (no Celery required)
    python manage.py run_diarization --sync
"""

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Count, Q

from videos.models import Video, VideoSegment
from videos.tasks import run_diarization_task


class Command(BaseCommand):
    help = 'Run speaker diarization for videos that have transcripts but are not diarized yet'

    def add_arguments(self, parser):
        parser.add_argument(
            '--video-id',
            type=str,
            default=None,
            metavar='UUID',
            help='Run diarization for a single video by UUID',
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
            help='Print which non-diarized videos would run without queueing tasks',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            default=False,
            help='Include videos that already have diarization labels',
        )
        parser.add_argument(
            '--sync',
            action='store_true',
            default=False,
            help='Run diarization in-process instead of queueing Celery tasks',
        )

    def handle(self, *args, **options):
        video_id = options['video_id']
        dry_run = options['dry_run']
        force = options['force']
        run_sync = options['sync']
        statuses = [s.strip() for s in options['include_status'].split(',') if s.strip()]

        hf_token = getattr(settings, 'HF_TOKEN', '')
        if not hf_token:
            self.stderr.write(self.style.ERROR(
                'HF_TOKEN is not configured. Add it to .env before running diarization.'
            ))
            return

        if video_id:
            qs = Video.objects.filter(id=video_id)
            if not qs.exists():
                self.stderr.write(self.style.ERROR(f'No video found with id={video_id}'))
                return
        else:
            qs = Video.objects.filter(status__in=statuses).order_by('created_at')

        # Always require transcript segments; default excludes already diarized videos.
        qs = qs.annotate(
            total_segments=Count('segments', distinct=True),
            labelled_segments=Count(
                'segments',
                filter=Q(segments__speaker_label__isnull=False) & ~Q(segments__speaker_label=''),
                distinct=True,
            ),
        ).filter(total_segments__gt=0)
        if not force:
            qs = qs.filter(labelled_segments=0)
        qs = qs.distinct()
        total = qs.count()

        if total == 0:
            msg = 'No videos with transcript segments matched — nothing to do.' if force \
                else 'No non-diarized videos matched — nothing to do.'
            self.stdout.write(self.style.WARNING(msg))
            return

        mode = 'DRY RUN' if dry_run else ('SYNC' if run_sync else 'CELERY')
        self.stdout.write(f'\n[{mode}] Running diarization for {total} video(s)…\n')

        processed = 0
        for video in qs:
            seg_count = VideoSegment.objects.filter(video=video).count()
            title_display = (video.title or str(video.id))[:60]
            self.stdout.write(f'  {video.id}  {title_display}  (segments={seg_count})')

            if dry_run:
                self.stdout.write(self.style.WARNING('    -> skipped (dry-run)\n'))
                continue

            try:
                if run_sync:
                    run_diarization_task(str(video.id))
                    self.stdout.write(self.style.SUCCESS('    -> done\n'))
                else:
                    run_diarization_task.apply_async(args=[str(video.id)], queue='captions')
                    self.stdout.write(self.style.SUCCESS('    -> queued\n'))
                processed += 1
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f'    -> ERROR: {exc}\n'))

        if dry_run:
            self.stdout.write(
                self.style.WARNING(f'\nDry run complete — {total} video(s) would be processed.')
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f'\nDone — {processed}/{total} video(s) processed.')
            )
            if not run_sync:
                self.stdout.write(
                    'Make sure your Celery worker is running:\n'
                    '  celery -A cliplens worker -l info -Q processing,captions,default\n'
                )
