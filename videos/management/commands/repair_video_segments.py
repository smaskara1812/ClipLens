"""
Repair tool — populate VideoSegment rows for videos that have a VTT file on
disk but no transcript rows in the DB.

Usage:
    # Repair one specific video:
    python manage.py repair_video_segments --video <uuid>

    # Repair every video in this DB that has VTTs but no segments:
    python manage.py repair_video_segments --all

    # Operate against a specific tenant DB:
    python manage.py repair_video_segments --tenant <slug> --all

    # Dry run (show what would be repaired, don't write):
    python manage.py repair_video_segments --all --dry-run

The command parses the latest auto-generated (or any) VTT file for each
target video and inserts one VideoSegment row per cue. It DOES NOT overwrite
existing segments — if any segments exist for the video, that video is
skipped (use --force to override).
"""

import logging
import os
import re

from django.core.management.base import BaseCommand
from django.conf import settings

logger = logging.getLogger(__name__)

VTT_TS = re.compile(
    r'^(\d{1,2}):(\d{2})(?::(\d{2}))?\.(\d{3})\s+-->\s+'
    r'(\d{1,2}):(\d{2})(?::(\d{2}))?\.(\d{3})'
)


def _parse_vtt_ts(line):
    m = VTT_TS.match(line.strip())
    if not m:
        return None
    g = m.groups()
    if g[2] is not None:
        h1, m1, s1, ms1 = int(g[0]), int(g[1]), int(g[2]), int(g[3])
    else:
        h1, m1, s1, ms1 = 0, int(g[0]), int(g[1]), int(g[3])
    if g[6] is not None:
        h2, m2, s2, ms2 = int(g[4]), int(g[5]), int(g[6]), int(g[7])
    else:
        h2, m2, s2, ms2 = 0, int(g[4]), int(g[5]), int(g[7])
    start = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000
    end   = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000
    return (start, end)


def parse_vtt_file(path):
    """Yield (start_s, end_s, text) tuples from a WebVTT file."""
    with open(path, 'r', encoding='utf-8') as fh:
        lines = fh.readlines()
    i = 0
    while i < len(lines):
        ts = _parse_vtt_ts(lines[i])
        if ts is None:
            i += 1
            continue
        start, end = ts
        i += 1
        text_parts = []
        while i < len(lines) and lines[i].strip():
            text_parts.append(lines[i].rstrip('\n'))
            i += 1
        text = '\n'.join(text_parts).strip()
        if text and end > start:
            yield (start, end, text)


class Command(BaseCommand):
    help = 'Repair VideoSegment rows from existing VTT files on disk.'

    def add_arguments(self, parser):
        parser.add_argument('--video', help='Repair a single video by UUID')
        parser.add_argument('--all', action='store_true', help='Scan every video for missing segments')
        parser.add_argument('--tenant', help='Operate against a specific tenant DB (slug)')
        parser.add_argument('--dry-run', action='store_true', help='Show what would be done, do not write')
        parser.add_argument('--force', action='store_true',
                            help='Wipe existing segments + repopulate even if some exist')

    def handle(self, **opts):
        # Resolve which DB to work in, AND set the tenant media root so
        # FileField.path() resolves correctly inside the tenant's media folder.
        tenant_slug = opts.get('tenant')
        if tenant_slug:
            from tenants.models import Tenant
            from tenants.provisioning import _register_db_alias
            from tenants.storage import set_media_root
            try:
                t = Tenant.objects.using('control').get(slug=tenant_slug)
            except Tenant.DoesNotExist:
                self.stderr.write(self.style.ERROR(f'Tenant "{tenant_slug}" not found.'))
                return
            db_alias = t.db_name
            _register_db_alias(db_alias)
            # Set tenant-aware media root for this command's lifetime
            tenant_media = os.path.join(settings.MEDIA_ROOT, t.media_folder.rstrip('/'))
            set_media_root(tenant_media)
        else:
            db_alias = 'default'

        from videos.models import Video, Subtitle, VideoSegment

        # Pick target videos
        if opts.get('video'):
            videos = list(Video.objects.using(db_alias).filter(id=opts['video']))
            if not videos:
                self.stderr.write(self.style.ERROR(f'Video {opts["video"]} not found in {db_alias}'))
                return
        elif opts.get('all'):
            videos = list(Video.objects.using(db_alias).all())
        else:
            self.stderr.write('Pass --video <uuid> OR --all')
            return

        self.stdout.write(f'Scanning {len(videos)} video(s) in DB {db_alias}…')

        repaired = 0
        skipped_has_segments = 0
        skipped_no_vtt = 0
        total_segments_created = 0

        for v in videos:
            existing = VideoSegment.objects.using(db_alias).filter(video=v).count()
            if existing and not opts.get('force'):
                skipped_has_segments += 1
                continue
            # Pick the best subtitle to import from — prefer English auto
            sub = (
                Subtitle.objects.using(db_alias)
                .filter(video=v, is_auto_generated=True, is_translation=False)
                .order_by('language' if 1 else 'id')   # language='en' will sort first if present
                .first()
            )
            if not sub:
                # Fall back to any subtitle
                sub = Subtitle.objects.using(db_alias).filter(video=v).first()
            if not sub:
                skipped_no_vtt += 1
                continue

            # Resolve absolute path via storage
            try:
                path = sub.file.path
            except Exception:
                path = os.path.join(settings.MEDIA_ROOT, sub.file.name)
            if not os.path.exists(path):
                self.stdout.write(self.style.WARNING(f'  {v.id} — VTT not on disk: {path}'))
                skipped_no_vtt += 1
                continue

            try:
                cues = list(parse_vtt_file(path))
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f'  {v.id} — VTT parse error: {exc}'))
                continue
            if not cues:
                self.stdout.write(f'  {v.id} — 0 cues in {sub.file.name}')
                skipped_no_vtt += 1
                continue

            if opts.get('dry_run'):
                self.stdout.write(f'  [dry-run] {v.id} ({v.title[:40]}) — would create {len(cues)} segments from {sub.file.name}')
                continue

            if opts.get('force') and existing:
                VideoSegment.objects.using(db_alias).filter(video=v).delete()

            to_create = [
                VideoSegment(video=v, start_seconds=s, end_seconds=e, text=t)
                for (s, e, t) in cues
            ]
            VideoSegment.objects.using(db_alias).bulk_create(to_create)
            repaired += 1
            total_segments_created += len(cues)
            self.stdout.write(self.style.SUCCESS(
                f'  ✓ {v.id} ({v.title[:40]}) — inserted {len(cues)} segments'
            ))

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Done. repaired={repaired} segments_created={total_segments_created} '
            f'skipped_has_existing={skipped_has_segments} skipped_no_vtt={skipped_no_vtt}'
        ))
