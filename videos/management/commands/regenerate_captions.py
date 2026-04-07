"""
Management command: regenerate_captions
========================================
Re-queues the generate_captions_task Celery task for videos that are
missing auto-generated subtitles (or all ready videos).

Usage:
    # Queue caption generation for all ready videos without auto-captions
    python manage.py regenerate_captions

    # Include videos that already have captions (force re-generate)
    python manage.py regenerate_captions --force

    # Single video by UUID
    python manage.py regenerate_captions --video-id <uuid>

    # Dry-run: show what would be queued
    python manage.py regenerate_captions --dry-run

    # Run synchronously in this process (no Celery required)
    python manage.py regenerate_captions --sync
"""

from django.core.management.base import BaseCommand

from videos.models import Video, Subtitle
from videos.tasks import generate_captions_task


class Command(BaseCommand):
    help = 'Re-queue caption (subtitle) generation for ready videos'

    def add_arguments(self, parser):
        parser.add_argument(
            '--video-id',
            type=str,
            default=None,
            metavar='UUID',
            help='Regenerate captions for a single video by its UUID',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            default=False,
            help='Re-generate even if auto-captions already exist (deletes existing)',
        )
        parser.add_argument(
            '--language',
            type=str,
            default='en',
            metavar='LANG',
            help='Language code to transcribe (default: en)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Print which videos would be queued without actually queuing them',
        )
        parser.add_argument(
            '--sync',
            action='store_true',
            default=False,
            help='Run tasks synchronously in this process instead of via Celery',
        )

    def handle(self, *args, **options):
        video_id = options['video_id']
        dry_run  = options['dry_run']
        run_sync = options['sync']
        force    = options['force']
        language = options['language']

        # ── Build queryset ────────────────────────────────────────────────────
        if video_id:
            qs = Video.objects.filter(id=video_id, status=Video.STATUS_READY)
            if not qs.exists():
                self.stderr.write(self.style.ERROR(
                    f'No ready video found with id={video_id}'
                ))
                return
        else:
            qs = Video.objects.filter(status=Video.STATUS_READY).order_by('created_at')

        if not force:
            # Exclude videos that already have auto-generated captions for this language
            already_done = Subtitle.objects.filter(
                is_auto_generated=True,
                language=language,
            ).values_list('video_id', flat=True)
            qs = qs.exclude(id__in=already_done)

        total = qs.count()

        if total == 0:
            self.stdout.write(self.style.WARNING(
                'No videos need captions — nothing to do. '
                '(Use --force to re-generate existing captions.)'
            ))
            return

        mode = 'DRY RUN' if dry_run else ('SYNC' if run_sync else 'CELERY')
        self.stdout.write(f'\n[{mode}] Queuing caption generation for {total} video(s)…\n')

        queued = 0
        for video in qs:
            title_display = (video.title or str(video.id))[:60]
            self.stdout.write(f'  {video.id}  {title_display}')

            if dry_run:
                self.stdout.write(self.style.WARNING('    → skipped (dry-run)\n'))
                continue

            try:
                if force:
                    # Remove existing auto captions so the task doesn't skip
                    deleted, _ = Subtitle.objects.filter(
                        video=video,
                        language=language,
                        is_auto_generated=True,
                    ).delete()
                    if deleted:
                        self.stdout.write(f'    → deleted {deleted} existing subtitle(s)')

                if run_sync:
                    generate_captions_task(str(video.id), language=language)
                    self.stdout.write(self.style.SUCCESS('    → done\n'))
                else:
                    generate_captions_task.apply_async(
                        args=[str(video.id)],
                        kwargs={'language': language},
                        queue='captions',
                    )
                    self.stdout.write(self.style.SUCCESS('    → queued\n'))
                queued += 1
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f'    → ERROR: {exc}\n'))

        # ── Summary ───────────────────────────────────────────────────────────
        if dry_run:
            self.stdout.write(
                self.style.WARNING(f'\nDry run complete — {total} video(s) would be queued.')
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f'\nDone — {queued}/{total} video(s) queued.')
            )
            if not run_sync:
                self.stdout.write(
                    'Make sure your Celery worker is running:\n'
                    '  celery -A ClipStream worker -l info -Q processing,captions,default\n'
                )
