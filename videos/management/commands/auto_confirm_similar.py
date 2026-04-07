"""
Management command: auto_confirm_similar
=========================================
Retroactively auto-confirms DetectedFace crops that are already very close
to their identity's reference embedding (cosine similarity >= threshold).

These are crops that the face-clustering algorithm already grouped confidently —
the only reason they sit as UNREVIEWED is that they were ingested before the
auto-confirm logic was added to analyze_video_frames_task.

Workflow:
  1. Load all UNREVIEWED DetectedFace rows that have an embedding stored.
  2. For each one, compare its embedding to its FaceIdentity.ref_embedding.
  3. If similarity >= threshold → mark CONFIRMED.
  4. Optionally scope to a single video or identity.

Usage:
    # Confirm all high-confidence unreviewed crops (default threshold 0.75)
    python manage.py auto_confirm_similar

    # Stricter threshold (fewer auto-confirms)
    python manage.py auto_confirm_similar --threshold 0.82

    # Preview only (no DB writes)
    python manage.py auto_confirm_similar --dry-run

    # Scope to one video
    python manage.py auto_confirm_similar --video-id <uuid>

    # Scope to one identity
    python manage.py auto_confirm_similar --identity-id <int>
"""

import json

import numpy as np
from django.conf import settings
from django.core.management.base import BaseCommand

from videos.models import DetectedFace, FaceIdentity


def _cosine_sim(a, b):
    a, b = np.array(a, dtype=np.float32), np.array(b, dtype=np.float32)
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / denom) if denom > 0 else 0.0


class Command(BaseCommand):
    help = 'Auto-confirm high-confidence unreviewed face crops based on embedding similarity'

    def add_arguments(self, parser):
        parser.add_argument(
            '--threshold',
            type=float,
            default=None,
            metavar='FLOAT',
            help='Cosine similarity threshold to auto-confirm. '
                 'Defaults to FACE_AUTO_CONFIRM_THRESHOLD in settings (currently '
                 f'{getattr(settings, "FACE_AUTO_CONFIRM_THRESHOLD", 0.75)}). '
                 'Higher = stricter (fewer confirms).',
        )
        parser.add_argument(
            '--video-id',
            type=str,
            default=None,
            metavar='UUID',
            help='Scope to a single video.',
        )
        parser.add_argument(
            '--identity-id',
            type=int,
            default=None,
            metavar='INT',
            help='Scope to a single FaceIdentity.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Preview which crops would be confirmed without writing anything.',
        )

    def handle(self, *args, **options):
        threshold   = options['threshold'] if options['threshold'] is not None else getattr(settings, 'FACE_AUTO_CONFIRM_THRESHOLD', 0.75)
        dry_run     = options['dry_run']
        video_id    = options['video_id']
        identity_id = options['identity_id']

        self.stdout.write(
            f'\n[{"DRY RUN — " if dry_run else ""}auto_confirm_similar]  '
            f'threshold={threshold}\n'
        )

        # ── Load candidate faces ───────────────────────────────────────────────
        qs = (
            DetectedFace.objects
            .filter(status=DetectedFace.STATUS_UNREVIEWED)
            .exclude(embedding='')
            .select_related('identity', 'video')
        )
        if video_id:
            qs = qs.filter(video_id=video_id)
        if identity_id:
            qs = qs.filter(identity_id=identity_id)

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.WARNING('No unreviewed faces with embeddings found.\n'))
            return

        self.stdout.write(f'Checking {total} unreviewed crop(s)…\n')

        # Cache ref embeddings per identity to avoid redundant DB hits
        ref_cache = {}

        to_confirm = []
        skipped_no_ref = 0
        skipped_low_sim = 0

        for face in qs.iterator(chunk_size=500):
            identity = face.identity
            if identity is None:
                skipped_no_ref += 1
                continue

            # Load ref embedding (cached)
            if identity.pk not in ref_cache:
                if identity.ref_embedding:
                    try:
                        ref_cache[identity.pk] = np.array(
                            json.loads(identity.ref_embedding), dtype=np.float32
                        )
                    except Exception:
                        ref_cache[identity.pk] = None
                else:
                    ref_cache[identity.pk] = None

            ref_emb = ref_cache[identity.pk]
            if ref_emb is None:
                skipped_no_ref += 1
                continue

            try:
                face_emb = np.array(json.loads(face.embedding), dtype=np.float32)
            except Exception:
                skipped_no_ref += 1
                continue

            sim = _cosine_sim(face_emb, ref_emb)
            if sim >= threshold:
                to_confirm.append(face.pk)
            else:
                skipped_low_sim += 1

        confirmed_count = len(to_confirm)
        self.stdout.write(
            f'  → {confirmed_count} would be confirmed  '
            f'({skipped_low_sim} below threshold, {skipped_no_ref} missing ref)\n'
        )

        if not dry_run and to_confirm:
            # Bulk update in batches
            BATCH = 1000
            total_updated = 0
            for i in range(0, len(to_confirm), BATCH):
                batch = to_confirm[i:i + BATCH]
                updated = DetectedFace.objects.filter(pk__in=batch).update(
                    status=DetectedFace.STATUS_CONFIRMED
                )
                total_updated += updated
            self.stdout.write(
                self.style.SUCCESS(f'\nDone — {total_updated} crop(s) confirmed.\n')
            )
        elif dry_run:
            self.stdout.write(
                self.style.WARNING(f'\nDry run — no changes made.\n')
            )
        else:
            self.stdout.write(self.style.SUCCESS('\nNothing to do.\n'))
