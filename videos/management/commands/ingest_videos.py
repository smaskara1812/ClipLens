"""
Management command: ingest_videos

Scans media/originals/ for video files that are not yet tracked in the database,
creates Video records for them, and kicks off HLS processing.

Usage examples
--------------
  # Ingest all new files in media/originals/ into channel "Company Training"
  python manage.py ingest_videos --channel "Company Training"

  # Ingest using channel slug instead of name
  python manage.py ingest_videos --channel-slug company-training

  # Dry run — list what would be ingested without creating anything
  python manage.py ingest_videos --channel "Company Training" --dry-run

  # Force re-process files that already have a Video record
  python manage.py ingest_videos --channel "Company Training" --reprocess

  # Set visibility for all ingested videos (default: public)
  python manage.py ingest_videos --channel "Company Training" --visibility private

  # Ingest synchronously (no Celery — useful if worker isn't running)
  python manage.py ingest_videos --channel "Company Training" --sync

Notes
-----
  - The video title is derived from the filename (extension stripped, underscores/
    hyphens replaced with spaces, capitalised).
  - Files already referenced by an existing Video.original_file are skipped unless
    --reprocess is given.
  - Supported extensions: .mp4 .mkv .mov .avi .wmv .flv .webm .m4v .ts .mts
"""

import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from videos.models import Channel, Video

VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.mov', '.avi', '.wmv', '.flv', '.webm', '.m4v', '.ts', '.mts'}

VISIBILITY_MAP = {
    'public':      Video.VISIBILITY_PUBLIC,
    'private':     Video.VISIBILITY_PRIVATE,
    'subscribers': Video.VISIBILITY_SUBSCRIBERS,
}


def _title_from_filename(stem: str) -> str:
    """'my_awesome_video' → 'My Awesome Video'"""
    return stem.replace('_', ' ').replace('-', ' ').strip().title()


def _relative_path(abs_path: Path) -> str:
    """Return a path relative to MEDIA_ROOT so it can be stored in FileField."""
    media_root = Path(settings.MEDIA_ROOT)
    try:
        return str(abs_path.relative_to(media_root))
    except ValueError:
        return str(abs_path)


class Command(BaseCommand):
    help = 'Ingest video files from media/originals/ into the database and start HLS processing'

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group()
        group.add_argument('--channel', metavar='NAME', help='Channel name (exact match, case-insensitive)')
        group.add_argument('--channel-slug', metavar='SLUG', help='Channel slug')

        parser.add_argument(
            '--subdir', metavar='DIR', default='',
            help='Subdirectory inside media/originals/ to scan (default: root of originals/)',
        )
        parser.add_argument(
            '--visibility', choices=list(VISIBILITY_MAP), default='public',
            help='Visibility for ingested videos (default: public)',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='List files that would be ingested without creating records',
        )
        parser.add_argument(
            '--reprocess', action='store_true',
            help='Re-queue processing for files that already have a Video record',
        )
        parser.add_argument(
            '--sync', action='store_true',
            help='Run HLS processing in a thread instead of Celery (no worker needed)',
        )

    def handle(self, *args, **options):
        # ── Resolve channel ────────────────────────────────────────────────────
        channel = None
        if options['channel']:
            channel = Channel.objects.filter(name__iexact=options['channel']).first()
            if not channel:
                raise CommandError(
                    f'Channel "{options["channel"]}" not found.\n'
                    f'  Available channels: {", ".join(Channel.objects.values_list("name", flat=True)) or "(none)"}'
                )
        elif options['channel_slug']:
            channel = Channel.objects.filter(slug=options['channel_slug']).first()
            if not channel:
                raise CommandError(f'Channel with slug "{options["channel_slug"]}" not found.')
        else:
            raise CommandError('Provide --channel "Name" or --channel-slug slug.')

        self.stdout.write(f'\n  Channel : {channel.name}  (/{channel.slug}/)')

        # ── Resolve scan directory ─────────────────────────────────────────────
        originals_root = Path(settings.MEDIA_ROOT) / 'originals'
        if options['subdir']:
            scan_dir = originals_root / options['subdir']
        else:
            scan_dir = originals_root

        if not scan_dir.exists():
            raise CommandError(f'Directory does not exist: {scan_dir}')

        self.stdout.write(f'  Scanning: {scan_dir}\n')

        # ── Collect files ──────────────────────────────────────────────────────
        files = sorted(
            p for p in scan_dir.iterdir()
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
        )

        if not files:
            self.stdout.write(self.style.WARNING('  No video files found.'))
            return

        # Build a set of already-tracked relative paths
        tracked = set(Video.objects.values_list('original_file', flat=True))

        new_files = []
        already_tracked = []
        for f in files:
            rel = _relative_path(f)
            if rel in tracked:
                already_tracked.append(f)
            else:
                new_files.append(f)

        self.stdout.write(f'  Found    : {len(files)} file(s)')
        self.stdout.write(f'  New      : {len(new_files)}  |  Already tracked: {len(already_tracked)}')
        if options['reprocess']:
            self.stdout.write(f'  --reprocess: will also re-queue {len(already_tracked)} tracked file(s)')
        self.stdout.write('')

        to_process = new_files + (already_tracked if options['reprocess'] else [])

        if not to_process:
            self.stdout.write(self.style.SUCCESS('  Nothing to do.'))
            return

        if options['dry_run']:
            self.stdout.write(self.style.WARNING('  DRY RUN — no records created:\n'))
            for f in to_process:
                title = _title_from_filename(f.stem)
                status = '(re-process)' if f in already_tracked else '(new)'
                self.stdout.write(f'    {status:14} {title}  [{f.name}]')
            return

        # ── Ingest ────────────────────────────────────────────────────────────
        visibility = VISIBILITY_MAP[options['visibility']]
        created = 0
        requeued = 0

        for f in to_process:
            rel = _relative_path(f)
            is_reprocess = f in already_tracked

            if is_reprocess:
                video = Video.objects.filter(original_file=rel).first()
                if not video:
                    continue
                self.stdout.write(f'  ↻ Re-queuing : {video.title}')
            else:
                title = _title_from_filename(f.stem)
                video = Video(
                    channel=channel,
                    title=title,
                    original_filename=f.name,
                    file_size=f.stat().st_size,
                    visibility=visibility,
                    status=Video.STATUS_PENDING,
                    uploaded_by=f'ingest_videos (channel={channel.slug})',
                )
                video.original_file = rel   # store relative path directly
                video.save()
                created += 1
                self.stdout.write(self.style.SUCCESS(f'  ✓ Created    : {title}  [{f.name}]'))

            # Dispatch processing
            self._dispatch(video, sync=options['sync'])
            if is_reprocess:
                requeued += 1

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'  Done — {created} video(s) created, {requeued} re-queued for processing.'
        ))
        if not options['sync']:
            self.stdout.write('  (Make sure your Celery worker is running to process the queue.)')

    # ── Dispatch helper ────────────────────────────────────────────────────────

    def _dispatch(self, video, *, sync: bool):
        from videos import tasks as _tasks
        from videos.services import process_video
        import threading

        if sync:
            self.stdout.write(f'    → Processing synchronously…')
            process_video(str(video.id))
        else:
            try:
                _tasks.process_video_task.apply_async(
                    args=[str(video.id)],
                    queue='processing',
                )
            except Exception as exc:
                self.stdout.write(self.style.WARNING(
                    f'    ⚠ Celery unavailable ({exc}), falling back to thread.'
                ))
                threading.Thread(
                    target=process_video, args=(str(video.id),), daemon=True
                ).start()
