"""
Management command: run_insightface
=====================================
Re-runs InsightFace face detection + ArcFace embedding on existing videos.
Frames are re-extracted, faces detected, clustered into FaceIdentity rows,
and DetectedFace crops saved.

This command does NOT touch YOLO labels, BLIP descriptions, or CLIP embeddings.

Key behaviours:
  • Existing DetectedFace rows for the video are deleted before the new run
    (same as the main pipeline) to avoid duplicates.
  • Face clustering uses the same greedy cosine-similarity algorithm as the
    main analyze_video_frames_task.
  • The auto-confirm threshold from settings (FACE_AUTO_CONFIRM_THRESHOLD)
    is honoured.

Usage:
    # Run on all ready videos
    python manage.py run_insightface --sync

    # Single video
    python manage.py run_insightface --video-id <uuid>

    # Scope to channel
    python manage.py run_insightface --channel <slug>

    # Preview only
    python manage.py run_insightface --dry-run
"""

import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from django.conf import settings
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


def _cosine_sim(a, b):
    a, b = np.array(a, dtype=np.float32), np.array(b, dtype=np.float32)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return float(np.dot(a, b) / (na * nb)) if na > 0 and nb > 0 else 0.0


def _cluster_embeddings(embeddings, threshold=0.35):
    cluster_means, cluster_counts, assignments = [], [], []
    for emb in embeddings:
        emb = np.array(emb, dtype=np.float32)
        best_idx, best_sim = -1, -1.0
        for i, mean in enumerate(cluster_means):
            sim = _cosine_sim(emb, mean)
            if sim > best_sim:
                best_sim, best_idx = sim, i
        if best_sim >= threshold:
            n = cluster_counts[best_idx]
            cluster_means[best_idx] = (cluster_means[best_idx] * n + emb) / (n + 1)
            cluster_counts[best_idx] += 1
            assignments.append(best_idx)
        else:
            cluster_means.append(emb.copy())
            cluster_counts.append(1)
            assignments.append(len(cluster_means) - 1)
    return assignments, cluster_means


