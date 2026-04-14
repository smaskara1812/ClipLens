"""
Management command: generate_seek_sprites
==========================================
Generate (or regenerate) seek-bar sprite sheets for existing videos.
New videos get sprites automatically during processing; this command
backfills videos that were ingested before the feature existed.

Usage:
    # Backfill all ready videos that don't have a sprite yet
    python manage.py generate_seek_sprites

    # Single video by UUID
    python manage.py generate_seek_sprites --video-id <uuid>

    # All videos in a channel (by slug)
    python manage.py generate_seek_sprites --channel <slug>

    # Force regenerate even if a sprite already exists
    python manage.py generate_seek_sprites --force

    # Dry-run: show what would be processed without running FFmpeg
    python manage.py generate_seek_sprites --dry-run

    # Run synchronously in this process (no Celery required)
    python manage.py generate_seek_sprites --sync
"""

import os

from django.conf import settings
from django.core.management.base import BaseCommand

from videos.models import Channel, Video
from videos.tasks import generate_seek_thumbnails_task


class Command(BaseCommand):
    help = 'Generate seek-bar sprite sheets for videos (backfill or targeted regeneration)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--video-id',
            type=str,
            default=None,
            metavar='UUID',
            help='Generate sprite for a single video by UUID',
        )
        parser.add_argument(
            '--channel',
            type=str,
            default=None,
            metavar='SLUG',
            help='Limit to videos belonging to a specific channel (by slug)',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            default=False,
            help='Regenerate sprite even if one already exists',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Print which videos would be processed without running FFmpeg',
        )
        parser.add_argument(
            '--sync',
            action='store_true',
            default=False,
            help='Run tasks synchronously in this process instead of queuing to Celery',
        )

    def handle(self, *args, **options):
        video_id = options['video_id']
        channel_slug = options['channel']
        force = options['force']
        dry_run = options['dry_run']
        run_sync = options['sync']

        if not getattr(settings, 'SEEK_THUMBNAILS_ENABLED', True):
            self.stdout.write(self.style.WARNING(
                'SEEK_THUMBNAILS_ENABLED is False — set it to true before running this command.'
            ))
            return

        # ── Build queryset ────────────────────────────────────────────────────
        if video_id:
            qs = Video.objects.filter(id=video_id, status='ready')
            if not qs.exists():
                self.stderr.write(self.style.ERROR(
                    f'No ready video found with id={video_id}'
                ))
                return
        else:
            qs = Video.objects.filter(status='ready').order_by('created_at')

            if channel_slug:
                try:
                    channel = Channel.objects.get(slug=channel_slug)
                except Channel.DoesNotExist:
                    self.stderr.write(self.style.ERROR(
                        f'No channel found with slug="{channel_slug}"'
                    ))
                    return
                qs = qs.filter(channel=channel)

        # Exclude videos that already have a valid sprite (unless --force)
        if not force:
            missing = []
            for v in qs:
                if v.seek_sprite:
                    sprite_path = os.path.join(settings.MEDIA_ROOT, v.seek_sprite)
                    if os.path.exists(sprite_path):
                        continue  # already has it — skip
                missing.append(v)
            qs = missing
        else:
            qs = list(qs)

        # Filter out videos with no source file
        eligible = []
        skipped_no_file = []
        for v in qs:
            if not v.original_file or not v.original_file.name:
                skipped_no_file.append(v)
                continue
            try:
                src = v.original_file.path
            except Exception:
                skipped_no_file.append(v)
                continue
            if not os.path.exists(src):
                skipped_no_file.append(v)
            else:
                eligible.append(v)

        total = len(eligible)

        if skipped_no_file:
            self.stdout.write(self.style.WARNING(
                f'\n{len(skipped_no_file)} video(s) skipped — original file not found:'
            ))
            for v in skipped_no_file:
                self.stdout.write(f'  {v.id}  {(v.title or str(v.id))[:60]}')

        if total == 0:
            self.stdout.write(self.style.WARNING('\nNo eligible videos — nothing to do.'))
            return

        mode = 'DRY RUN' if dry_run else ('SYNC' if run_sync else 'CELERY')
        self.stdout.write(f'\n[{mode}] Generating seek sprites for {total} video(s)…\n')

        processed = 0
        for video in eligible:
            title_display = (video.title or str(video.id))[:60]
            sprite_status = 'existing' if video.seek_sprite else 'missing'
            self.stdout.write(f'  {video.id}  {title_display}  [{sprite_status}]')

            if dry_run:
                self.stdout.write(self.style.WARNING('    → skipped (dry-run)\n'))
                continue

            try:
                if run_sync:
                    generate_seek_thumbnails_task(str(video.id))
                    self.stdout.write(self.style.SUCCESS('    → done\n'))
                else:
                    generate_seek_thumbnails_task.apply_async(
                        args=[str(video.id)],
                        queue='processing',
                    )
                    self.stdout.write(self.style.SUCCESS('    → queued\n'))
                processed += 1
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f'    → ERROR: {exc}\n'))

        # ── Summary ───────────────────────────────────────────────────────────
        if dry_run:
            self.stdout.write(
                self.style.WARNING(f'\nDry run complete — {total} video(s) would be processed.')
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(f'\nDone — {processed}/{total} video(s) {"processed" if run_sync else "queued"}.')
            )
            if not run_sync:
                self.stdout.write(
                    'Make sure your Celery worker is running:\n'
                    '  celery -A cliplens worker -l info -Q processing,captions,default\n'
                )
