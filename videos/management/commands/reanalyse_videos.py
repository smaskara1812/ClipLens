"""
Management command: reanalyse_videos
=====================================
Re-queues the analyze_video_frames_task Celery task for every ready video
(or a single video by ID).  Useful when you:

  • Add a new AI model (e.g. CLIP, Florence-2) and want embeddings/captions
    generated for existing uploads
  • Change FRAME_INTERVAL_SECONDS, HLS_QUALITIES, or any analysis setting
  • Fix a bug in face clustering / scene captioning

The full pipeline that re-runs for each video:
  Phase A — YOLO object detection (labels per frame)
  Phase A — BLIP / Florence-2 scene captions (description per frame)
  Phase A — CLIP image embeddings (clip_embedding per frame, used for visual search)
  Phase B — InsightFace face recognition (DetectedFace + FaceIdentity rows)

The command does NOT re-encode video — it re-extracts frames from the original
file and runs all inference fresh.

Usage:
    # Re-analyse all ready videos (queued to Celery, non-blocking)
    python manage.py reanalyse_videos

    # Single video
    python manage.py reanalyse_videos --video-id <uuid>

    # Dry-run: show what would be queued without actually queuing
    python manage.py reanalyse_videos --dry-run

    # Run synchronously in the current process (no Celery required)
    python manage.py reanalyse_videos --sync
"""

from django.core.management.base import BaseCommand

from videos.models import Video
from videos.tasks import analyze_video_frames_task


class Command(BaseCommand):
    help = 'Re-queue frame analysis (YOLO + captions + CLIP + faces) for videos'

    def add_arguments(self, parser):
        parser.add_argument(
            '--video-id',
            type=str,
            default=None,
            metavar='UUID',
            help='Re-analyse a single video by its UUID (omit to process all ready videos)',
        )
        parser.add_argument(
            '--include-status',
            type=str,
            default='ready',
            metavar='STATUS',
            help=(
                'Comma-separated list of statuses to include '
                '(default: "ready"). '
                'Example: --include-status ready,processing'
            ),
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
        video_id     = options['video_id']
        dry_run      = options['dry_run']
        run_sync     = options['sync']
        statuses     = [s.strip() for s in options['include_status'].split(',')]

        # ── Build queryset ────────────────────────────────────────────────────
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
        self.stdout.write(
            f'\n[{mode}] Queuing frame analysis for {total} video(s)…\n'
        )

        queued = 0
        for video in qs:
            title_display = (video.title or str(video.id))[:60]
            self.stdout.write(f'  {video.id}  {title_display}')

            if dry_run:
                self.stdout.write(self.style.WARNING('    → skipped (dry-run)\n'))
                continue

            try:
                if run_sync:
                    # Run directly in this process — blocks until complete
                    analyze_video_frames_task(str(video.id))
                    self.stdout.write(self.style.SUCCESS('    → done\n'))
                else:
                    # Send to Celery worker
                    analyze_video_frames_task.apply_async(
                        args=[str(video.id)],
                        queue='processing',
                    )
                    self.stdout.write(self.style.SUCCESS('    → queued\n'))
                queued += 1
            except Exception as exc:
                self.stderr.write(
                    self.style.ERROR(f'    → ERROR: {exc}\n')
                )

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
