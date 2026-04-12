"""
Management command: rebuild_speaker_face_suggestions
==================================================
Recomputes pending voice→face suggestions from existing DB data only
(no pyannote / no Celery). Use after restore, heuristic tuning, or new face data.

Usage:
    python manage.py rebuild_speaker_face_suggestions
    python manage.py rebuild_speaker_face_suggestions --video-id <uuid>
    python manage.py rebuild_speaker_face_suggestions --channel my-channel
    python manage.py rebuild_speaker_face_suggestions --dry-run
    python manage.py rebuild_speaker_face_suggestions --limit 50
"""

from django.core.management.base import BaseCommand
from django.db.models import Count, Exists, OuterRef

from videos.models import DetectedFace, Video, VideoSegment
from videos.tasks import _upsert_speaker_face_suggestions


class Command(BaseCommand):
    help = (
        'Rebuild speaker→face suggestions from transcript segments + detected faces '
        '(no diarization re-run).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--video-id',
            type=str,
            default=None,
            metavar='UUID',
            help='Only process this video',
        )
        parser.add_argument(
            '--channel',
            type=str,
            default=None,
            metavar='SLUG',
            help='Only process videos in this channel slug',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=None,
            metavar='N',
            help='Max number of videos to process',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='List candidate videos only; do not write suggestions',
        )

    def handle(self, *args, **options):
        video_id = options['video_id']
        channel_slug = options['channel']
        limit = options['limit']
        dry_run = options['dry_run']

        has_spk = VideoSegment.objects.filter(
            video_id=OuterRef('pk'),
            speaker_identity__isnull=False,
        )
        has_face = DetectedFace.objects.filter(
            video_id=OuterRef('pk'),
            identity__isnull=False,
        )

        qs = (
            Video.objects.filter(status=Video.STATUS_READY)
            .annotate(
                _has_spk=Exists(has_spk),
                _has_face=Exists(has_face),
                seg_count=Count('segments', distinct=True),
            )
            .filter(_has_spk=True, _has_face=True)
            .order_by('-created_at')
        )

        if video_id:
            qs = qs.filter(id=video_id)
            if not qs.exists():
                self.stderr.write(self.style.ERROR(f'No matching video id={video_id}'))
                return

        if channel_slug:
            qs = qs.filter(channel__slug=channel_slug)
            if not qs.exists() and video_id is None:
                self.stderr.write(
                    self.style.ERROR(f'No candidate videos in channel slug={channel_slug!r}')
                )
                return

        if limit is not None:
            qs = list(qs[:limit])
        else:
            qs = list(qs)

        total = len(qs)
        if total == 0:
            self.stdout.write(self.style.WARNING('No videos with both diarized segments and faces.'))
            return

        self.stdout.write(
            f'{"[DRY RUN] " if dry_run else ""}{total} video(s) to process.\n'
        )

        if dry_run:
            for v in qs[:200]:
                self.stdout.write(f'  {v.id}  {(v.title or "")[:56]}')
            if total > 200:
                self.stdout.write(f'  … and {total - 200} more')
            return

        total_upserts = 0
        for v in qs:
            segments = list(
                VideoSegment.objects.filter(video=v)
                .select_related('speaker_identity')
                .order_by('start_seconds')
            )
            n = _upsert_speaker_face_suggestions(v, segments)
            total_upserts += n
            title = (v.title or str(v.id))[:50]
            self.stdout.write(f'  {v.id}  {title}  → {n} suggestion row(s) touched')

        self.stdout.write(self.style.SUCCESS(f'\nDone — {total_upserts} suggestion upsert(s) across {total} video(s).'))
