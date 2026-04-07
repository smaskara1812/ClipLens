"""
Management command: propagate_identities
=========================================
After you manually name a person and review their face crops on /faces/<id>/,
run this command to automatically rename any auto-generated "Person N" identities
that are actually the same person based on face embedding similarity.

What it does:
  1. Re-computes reference embeddings for all named identities using only
     non-rejected face crops (confirmed + unreviewed faces count toward the mean).
  2. Compares every auto-named identity ("Person N") against all named identities.
  3. If the similarity is above the threshold, merges the auto identity into the
     named one — all its DetectedFace rows are re-pointed, and the auto identity
     is deleted.
  4. Updates VideoFrame.face_names for any affected video frames.

Workflow example:
  1. Upload a few videos.  Face recognition creates "Person 1", "Person 2", etc.
  2. Go to /faces/ — find "Person 1" — review crops — rename to "Kashish".
  3. Go to /faces/  — find bad crops under Kashish — click ✗ to reject them.
  4. Run: python manage.py propagate_identities
     → Any other "Person N" that looks like Kashish gets merged into "Kashish".

Usage:
    python manage.py propagate_identities
    python manage.py propagate_identities --threshold 0.50   # stricter matching
    python manage.py propagate_identities --dry-run          # preview only
"""

import json
import math

import numpy as np
from django.core.management.base import BaseCommand

from videos.models import DetectedFace, FaceIdentity, VideoFrame


def _cosine_sim(a, b):
    """Cosine similarity between two lists / numpy arrays."""
    a, b = np.array(a, dtype=np.float32), np.array(b, dtype=np.float32)
    denom = (np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom > 0 else 0.0


def _mean_embedding(embeddings):
    """Return the mean of a list of 512-dim float lists, normalised to unit length."""
    arr = np.array(embeddings, dtype=np.float32)
    mean = arr.mean(axis=0)
    norm = np.linalg.norm(mean)
    return (mean / norm).tolist() if norm > 0 else mean.tolist()


class Command(BaseCommand):
    help = 'Propagate named face identities across videos by re-matching auto-named ones'

    def add_arguments(self, parser):
        parser.add_argument(
            '--threshold',
            type=float,
            default=0.45,
            metavar='FLOAT',
            help='Cosine similarity threshold for a match (default: 0.45). '
                 'Higher = stricter (fewer merges).',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Show what would be merged without making any changes.',
        )

    def handle(self, *args, **options):
        threshold = options['threshold']
        dry_run   = options['dry_run']

        self.stdout.write(
            f'\n[{"DRY RUN — " if dry_run else ""}propagate_identities]  '
            f'threshold={threshold}\n'
        )

        # ── Step 1: Load named identities ─────────────────────────────────────
        named = list(FaceIdentity.objects.filter(is_auto_named=False).exclude(ref_embedding=''))
        if not named:
            self.stdout.write(self.style.WARNING(
                'No named identities found.  '
                'Tag at least one identity on /faces/ first, then re-run.\n'
            ))
            return
        self.stdout.write(f'Named identities: {len(named)}\n')

        # ── Step 2: Re-compute ref embeddings from non-rejected faces ─────────
        self.stdout.write('Re-computing reference embeddings from reviewed crops…\n')
        updated_named = []
        for identity in named:
            # Collect all non-rejected embeddings for this identity
            face_qs = (
                DetectedFace.objects
                .filter(identity=identity)
                .exclude(status=DetectedFace.STATUS_REJECTED)
                .exclude(embedding='')
            )
            embs = []
            for df in face_qs:
                try:
                    embs.append(json.loads(df.embedding))
                except Exception:
                    pass

            if not embs:
                self.stdout.write(
                    self.style.WARNING(
                        f'  "{identity.name}" — no usable embeddings, keeping old ref.\n'
                    )
                )
                updated_named.append((identity, json.loads(identity.ref_embedding)))
                continue

            new_ref = _mean_embedding(embs)
            if not dry_run:
                identity.ref_embedding = json.dumps(new_ref)
                identity.save(update_fields=['ref_embedding'])
            updated_named.append((identity, new_ref))
            self.stdout.write(
                f'  "{identity.name}" — ref recomputed from {len(embs)} face(s).\n'
            )

        # ── Step 3: Load auto-named identities ────────────────────────────────
        auto = list(FaceIdentity.objects.filter(is_auto_named=True).exclude(ref_embedding=''))
        self.stdout.write(f'\nAuto-named identities to check: {len(auto)}\n\n')

        if not auto:
            self.stdout.write(self.style.SUCCESS('Nothing to merge — no auto-named identities.\n'))
            return

        merged_count = 0
        affected_video_ids = set()

        for auto_id in auto:
            try:
                auto_emb = json.loads(auto_id.ref_embedding)
            except Exception:
                continue

            best_named, best_sim = None, -1.0
            for (named_id, named_emb) in updated_named:
                sim = _cosine_sim(auto_emb, named_emb)
                if sim > best_sim:
                    best_sim, best_named = sim, named_id

            label = f'"{auto_id.name}" (id={auto_id.pk})'
            if best_sim >= threshold and best_named is not None:
                self.stdout.write(
                    f'  {label}  →  MERGE into "{best_named.name}"  '
                    f'(sim={best_sim:.3f})\n'
                )
                if not dry_run:
                    # Collect affected video IDs before reassigning
                    vids = (
                        DetectedFace.objects
                        .filter(identity=auto_id)
                        .values_list('video_id', flat=True)
                        .distinct()
                    )
                    affected_video_ids.update(str(v) for v in vids)

                    # Re-point all DetectedFace rows
                    DetectedFace.objects.filter(identity=auto_id).update(identity=best_named)

                    # Delete the now-empty auto identity
                    auto_id.delete()
                merged_count += 1
            else:
                sim_str = f'{best_sim:.3f}' if best_named else 'n/a'
                self.stdout.write(
                    f'  {label}  →  no match  '
                    f'(best_sim={sim_str})\n'
                )

        # ── Step 4: Update VideoFrame.face_names for affected frames ──────────
        if affected_video_ids and not dry_run:
            self.stdout.write(
                f'\nUpdating face_names on VideoFrame rows for '
                f'{len(affected_video_ids)} video(s)…\n'
            )
            from videos.models import Video
            for vid_id in affected_video_ids:
                try:
                    video = Video.objects.get(id=vid_id)
                    for vf in VideoFrame.objects.filter(video=video):
                        names = set(
                            DetectedFace.objects
                            .filter(frame=vf)
                            .exclude(identity__isnull=True)
                            .values_list('identity__name', flat=True)
                            .distinct()
                        )
                        VideoFrame.objects.filter(pk=vf.pk).update(
                            face_names=', '.join(sorted(names))
                        )
                except Exception as exc:
                    self.stderr.write(f'  Warning: could not update video {vid_id}: {exc}\n')

        # ── Summary ───────────────────────────────────────────────────────────
        self.stdout.write('\n')
        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f'Dry run complete — {merged_count} identity/identities would be merged.\n'
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f'Done — {merged_count} identity/identities merged.\n'
                )
            )