def _run_insightface_for_video(video_id: str):
    """
    Extract frames, run InsightFace, update DetectedFace / FaceIdentity.
    """
    import cv2
    from django.core.files.base import ContentFile

    from videos.models import Video, VideoFrame, DetectedFace, FaceIdentity

    try:
        video = Video.objects.get(id=video_id)
    except Video.DoesNotExist:
        logger.warning(f'run_insightface: video {video_id} not found')
        return

    if not video.original_file or not video.original_file.name:
        logger.warning(f'run_insightface: video {video_id} has no original file')
        return

    source_path = Path(settings.MEDIA_ROOT) / video.original_file.name
    if not source_path.exists():
        logger.warning(f'run_insightface: source file missing for {video_id}')
        return

    interval = int(getattr(settings, 'FRAME_INTERVAL_SECONDS', 5))
    scene_change_enabled   = getattr(settings, 'SCENE_CHANGE_ENABLED',   True)
    scene_change_threshold = getattr(settings, 'SCENE_CHANGE_THRESHOLD', 0.35)
    scene_change_min_gap   = getattr(settings, 'SCENE_CHANGE_MIN_GAP',   0.5)
    cluster_threshold      = getattr(settings, 'FACE_CLUSTER_THRESHOLD', 0.35)
    auto_confirm_threshold = getattr(settings, 'FACE_AUTO_CONFIRM_THRESHOLD', 0.75)
    min_face_size          = getattr(settings, 'MIN_FACE_SIZE', 40)
    max_faces_per_frame    = getattr(settings, 'MAX_FACES_PER_FRAME', 10)

    tmp_dir = tempfile.mkdtemp(prefix='fs_face_')
    try:
        frame_pattern = os.path.join(tmp_dir, 'frame_%05d.jpg')

        if scene_change_enabled:
            vf_filter = (
                f"select='eq(n,0)+gt(scene,{scene_change_threshold})"
                f"+gte(t-prev_selected_t,{interval})',"
                f"showinfo"
            )
        else:
            vf_filter = f"fps=1/{interval},showinfo"

        cmd = [
            settings.FFMPEG_PATH,
            '-i', str(source_path),
            '-vf', vf_filter,
            '-vsync', 'vfr',
            '-q:v', '3',
            '-f', 'image2',
            frame_pattern,
            '-y',
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            logger.error(f'run_insightface: ffmpeg failed for {video_id}: {result.stderr[-300:]}')
            return

        frame_files = sorted(Path(tmp_dir).glob('frame_*.jpg'))
        if not frame_files:
            return

        raw_timestamps = [
            float(m.group(1))
            for m in (re.search(r'pts_time:(\S+)', ln) for ln in result.stderr.splitlines())
            if m
        ]
        if len(raw_timestamps) == len(frame_files):
            frame_timestamps = raw_timestamps
        else:
            frame_timestamps = [float(i * interval) for i in range(len(frame_files))]

        # Dedup
        kept_files, kept_timestamps = [], []
        last_t = -999.0
        for f, t in zip(frame_files, frame_timestamps):
            if t - last_t >= scene_change_min_gap:
                kept_files.append(f)
                kept_timestamps.append(t)
                last_t = t
            else:
                Path(f).unlink(missing_ok=True)
        frame_files, frame_timestamps = kept_files, kept_timestamps

        # Load InsightFace
        try:
            from insightface.app import FaceAnalysis
            face_app = FaceAnalysis(name='buffalo_l', providers=['CPUExecutionProvider'])
            face_app.prepare(ctx_id=0, det_size=(640, 640))
            logger.info(f'run_insightface: InsightFace loaded for {video_id}')
        except Exception as exc:
            logger.error(f'run_insightface: InsightFace unavailable: {exc}')
            return

        # VideoFrame lookup: round(ts) → pk
        frame_lookup = {
            round(ts): pk
            for pk, ts in VideoFrame.objects.filter(video=video).values_list('pk', 'timestamp')
        }

        # Collect raw faces
        raw_faces = []
        for frame_path, timestamp in zip(frame_files, frame_timestamps):
            img = cv2.imread(str(frame_path))
            if img is None:
                continue
            try:
                faces = face_app.get(img)
            except Exception:
                continue

            frame_pk = frame_lookup.get(round(timestamp))

            for face in faces[:max_faces_per_frame]:
                if face.embedding is None:
                    continue
                bbox = face.bbox.tolist() if hasattr(face.bbox, 'tolist') else list(face.bbox)
                w = bbox[2] - bbox[0]
                h = bbox[3] - bbox[1]
                if w < min_face_size or h < min_face_size:
                    continue
                raw_faces.append({
                    'frame_path': str(frame_path),
                    'frame_pk':   frame_pk,
                    'timestamp':  timestamp,
                    'bbox':       bbox,
                    'embedding':  face.embedding.tolist(),
                    'confidence': float(face.det_score) if hasattr(face, 'det_score') else 0.9,
                    'img':        img,
                })

        if not raw_faces:
            logger.info(f'run_insightface: no faces detected in {video_id}')
            # Delete old faces for this video
            DetectedFace.objects.filter(video=video).delete()
            return

        # Cluster faces
        embeddings = [f['embedding'] for f in raw_faces]
        assignments, cluster_means = _cluster_embeddings(embeddings, threshold=cluster_threshold)

        # Delete existing DetectedFace rows for this video
        DetectedFace.objects.filter(video=video).delete()

        # Map cluster index → FaceIdentity
        # Re-use existing identities where cosine similarity is high enough;
        # otherwise create new ones.
        existing_identities = list(
            FaceIdentity.objects.filter(
                detected_faces__video=video
            ).distinct()
        )
        # Fresh run — match cluster means against ALL existing identities
        all_identities = list(FaceIdentity.objects.exclude(ref_embedding='').exclude(ref_embedding=None))

        cluster_to_identity = {}
        for cidx, mean_emb in enumerate(cluster_means):
            best_id, best_sim = None, -1.0
            for ident in all_identities:
                try:
                    ref = np.array(json.loads(ident.ref_embedding), dtype=np.float32)
                    sim = _cosine_sim(mean_emb, ref)
                    if sim > best_sim:
                        best_sim, best_id = sim, ident
                except Exception:
                    continue
            if best_sim >= cluster_threshold and best_id is not None:
                cluster_to_identity[cidx] = best_id
            else:
                new_identity = FaceIdentity.objects.create(
                    name='',
                    ref_embedding=json.dumps(mean_emb.tolist()),
                )
                cluster_to_identity[cidx] = new_identity

        # Save crops + DetectedFace rows
        crops_dir = os.path.join(settings.MEDIA_ROOT, 'face_crops')
        os.makedirs(crops_dir, exist_ok=True)

        face_objects = []
        for face_data, cluster_idx in zip(raw_faces, assignments):
            identity = cluster_to_identity[cluster_idx]
            frame_pk = face_data['frame_pk']
            frame_obj = VideoFrame.objects.filter(pk=frame_pk).first() if frame_pk else None

            bbox = face_data['bbox']
            x1, y1, x2, y2 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
            img = face_data['img']
            h_img, w_img = img.shape[:2]
            pad = 10
            cx1 = max(0, x1 - pad)
            cy1 = max(0, y1 - pad)
            cx2 = min(w_img, x2 + pad)
            cy2 = min(h_img, y2 + pad)
            crop = img[cy1:cy2, cx1:cx2]

            import uuid as _uuid
            crop_filename = f'{_uuid.uuid4().hex}.jpg'
            crop_path = os.path.join(crops_dir, crop_filename)
            cv2.imwrite(crop_path, crop)

            sim_to_ref = _cosine_sim(
                face_data['embedding'],
                json.loads(identity.ref_embedding) if identity.ref_embedding else face_data['embedding']
            )
            auto_status = (
                DetectedFace.STATUS_CONFIRMED
                if sim_to_ref >= auto_confirm_threshold
                else DetectedFace.STATUS_UNREVIEWED
            )

            df = DetectedFace(
                video=video,
                frame=frame_obj,
                identity=identity,
                timestamp=face_data['timestamp'],
                bbox_x=x1, bbox_y=y1, bbox_w=x2 - x1, bbox_h=y2 - y1,
                confidence=face_data['confidence'],
                embedding=json.dumps(face_data['embedding']),
                crop_path=f'face_crops/{crop_filename}',
                status=auto_status,
            )
            face_objects.append(df)

        DetectedFace.objects.bulk_create(face_objects, batch_size=200)

        # Update face_count on VideoFrame rows
        from django.db.models import Count
        for pk, cnt in (
            DetectedFace.objects
            .filter(video=video, frame__isnull=False)
            .values('frame_id')
            .annotate(c=Count('id'))
            .values_list('frame_id', 'c')
        ):
            VideoFrame.objects.filter(pk=pk).update(face_count=cnt)

        logger.info(f'run_insightface: {video_id} → {len(face_objects)} face crops saved')

    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


class Command(BaseCommand):
    help = 'Re-run InsightFace face detection on videos and update DetectedFace rows'

    def add_arguments(self, parser):
        parser.add_argument('--video-id', type=str, default=None, metavar='UUID')
        parser.add_argument('--channel', type=str, default=None, metavar='SLUG')
        parser.add_argument('--dry-run', action='store_true', default=False)
        parser.add_argument('--sync', action='store_true', default=False)

    def handle(self, *args, **options):
        from videos.models import Video

        video_id     = options['video_id']
        channel_slug = options['channel']
        dry_run      = options['dry_run']

        if video_id:
            qs = Video.objects.filter(id=video_id, status=Video.STATUS_READY)
        else:
            qs = Video.objects.filter(status=Video.STATUS_READY).order_by('created_at')
            if channel_slug:
                qs = qs.filter(channel__slug=channel_slug)

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.WARNING('No matching ready videos found.'))
            return

        mode = 'DRY RUN' if dry_run else 'SYNC'
        self.stdout.write(f'\n[{mode}] InsightFace face detection for {total} video(s)…\n')
        self.stdout.write(
            self.style.WARNING(
                '  NOTE: Existing DetectedFace rows for each processed video will be replaced.\n'
            )
        )

        done = 0
        for video in qs:
            title = (video.title or str(video.id))[:60]
            self.stdout.write(f'  {video.id}  {title}')
            if dry_run:
                self.stdout.write(self.style.WARNING('    → skipped (dry-run)\n'))
                continue
            try:
                _run_insightface_for_video(str(video.id))
                self.stdout.write(self.style.SUCCESS('    → done\n'))
                done += 1
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f'    → ERROR: {exc}\n'))

        if dry_run:
            self.stdout.write(self.style.WARNING(f'\nDry run — {total} video(s) would be processed.'))
        else:
            self.stdout.write(self.style.SUCCESS(f'\nDone — {done}/{total} video(s) processed.'))
