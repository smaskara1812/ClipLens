"""
Celery tasks for ClipStream.

Queues:
    processing  — HLS encoding (slow, CPU-bound)
    captions    — Whisper transcription (slow, model inference)
    default     — everything else
"""
import logging
import os
import subprocess
import json
from pathlib import Path

from celery import shared_task
from django.conf import settings

logger = logging.getLogger(__name__)


# ── Video processing ──────────────────────────────────────────────────────────

@shared_task(
    bind=True,
    name='videos.tasks.process_video_task',
    queue='processing',
    max_retries=2,
    default_retry_delay=30,
    acks_late=True,
)
def process_video_task(self, video_id: str):
    """
    Convert uploaded video to HLS (single or multi-quality).
    Replaces the old threading.Thread approach.
    On completion, triggers auto-caption generation if enabled.
    """
    from .services import process_video
    try:
        process_video(video_id)
        # Trigger caption generation if enabled
        if getattr(settings, 'AUTO_CAPTION_ON_UPLOAD', True):
            generate_captions_task.apply_async(
                args=[video_id],
                queue='captions',
                countdown=5,
            )
        # Trigger frame analysis (object detection) if enabled
        if getattr(settings, 'FRAME_ANALYSIS_ENABLED', True):
            analyze_video_frames_task.apply_async(
                args=[video_id],
                queue='processing',
                countdown=10,
            )
    except Exception as exc:
        logger.error(f'process_video_task failed for {video_id}: {exc}')
        raise self.retry(exc=exc)


# ── Auto-caption generation (faster-whisper) ──────────────────────────────────

def _seconds_to_vtt_time(seconds: float) -> str:
    """Convert float seconds → WebVTT timestamp (HH:MM:SS.mmm)."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f'{h:02d}:{m:02d}:{s:06.3f}'


def _segments_to_vtt_and_index(segments, video) -> str:
    """
    Consume the faster-whisper segment iterator once:
      1. Build a WebVTT string
      2. Bulk-create VideoSegment records for in-video search

    Returns the WebVTT string.
    """
    from .models import VideoSegment

    vtt_lines    = ['WEBVTT', '']
    seg_objects  = []

    for i, seg in enumerate(segments, 1):
        text = seg.text.strip()
        if not text:
            continue
        start_ts = _seconds_to_vtt_time(seg.start)
        end_ts   = _seconds_to_vtt_time(seg.end)
        vtt_lines += [f'{i}', f'{start_ts} --> {end_ts}', text, '']
        seg_objects.append(VideoSegment(
            video=video,
            start_seconds=seg.start,
            end_seconds=seg.end,
            text=text,
        ))

    # Delete any old segments for this video then bulk-insert
    logger.info(f'_segments_to_vtt_and_index: {len(seg_objects)} segment(s) for video {video.id}')
    if seg_objects:
        try:
            VideoSegment.objects.filter(video=video).delete()
            VideoSegment.objects.bulk_create(seg_objects, batch_size=500)
            logger.info(f'_segments_to_vtt_and_index: indexed {len(seg_objects)} segments for {video.id}')
        except Exception as exc:
            # Log but don't abort — subtitle is still useful even without the search index
            logger.error(f'_segments_to_vtt_and_index: bulk_create failed for {video.id}: {exc}')

    return '\n'.join(vtt_lines)


def _has_audio_stream(source_path: str) -> bool:
    """Return True if the file has at least one audio stream."""
    cmd = [
        settings.FFPROBE_PATH,
        '-v', 'quiet',
        '-print_format', 'json',
        '-show_streams',
        '-select_streams', 'a',
        str(source_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout)
        return len(data.get('streams', [])) > 0
    except Exception:
        return False


def _extract_audio_wav(source_path: str, out_path: str) -> bool:
    """
    Use FFmpeg to extract audio as a 16kHz mono WAV — the format
    Whisper expects. Returns True on success.
    """
    cmd = [
        settings.FFMPEG_PATH,
        '-i', str(source_path),
        '-vn',                  # no video
        '-acodec', 'pcm_s16le', # 16-bit PCM
        '-ar', '16000',         # 16 kHz — Whisper's native sample rate
        '-ac', '1',             # mono
        '-y',
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    return result.returncode == 0


@shared_task(
    bind=True,
    name='videos.tasks.generate_captions_task',
    queue='captions',
    max_retries=1,
    default_retry_delay=60,
    acks_late=True,
)
def generate_captions_task(self, video_id: str, language: str = 'en'):
    """
    Use faster-whisper to transcribe the video and create a Subtitle record.

    Pipeline:
        1. Check video has an audio stream (skip silently if not)
        2. Extract audio to a temp 16kHz mono WAV via FFmpeg
        3. Run Whisper on the WAV (avoids PyAV / av IndexError on video files)
        4. Save WebVTT as a Subtitle record
        5. Clean up the temp WAV
    """
    import tempfile
    from .models import Video, Subtitle
    from django.core.files.base import ContentFile

    try:
        video = Video.objects.get(id=video_id)
    except Video.DoesNotExist:
        logger.warning(f'generate_captions_task: video {video_id} not found')
        return

    if video.status != Video.STATUS_READY:
        logger.info(f'generate_captions_task: video {video_id} not ready, skipping')
        return

    # Skip if auto-caption already exists for this language
    if Subtitle.objects.filter(video=video, language=language, is_auto_generated=True).exists():
        logger.info(f'generate_captions_task: captions already exist for {video_id}/{language}')
        return

    source_path = Path(settings.MEDIA_ROOT) / video.original_file.name
    if not source_path.exists():
        logger.warning(f'generate_captions_task: source file missing for {video_id}')
        return

    # ── Step 1: check for audio stream ───────────────────────────────────────
    if not _has_audio_stream(source_path):
        logger.info(f'generate_captions_task: no audio stream in {video_id}, skipping')
        return  # Not an error — video simply has no speech to transcribe

    logger.info(f'generate_captions_task: starting transcription for {video_id}')

    tmp_wav = None
    try:
        # ── Step 2: extract audio to temp WAV ────────────────────────────────
        tmp_wav = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        tmp_wav.close()

        ok = _extract_audio_wav(source_path, tmp_wav.name)
        if not ok:
            logger.error(f'generate_captions_task: FFmpeg audio extraction failed for {video_id}')
            return

        logger.info(f'generate_captions_task: audio extracted to {tmp_wav.name}')

        # ── Step 3: run Whisper on the WAV ───────────────────────────────────
        from faster_whisper import WhisperModel

        model_size   = getattr(settings, 'WHISPER_MODEL_SIZE',   'base')
        device       = getattr(settings, 'WHISPER_DEVICE',       'cpu')
        compute_type = getattr(settings, 'WHISPER_COMPUTE_TYPE', 'int8')

        model = WhisperModel(model_size, device=device, compute_type=compute_type)
        segments, info = model.transcribe(
            tmp_wav.name,                               # ← WAV, not the original video
            language=language if language != 'auto' else None,
            beam_size=5,
            vad_filter=True,
            vad_parameters={'min_silence_duration_ms': 500},
        )

        detected_lang = info.language if language == 'auto' else language
        vtt_content   = _segments_to_vtt_and_index(segments, video)

        if not vtt_content.strip().replace('WEBVTT', '').strip():
            logger.info(f'generate_captions_task: empty transcription (silent video?) for {video_id}')
            return

        # ── Step 4: save WebVTT ───────────────────────────────────────────────
        subtitle = Subtitle(
            video=video,
            language=detected_lang,
            language_label=_lang_label(detected_lang),
            format=Subtitle.FORMAT_VTT,
            is_auto_generated=True,
        )
        subtitle.file.save(
            f'{video_id}_{detected_lang}_auto.vtt',
            ContentFile(vtt_content.encode('utf-8')),
            save=False,
        )
        subtitle.save()
        logger.info(f'generate_captions_task: saved captions for {video_id} [{detected_lang}]')

    except Exception as exc:
        logger.error(f'generate_captions_task failed for {video_id}: {exc}')
        raise self.retry(exc=exc)
    finally:
        # ── Step 5: clean up temp WAV ─────────────────────────────────────────
        if tmp_wav and os.path.exists(tmp_wav.name):
            os.unlink(tmp_wav.name)
        # Always restore ready status regardless of outcome
        Video.objects.filter(id=video_id).update(status=Video.STATUS_READY)


# ── Video frame analysis (YOLOv8 object detection) ───────────────────────────

def _cosine_sim(a, b):
    """Cosine similarity between two numpy arrays."""
    import numpy as np
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _cluster_embeddings(embeddings, threshold=0.35):
    """
    Greedy clustering of face embeddings by cosine similarity.
    Returns list of cluster indices (same length as embeddings).
    Each unique index = one identity.
    """
    import numpy as np

    cluster_means = []   # running mean embedding per cluster
    cluster_counts = []
    assignments = []

    for emb in embeddings:
        emb = np.array(emb, dtype=np.float32)
        best_idx, best_sim = -1, -1.0

        for i, mean in enumerate(cluster_means):
            sim = _cosine_sim(emb, mean)
            if sim > best_sim:
                best_sim, best_idx = sim, i

        if best_sim >= threshold:
            # Update running mean
            n = cluster_counts[best_idx]
            cluster_means[best_idx] = (cluster_means[best_idx] * n + emb) / (n + 1)
            cluster_counts[best_idx] += 1
            assignments.append(best_idx)
        else:
            cluster_means.append(emb.copy())
            cluster_counts.append(1)
            assignments.append(len(cluster_means) - 1)

    return assignments, cluster_means


@shared_task(
    bind=True,
    name='videos.tasks.analyze_video_frames_task',
    queue='processing',
    max_retries=1,
    default_retry_delay=30,
    acks_late=True,
)
def analyze_video_frames_task(self, video_id: str):
    """
    Phase A — Object Detection (YOLOv8):
      Extract frames, detect objects, build visual search index.

    Phase B — Face Recognition (InsightFace):
      Detect faces, compute ArcFace embeddings, cluster into identities,
      save DetectedFace + FaceIdentity rows, crop face thumbnails.
    """
    import shutil
    import tempfile
    import json
    import numpy as np
    import cv2
    from .models import Video, VideoFrame, DetectedFace, FaceIdentity

    if not getattr(settings, 'FRAME_ANALYSIS_ENABLED', True):
        logger.info(f'analyze_video_frames_task: disabled, skipping {video_id}')
        return

    try:
        video = Video.objects.get(id=video_id)
    except Video.DoesNotExist:
        logger.warning(f'analyze_video_frames_task: video {video_id} not found')
        return

    if video.status != Video.STATUS_READY:
        logger.info(f'analyze_video_frames_task: video {video_id} not ready, skipping')
        return

    source_path = Path(settings.MEDIA_ROOT) / video.original_file.name
    if not source_path.exists():
        logger.warning(f'analyze_video_frames_task: source file missing for {video_id}')
        return

    interval        = int(getattr(settings, 'FRAME_INTERVAL_SECONDS', 5))
    yolo_model_name = getattr(settings, 'YOLO_MODEL', 'yolov8n')
    face_enabled    = getattr(settings, 'FACE_RECOGNITION_ENABLED', True)

    tmp_dir = None
    try:
        # ── Step 1: extract frames ────────────────────────────────────────────
        tmp_dir = tempfile.mkdtemp(prefix='fs_frames_')
        frame_pattern = os.path.join(tmp_dir, 'frame_%05d.jpg')

        cmd = [
            settings.FFMPEG_PATH,
            '-i', str(source_path),
            '-vf', f'fps=1/{interval}',
            '-q:v', '3',
            '-f', 'image2',
            frame_pattern,
            '-y',
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            logger.error(
                f'analyze_video_frames_task: ffmpeg failed for {video_id}: '
                f'{result.stderr[-400:]}'
            )
            return

        frame_files = sorted(Path(tmp_dir).glob('frame_*.jpg'))
        if not frame_files:
            logger.info(f'analyze_video_frames_task: no frames extracted for {video_id}')
            return

        logger.info(f'analyze_video_frames_task: {len(frame_files)} frames for {video_id}')

        # ── Step 2: load models ───────────────────────────────────────────────
        from ultralytics import YOLO
        yolo = YOLO(f'{yolo_model_name}.pt')

        # Scene captioning — model selected via SCENE_CAPTION_MODEL setting
        scene_enabled  = getattr(settings, 'SCENE_DESCRIPTION_ENABLED', True)
        caption_model  = getattr(settings, 'SCENE_CAPTION_MODEL', 'blip').lower()
        blip_model = blip_processor = None
        florence_model = florence_processor = None
        if scene_enabled:
            if caption_model == 'florence2':
                try:
                    import sys, types as _types, importlib.util as _ilu
                    if 'flash_attn' not in sys.modules:
                        _stub = _types.ModuleType('flash_attn')
                        _stub.__spec__ = _ilu.spec_from_loader('flash_attn', loader=None)
                        _stub.__version__ = '0.0.0'
                        sys.modules['flash_attn'] = _stub
                    from transformers import AutoProcessor, AutoModelForCausalLM
                    florence_processor = AutoProcessor.from_pretrained(
                        'microsoft/Florence-2-base', trust_remote_code=True
                    )
                    florence_model = AutoModelForCausalLM.from_pretrained(
                        'microsoft/Florence-2-base', trust_remote_code=True
                    )
                    florence_model.eval()
                    logger.info(f'analyze_video_frames_task: Florence-2 loaded for {video_id}')
                except Exception as exc:
                    logger.warning(
                        f'analyze_video_frames_task: Florence-2 unavailable ({exc}), '
                        f'skipping scene descriptions for {video_id}'
                    )
                    florence_model = florence_processor = None
            else:
                try:
                    from transformers import BlipProcessor, BlipForConditionalGeneration
                    import torch as _torch
                    blip_processor = BlipProcessor.from_pretrained('Salesforce/blip-image-captioning-base')
                    blip_model = BlipForConditionalGeneration.from_pretrained(
                        'Salesforce/blip-image-captioning-base', torch_dtype=_torch.float32,
                    )
                    blip_model.eval()
                    logger.info(f'analyze_video_frames_task: BLIP loaded for {video_id}')
                except Exception as exc:
                    logger.warning(
                        f'analyze_video_frames_task: BLIP unavailable ({exc}), '
                        f'skipping scene descriptions for {video_id}'
                    )
                    blip_model = blip_processor = None

        face_app = None
        if face_enabled:
            try:
                from insightface.app import FaceAnalysis
                face_app = FaceAnalysis(
                    name='buffalo_l',
                    providers=['CPUExecutionProvider'],
                )
                face_app.prepare(ctx_id=0, det_size=(640, 640))
                logger.info(f'analyze_video_frames_task: InsightFace loaded for {video_id}')
            except Exception as exc:
                logger.warning(
                    f'analyze_video_frames_task: InsightFace unavailable ({exc}), '
                    f'skipping face recognition for {video_id}'
                )
                face_app = None

        # CLIP visual embedding
        clip_enabled = getattr(settings, 'CLIP_ENABLED', True)
        clip_model = clip_processor = None
        if clip_enabled:
            try:
                from transformers import CLIPProcessor, CLIPModel
                clip_processor = CLIPProcessor.from_pretrained('openai/clip-vit-base-patch32')
                clip_model = CLIPModel.from_pretrained('openai/clip-vit-base-patch32')
                clip_model.eval()
                logger.info(f'analyze_video_frames_task: CLIP loaded for {video_id}')
            except Exception as exc:
                logger.warning(
                    f'analyze_video_frames_task: CLIP unavailable ({exc}), '
                    f'skipping CLIP embeddings for {video_id}'
                )
                clip_model = clip_processor = None

        # ── Step 3: per-frame inference ───────────────────────────────────────
        frame_objects   = []   # VideoFrame rows
        raw_faces       = []   # {frame_idx, timestamp, bbox, embedding, confidence, img_path}

        for idx, frame_path in enumerate(frame_files):
            timestamp = float(idx * interval)

            # --- YOLO ---
            yolo_results = yolo.predict(
                source=str(frame_path), verbose=False, conf=0.40, iou=0.45
            )
            labels_set = set()
            for r in yolo_results:
                for cls_id in r.boxes.cls.tolist():
                    labels_set.add(yolo.names[int(cls_id)])

            # --- Scene caption (BLIP or Florence-2) ---
            description = ''
            try:
                from PIL import Image as _PILImage
                import torch as _torch
                pil_img = _PILImage.open(str(frame_path)).convert('RGB')
                if blip_model is not None and blip_processor is not None:
                    inputs = blip_processor(pil_img, return_tensors='pt')
                    with _torch.no_grad():
                        out = blip_model.generate(**inputs, max_new_tokens=50)
                    description = blip_processor.decode(out[0], skip_special_tokens=True)
                elif florence_model is not None and florence_processor is not None:
                    inputs = florence_processor(
                        text='<DETAILED_CAPTION>', images=pil_img, return_tensors='pt'
                    )
                    with _torch.no_grad():
                        out = florence_model.generate(
                            input_ids=inputs['input_ids'],
                            pixel_values=inputs['pixel_values'],
                            max_new_tokens=60,
                            num_beams=2,
                        )
                    raw = florence_processor.batch_decode(out, skip_special_tokens=False)[0]
                    parsed = florence_processor.post_process_generation(
                        raw, task='<DETAILED_CAPTION>', image_size=(pil_img.width, pil_img.height)
                    )
                    description = parsed.get('<DETAILED_CAPTION>', '')
            except Exception as exc:
                logger.warning(f'analyze_video_frames_task: caption error frame {idx}: {exc}')

            # --- CLIP image embedding ---
            clip_embedding = None
            if clip_model is not None and clip_processor is not None:
                try:
                    from PIL import Image as _PILImage
                    import torch as _torch
                    _pil = _PILImage.open(str(frame_path)).convert('RGB')
                    _inputs = clip_processor(images=_pil, return_tensors='pt')
                    with _torch.no_grad():
                        _feats = clip_model.get_image_features(**_inputs)
                        _feats = _feats / _feats.norm(dim=-1, keepdim=True)
                    clip_embedding = _feats[0].tolist()  # pgvector accepts Python list directly
                except Exception as exc:
                    logger.warning(f'analyze_video_frames_task: CLIP error frame {idx}: {exc}')

            frame_objects.append(VideoFrame(
                video          = video,
                timestamp      = timestamp,
                labels         = ', '.join(sorted(labels_set)),
                face_count     = 0,   # updated below after face detection
                description    = description,
                clip_embedding = clip_embedding,
            ))

            # --- InsightFace ---
            if face_app is not None:
                try:
                    img = cv2.imread(str(frame_path))
                    if img is None:
                        continue
                    faces = face_app.get(img)
                    for face in faces:
                        if face.det_score < 0.50:
                            continue
                        emb = face.embedding.tolist() if face.embedding is not None else None
                        if emb is None:
                            continue
                        # Capture pose (yaw/pitch/roll) for frontal-face scoring
                        try:
                            pose = face.pose.tolist() if face.pose is not None else [0.0, 0.0, 0.0]
                        except Exception:
                            pose = [0.0, 0.0, 0.0]
                        raw_faces.append({
                            'frame_idx':  idx,
                            'timestamp':  timestamp,
                            'bbox':       face.bbox.tolist(),
                            'embedding':  emb,
                            'confidence': float(face.det_score),
                            'pose':       pose,
                            'img_path':   str(frame_path),
                        })
                except Exception as exc:
                    logger.debug(
                        f'analyze_video_frames_task: face detection error '
                        f'frame {idx} video {video_id}: {exc}'
                    )

        # ── Step 4: update face_count on VideoFrame objects ───────────────────
        face_counts_per_frame = {}
        for rf in raw_faces:
            face_counts_per_frame[rf['frame_idx']] = (
                face_counts_per_frame.get(rf['frame_idx'], 0) + 1
            )
        for i, vf in enumerate(frame_objects):
            vf.face_count = face_counts_per_frame.get(i, 0)

        # ── Step 5: persist VideoFrame rows ──────────────────────────────────
        VideoFrame.objects.filter(video=video).delete()
        # Save all extracted frames — even blank ones are needed for player
        # navigation and future re-analysis. The old filter that dropped frames
        # with no YOLO/BLIP/CLIP results caused scenery/abstract videos to end
        # up with zero frames in the DB.
        VideoFrame.objects.bulk_create(
            frame_objects,
            batch_size=200,
        )
        # Refetch from DB so PKs are guaranteed to be populated
        saved_frames = list(VideoFrame.objects.filter(video=video).order_by('timestamp'))
        logger.info(
            f'analyze_video_frames_task: saved {len(saved_frames)} VideoFrame rows '
            f'for {video_id}'
        )

        # Build frame_idx → saved VideoFrame map (keyed by frame index)
        frame_map = {}
        for vf in saved_frames:
            key = round(vf.timestamp / interval)
            frame_map[key] = vf

        if not raw_faces or face_app is None:
            return  # No faces detected or face recognition disabled

        # ── Step 6: cluster faces into identities ─────────────────────────────
        logger.info(
            f'analyze_video_frames_task: clustering {len(raw_faces)} faces for {video_id}'
        )
        embeddings = [rf['embedding'] for rf in raw_faces]
        assignments, cluster_means = _cluster_embeddings(embeddings, threshold=0.35)

        n_identities = len(cluster_means)
        logger.info(
            f'analyze_video_frames_task: {n_identities} unique face(s) found in {video_id}'
        )

        # ── Step 7: match clusters against known (tagged) identities ─────────
        # Delete old detected faces for this video so re-analysis is clean.
        DetectedFace.objects.filter(video=video).delete()

        # After deleting, remove any auto-named identities that now have no
        # faces left anywhere — these are ghost/orphan rows from previous runs.
        FaceIdentity.objects.filter(is_auto_named=True, faces__isnull=True).delete()

        # Load ALL existing identities (named first, then auto-named) so that
        # an unknown person seen in a previous video reuses their existing identity
        # rather than getting a brand-new "Person #N" on every upload.
        NAMED_THRESHOLD = 0.45    # stricter: user-confirmed names
        AUTO_THRESHOLD  = 0.50    # stricter still: auto identities (avoid false merges)

        named_identities = list(
            FaceIdentity.objects.filter(is_auto_named=False).exclude(ref_embedding='')
        )
        auto_identities = list(
            FaceIdentity.objects.filter(is_auto_named=True).exclude(ref_embedding='')
        )

        def _load_embeddings(id_list):
            out = []
            for ki in id_list:
                try:
                    out.append(np.array(json.loads(ki.ref_embedding), dtype=np.float32))
                except Exception:
                    out.append(None)
            return out

        named_embeddings = _load_embeddings(named_identities)
        auto_embeddings  = _load_embeddings(auto_identities)

        def _best_match(cluster_mean, id_list, emb_list, threshold):
            best_id, best_sim = None, -1.0
            for ki, ke in zip(id_list, emb_list):
                if ke is None:
                    continue
                sim = _cosine_sim(cluster_mean, ke)
                if sim > best_sim:
                    best_sim, best_id = sim, ki
            if best_sim >= threshold and best_id is not None:
                return best_id, best_sim
            return None, best_sim

        identities = []
        for i, cluster_mean in enumerate(cluster_means):
            # Priority 1: match against user-named identities
            matched, sim = _best_match(cluster_mean, named_identities, named_embeddings, NAMED_THRESHOLD)

            # Priority 2: match against existing auto-named identities
            if matched is None:
                matched, sim = _best_match(cluster_mean, auto_identities, auto_embeddings, AUTO_THRESHOLD)

            if matched is not None:
                # Reuse existing identity and update its reference embedding.
                logger.info(
                    f'analyze_video_frames_task: cluster {i} matched identity '
                    f'"{matched.name}" (sim={sim:.3f}) for {video_id}'
                )
                try:
                    old_emb = np.array(json.loads(matched.ref_embedding), dtype=np.float32)
                    new_emb = (old_emb + cluster_mean) / 2.0
                    matched.ref_embedding = json.dumps(new_emb.tolist())
                    matched.save(update_fields=['ref_embedding'])
                except Exception:
                    pass
                identities.append(matched)
            else:
                # Truly unknown — create a new auto-named identity.
                fi = FaceIdentity.objects.create(
                    name='__tmp__',
                    is_auto_named=True,
                    ref_embedding=json.dumps(cluster_mean.tolist()),
                )
                fi.name = f'Person #{fi.pk}'
                fi.save(update_fields=['name'])
                identities.append(fi)
                logger.info(
                    f'analyze_video_frames_task: cluster {i} → new identity '
                    f'"{fi.name}" (best_sim={sim:.3f}) for {video_id}'
                )

        # ── Step 8: save face crops + DetectedFace rows ───────────────────────
        faces_dir = Path(settings.MEDIA_ROOT) / 'faces' / str(video_id)
        faces_dir.mkdir(parents=True, exist_ok=True)

        # ── Step 8 config ─────────────────────────────────────────────────────
        # Max crop *images* saved per identity per video.
        # All DetectedFace rows are still created (for appearance tracking),
        # but only the N most frontal-facing faces get an image written to disk.
        # Override in settings as FACE_MAX_CROPS_PER_VIDEO (default 6).
        MAX_CROPS = getattr(settings, 'FACE_MAX_CROPS_PER_VIDEO', 6)

        # Auto-confirm threshold: crops very close to cluster centroid get
        # STATUS_CONFIRMED automatically. Override as FACE_AUTO_CONFIRM_THRESHOLD.
        AUTO_CONFIRM_SIM = getattr(settings, 'FACE_AUTO_CONFIRM_THRESHOLD', 0.75)

        # Pre-pass: compute frontal score for every face, then pick top-N
        # per cluster to actually save to disk.
        face_frontal_scores = []
        for rf in raw_faces:
            yaw = rf['pose'][0] if rf.get('pose') else 0.0
            score = rf['confidence'] * (1.0 - min(abs(yaw), 90.0) / 90.0)
            face_frontal_scores.append(score)

        # For each cluster: indices of faces sorted by score descending, keep top N
        cluster_to_face_indices = {}
        for face_idx, cluster_id in enumerate(assignments):
            cluster_to_face_indices.setdefault(cluster_id, []).append(face_idx)

        faces_with_crops = set()
        identity_best_score = {}   # cluster_id → float (for thumbnail selection)
        identity_best_crop_idx = {}  # cluster_id → face_idx with best frontal score
        for cluster_id, idxs in cluster_to_face_indices.items():
            sorted_by_score = sorted(idxs, key=lambda i: face_frontal_scores[i], reverse=True)
            top_n = sorted_by_score[:MAX_CROPS]
            faces_with_crops.update(top_n)
            # Best frontal face in this cluster (for thumbnail)
            best_idx = sorted_by_score[0]
            identity_best_score[cluster_id] = face_frontal_scores[best_idx]
            identity_best_crop_idx[cluster_id] = best_idx

        detected_face_objects = []
        saved_crop_paths = {}  # face_idx → crop_rel

        for face_idx, (rf, cluster_id) in enumerate(zip(raw_faces, assignments)):
            identity = identities[cluster_id]
            frame_obj = frame_map.get(rf['frame_idx'])

            # Only write a crop image for the top-N frontal faces per identity
            crop_rel = ''
            if face_idx in faces_with_crops:
                try:
                    img = cv2.imread(rf['img_path'])
                    if img is not None:
                        x1, y1, x2, y2 = [int(v) for v in rf['bbox']]
                        pad = 15
                        x1 = max(0, x1 - pad); y1 = max(0, y1 - pad)
                        x2 = min(img.shape[1], x2 + pad); y2 = min(img.shape[0], y2 + pad)
                        crop = img[y1:y2, x1:x2]
                        if crop.size > 0:
                            crop_name = f'face_{rf["frame_idx"]:05d}_{face_idx:03d}.jpg'
                            cv2.imwrite(str(faces_dir / crop_name), crop)
                            crop_rel = f'faces/{video_id}/{crop_name}'
                            saved_crop_paths[face_idx] = crop_rel
                except Exception as exc:
                    logger.debug(f'analyze_video_frames_task: crop error face {face_idx}: {exc}')

            # Auto-confirm if embedding is very close to cluster centroid
            face_emb = np.array(rf['embedding'], dtype=np.float32)
            sim_to_centroid = _cosine_sim(face_emb, cluster_means[cluster_id])
            initial_status = (
                DetectedFace.STATUS_CONFIRMED
                if sim_to_centroid >= AUTO_CONFIRM_SIM
                else DetectedFace.STATUS_UNREVIEWED
            )

            detected_face_objects.append(DetectedFace(
                video      = video,
                frame      = frame_obj,
                identity   = identity,
                timestamp  = rf['timestamp'],
                bbox       = json.dumps(rf['bbox']),
                embedding  = json.dumps(rf['embedding']),
                confidence = rf['confidence'],
                crop_path  = crop_rel,
                status     = initial_status,
            ))

        # Apply best-frontal thumbnail to each identity, then persist
        for cluster_id, best_face_idx in identity_best_crop_idx.items():
            best_crop = saved_crop_paths.get(best_face_idx, '')
            if best_crop:
                identities[cluster_id].thumbnail = best_crop
        for identity in identities:
            if getattr(identity, 'thumbnail', None):
                FaceIdentity.objects.filter(pk=identity.pk).update(thumbnail=identity.thumbnail)

        DetectedFace.objects.bulk_create(detected_face_objects, batch_size=200)

        # Update face_names on VideoFrame rows
        for vf in saved_frames:
            fi = vf.timestamp / interval
            names_at_frame = set()
            for rf, cluster_id in zip(raw_faces, assignments):
                if rf['frame_idx'] == round(fi):
                    names_at_frame.add(identities[cluster_id].name)
            if names_at_frame:
                VideoFrame.objects.filter(pk=vf.pk).update(
                    face_names=', '.join(sorted(names_at_frame))
                )

        logger.info(
            f'analyze_video_frames_task: Phase B complete — '
            f'{len(detected_face_objects)} DetectedFace rows, '
            f'{n_identities} identities for {video_id}'
        )

    except Exception as exc:
        logger.error(f'analyze_video_frames_task failed for {video_id}: {exc}')
        raise self.retry(exc=exc)
    finally:
        if tmp_dir and os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _parse_vtt_segments(vtt_text: str) -> list[dict]:
    """
    Parse a WebVTT string into a list of dicts:
        [{'start': <float seconds>, 'end': <float seconds>, 'text': <str>}, ...]

    Used to rebuild VideoSegment rows from an already-saved subtitle file.
    """
    import re
    TIME_RE = re.compile(
        r'(\d+):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[.,](\d{3})'
    )

    def _ts(h, m, s, ms):
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    results = []
    lines   = vtt_text.splitlines()
    i = 0
    while i < len(lines):
        m = TIME_RE.match(lines[i].strip())
        if m:
            start = _ts(m.group(1), m.group(2), m.group(3), m.group(4))
            end   = _ts(m.group(5), m.group(6), m.group(7), m.group(8))
            i += 1
            text_parts = []
            while i < len(lines) and lines[i].strip():
                text_parts.append(lines[i].strip())
                i += 1
            text = ' '.join(text_parts).strip()
            if text:
                results.append({'start': start, 'end': end, 'text': text})
        else:
            i += 1
    return results


@shared_task(
    name='videos.tasks.reindex_segments_task',
    queue='default',
    max_retries=1,
    acks_late=True,
)
def reindex_segments_task(subtitle_id: int):
    """
    Rebuild VideoSegment rows by parsing a saved Subtitle's VTT file.
    Called after subtitle upload or caption regeneration so in-video speech
    search works even when the Whisper task itself couldn't index segments.
    """
    from .models import Subtitle, VideoSegment

    try:
        subtitle = Subtitle.objects.select_related('video').get(id=subtitle_id)
    except Subtitle.DoesNotExist:
        logger.warning(f'reindex_segments_task: subtitle {subtitle_id} not found')
        return

    if not subtitle.file:
        logger.warning(f'reindex_segments_task: subtitle {subtitle_id} has no file')
        return

    try:
        with subtitle.file.open('r') as f:
            vtt_text = f.read()
    except Exception as exc:
        logger.error(f'reindex_segments_task: could not read subtitle file {subtitle_id}: {exc}')
        return

    segments = _parse_vtt_segments(vtt_text)
    if not segments:
        logger.info(f'reindex_segments_task: no parseable segments in subtitle {subtitle_id}')
        return

    video      = subtitle.video
    seg_objects = [
        VideoSegment(
            video=video,
            start_seconds=seg['start'],
            end_seconds=seg['end'],
            text=seg['text'],
        )
        for seg in segments
    ]

    try:
        VideoSegment.objects.filter(video=video).delete()
        VideoSegment.objects.bulk_create(seg_objects, batch_size=500)
        logger.info(
            f'reindex_segments_task: indexed {len(seg_objects)} segments '
            f'for video {video.id} from subtitle {subtitle_id}'
        )
    except Exception as exc:
        logger.error(f'reindex_segments_task: bulk_create failed for video {video.id}: {exc}')
        raise


def _lang_label(code: str) -> str:
    """Map BCP-47 language code to a human-readable label."""
    _MAP = {
        'en': 'English', 'es': 'Spanish', 'fr': 'French', 'de': 'German',
        'it': 'Italian', 'pt': 'Portuguese', 'ru': 'Russian', 'ja': 'Japanese',
        'ko': 'Korean', 'zh': 'Chinese', 'ar': 'Arabic', 'hi': 'Hindi',
        'nl': 'Dutch', 'pl': 'Polish', 'tr': 'Turkish', 'sv': 'Swedish',
        'da': 'Danish', 'fi': 'Finnish', 'nb': 'Norwegian', 'uk': 'Ukrainian',
    }
    return _MAP.get(code, code.upper())


# ── Audio track extraction ─────────────────────────────────────────────────────

@shared_task(
    bind=True,
    name='videos.tasks.extract_audio_tracks_task',
    queue='processing',
    max_retries=1,
    acks_late=True,
)
def extract_audio_tracks_task(self, video_id: str):
    """
    Detect all audio streams in the source file and produce an HLS audio-only
    playlist for each one so the player can offer audio track switching.
    """
    from .models import Video, AudioTrack

    try:
        video = Video.objects.get(id=video_id)
    except Video.DoesNotExist:
        return

    source_path = Path(settings.MEDIA_ROOT) / video.original_file.name
    if not source_path.exists():
        logger.warning(f'extract_audio_tracks_task: source missing for {video_id}')
        return

    # 1. Probe audio streams
    probe_cmd = [
        settings.FFPROBE_PATH,
        '-v', 'quiet',
        '-print_format', 'json',
        '-show_streams',
        '-select_streams', 'a',
        str(source_path),
    ]
    try:
        result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)
        data   = json.loads(result.stdout)
        streams = data.get('streams', [])
    except Exception as exc:
        logger.error(f'extract_audio_tracks_task: probe failed for {video_id}: {exc}')
        return

    if len(streams) < 2:
        # Only one audio track — nothing to extract separately
        logger.info(f'extract_audio_tracks_task: only {len(streams)} audio stream(s) for {video_id}, skipping')
        return

    out_base = Path(settings.MEDIA_ROOT) / 'hls' / str(video_id) / 'audio'
    out_base.mkdir(parents=True, exist_ok=True)

    AudioTrack.objects.filter(video=video).delete()   # reset

    for idx, stream in enumerate(streams):
        tags     = stream.get('tags', {})
        label    = tags.get('title') or tags.get('handler_name') or f'Track {idx + 1}'
        language = tags.get('language', 'und')
        if language == 'und':
            language = 'en' if idx == 0 else f'track{idx}'

        out_dir = out_base / f'track{idx}'
        out_dir.mkdir(parents=True, exist_ok=True)
        playlist    = out_dir / 'playlist.m3u8'
        segment_pat = str(out_dir / 'segment_%04d.ts')

        cmd = [
            settings.FFMPEG_PATH,
            '-i', str(source_path),
            '-map', f'0:a:{idx}',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-ar', '44100',
            '-vn',
            '-start_number', '0',
            '-hls_time', str(getattr(settings, 'HLS_SEGMENT_DURATION', 6)),
            '-hls_list_size', '0',
            '-hls_segment_filename', segment_pat,
            '-hls_flags', 'independent_segments',
            '-f', 'hls',
            str(playlist),
            '-y',
        ]

        logger.info(f'extract_audio_tracks_task: extracting audio track {idx} for {video_id}')
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)

        if res.returncode != 0:
            logger.error(f'extract_audio_tracks_task: ffmpeg failed for track {idx}: {res.stderr[-500:]}')
            continue

        rel_path = f'hls/{video_id}/audio/track{idx}/playlist.m3u8'
        AudioTrack.objects.create(
            video=video,
            label=label,
            language=language,
            track_index=idx,
            hls_path=rel_path,
            is_default=(idx == 0),
        )
        logger.info(f'extract_audio_tracks_task: saved AudioTrack "{label}" for {video_id}')


# ── Photo analysis task ───────────────────────────────────────────────────────

@shared_task(
    bind=True,
    name='videos.tasks.analyze_photo_task',
    queue='processing',
    max_retries=1,
    default_retry_delay=30,
    acks_late=True,
)
def analyze_photo_task(self, photo_id: str):
    """
    Run AI analysis on a single uploaded photo:
      - YOLOv8  → object labels
      - BLIP / Florence-2 → scene description
      - CLIP    → 512-dim embedding (stored as pgvector)
      - InsightFace → face count + identity names

    No HLS or Whisper — photos need none of that.
    """
    import json
    import numpy as np
    import cv2
    from PIL import Image as _PILImage
    from .models import Photo, FaceIdentity, DetectedFace

    if not getattr(settings, 'FRAME_ANALYSIS_ENABLED', True):
        logger.info(f'analyze_photo_task: disabled, skipping {photo_id}')
        return

    try:
        photo = Photo.objects.get(id=photo_id)
    except Photo.DoesNotExist:
        logger.warning(f'analyze_photo_task: photo {photo_id} not found')
        return

    photo.status = Photo.STATUS_PROCESSING
    photo.save(update_fields=['status'])

    img_path = Path(settings.MEDIA_ROOT) / photo.file.name
    if not img_path.exists():
        photo.status = Photo.STATUS_FAILED
        photo.processing_error = 'Source file missing'
        photo.save(update_fields=['status', 'processing_error'])
        logger.warning(f'analyze_photo_task: file missing for {photo_id}')
        return

    yolo_model_name = getattr(settings, 'YOLO_MODEL', 'yolov8n')
    scene_enabled   = getattr(settings, 'SCENE_DESCRIPTION_ENABLED', True)
    caption_model   = getattr(settings, 'SCENE_CAPTION_MODEL', 'blip').lower()
    clip_enabled    = getattr(settings, 'CLIP_ENABLED', True)
    face_enabled    = getattr(settings, 'FACE_RECOGNITION_ENABLED', True)

    try:
        # ── Load models ───────────────────────────────────────────────────────
        from ultralytics import YOLO
        yolo = YOLO(f'{yolo_model_name}.pt')

        blip_model = blip_processor = None
        florence_model = florence_processor = None
        if scene_enabled:
            if caption_model == 'florence2':
                try:
                    import sys, types as _types, importlib.util as _ilu
                    if 'flash_attn' not in sys.modules:
                        _stub = _types.ModuleType('flash_attn')
                        _stub.__spec__ = _ilu.spec_from_loader('flash_attn', loader=None)
                        _stub.__version__ = '0.0.0'
                        sys.modules['flash_attn'] = _stub
                    from transformers import AutoProcessor, AutoModelForCausalLM
                    florence_processor = AutoProcessor.from_pretrained(
                        'microsoft/Florence-2-base', trust_remote_code=True
                    )
                    florence_model = AutoModelForCausalLM.from_pretrained(
                        'microsoft/Florence-2-base', trust_remote_code=True
                    )
                    florence_model.eval()
                except Exception as exc:
                    logger.warning(f'analyze_photo_task: Florence-2 unavailable ({exc})')
            else:
                try:
                    from transformers import BlipProcessor, BlipForConditionalGeneration
                    import torch as _torch
                    blip_processor = BlipProcessor.from_pretrained('Salesforce/blip-image-captioning-base')
                    blip_model = BlipForConditionalGeneration.from_pretrained(
                        'Salesforce/blip-image-captioning-base', torch_dtype=_torch.float32,
                    )
                    blip_model.eval()
                except Exception as exc:
                    logger.warning(f'analyze_photo_task: BLIP unavailable ({exc})')

        clip_model = clip_processor = None
        if clip_enabled:
            try:
                from transformers import CLIPProcessor, CLIPModel
                clip_processor = CLIPProcessor.from_pretrained('openai/clip-vit-base-patch32')
                clip_model = CLIPModel.from_pretrained('openai/clip-vit-base-patch32')
                clip_model.eval()
            except Exception as exc:
                logger.warning(f'analyze_photo_task: CLIP unavailable ({exc})')

        face_app = None
        if face_enabled:
            try:
                from insightface.app import FaceAnalysis
                face_app = FaceAnalysis(name='buffalo_l', providers=['CPUExecutionProvider'])
                face_app.prepare(ctx_id=0, det_size=(640, 640))
            except Exception as exc:
                logger.warning(f'analyze_photo_task: InsightFace unavailable ({exc})')

        # ── Run inference ─────────────────────────────────────────────────────
        pil_img = _PILImage.open(str(img_path)).convert('RGB')

        # YOLO
        yolo_results = yolo.predict(source=str(img_path), verbose=False, conf=0.40, iou=0.45)
        labels_set = set()
        for r in yolo_results:
            for cls_id in r.boxes.cls.tolist():
                labels_set.add(yolo.names[int(cls_id)])

        # Scene caption
        scene_description = ''
        try:
            import torch as _torch
            if blip_model is not None:
                inputs = blip_processor(pil_img, return_tensors='pt')
                with _torch.no_grad():
                    out = blip_model.generate(**inputs, max_new_tokens=60)
                scene_description = blip_processor.decode(out[0], skip_special_tokens=True)
            elif florence_model is not None:
                inputs = florence_processor(
                    text='<DETAILED_CAPTION>', images=pil_img, return_tensors='pt'
                )
                with _torch.no_grad():
                    out = florence_model.generate(
                        input_ids=inputs['input_ids'],
                        pixel_values=inputs['pixel_values'],
                        max_new_tokens=80, num_beams=2,
                    )
                raw = florence_processor.batch_decode(out, skip_special_tokens=False)[0]
                parsed = florence_processor.post_process_generation(
                    raw, task='<DETAILED_CAPTION>',
                    image_size=(pil_img.width, pil_img.height),
                )
                scene_description = parsed.get('<DETAILED_CAPTION>', '')
        except Exception as exc:
            logger.warning(f'analyze_photo_task: caption error for {photo_id}: {exc}')

        # CLIP
        clip_embedding = None
        if clip_model is not None:
            try:
                import torch as _torch
                _inputs = clip_processor(images=pil_img, return_tensors='pt')
                with _torch.no_grad():
                    _feats = clip_model.get_image_features(**_inputs)
                    _feats = _feats / _feats.norm(dim=-1, keepdim=True)
                clip_embedding = _feats[0].tolist()
            except Exception as exc:
                logger.warning(f'analyze_photo_task: CLIP error for {photo_id}: {exc}')

        # InsightFace
        face_count = 0
        face_name_list = []
        raw_faces = []
        if face_app is not None:
            try:
                cv_img = cv2.imread(str(img_path))
                if cv_img is not None:
                    faces = face_app.get(cv_img)
                    for face in faces:
                        if face.det_score < 0.50:
                            continue
                        emb = face.embedding.tolist() if face.embedding is not None else None
                        if emb is None:
                            continue
                        try:
                            pose = face.pose.tolist() if face.pose is not None else [0.0, 0.0, 0.0]
                        except Exception:
                            pose = [0.0, 0.0, 0.0]
                        raw_faces.append({
                            'bbox':       face.bbox.tolist(),
                            'embedding':  emb,
                            'confidence': float(face.det_score),
                            'pose':       pose,
                        })
            except Exception as exc:
                logger.warning(f'analyze_photo_task: face detection error for {photo_id}: {exc}')

        face_count = len(raw_faces)

        # Replace prior photo-level face rows so re-analysis is idempotent.
        DetectedFace.objects.filter(photo=photo).delete()

        matched_identities = []

        # Match detected faces against known identities
        if raw_faces:
            NAMED_THRESHOLD = 0.45
            AUTO_THRESHOLD  = 0.50

            named_identities = list(
                FaceIdentity.objects.filter(is_auto_named=False).exclude(ref_embedding='')
            )
            auto_identities = list(
                FaceIdentity.objects.filter(is_auto_named=True).exclude(ref_embedding='')
            )

            def _load_embs(id_list):
                out = []
                for ki in id_list:
                    try:
                        out.append(np.array(json.loads(ki.ref_embedding), dtype=np.float32))
                    except Exception:
                        out.append(None)
                return out

            named_embs = _load_embs(named_identities)
            auto_embs  = _load_embs(auto_identities)

            for rf in raw_faces:
                emb = np.array(rf['embedding'], dtype=np.float32)
                norm = np.linalg.norm(emb)
                if norm > 0:
                    emb = emb / norm

                matched_identity = None

                # 1. Try named identities first
                for ki, ke in zip(named_identities, named_embs):
                    if ke is None:
                        continue
                    cos_sim = float(np.dot(emb, ke) / (np.linalg.norm(ke) + 1e-8))
                    if cos_sim >= NAMED_THRESHOLD:
                        matched_identity = ki
                        break

                # 2. Try auto identities
                if matched_identity is None:
                    best_sim = -1.0
                    for ki, ke in zip(auto_identities, auto_embs):
                        if ke is None:
                            continue
                        cos_sim = float(np.dot(emb, ke) / (np.linalg.norm(ke) + 1e-8))
                        if cos_sim >= AUTO_THRESHOLD and cos_sim > best_sim:
                            best_sim = cos_sim
                            matched_identity = ki

                # 3. Create new auto identity
                if matched_identity is None:
                    n = FaceIdentity.objects.count() + 1
                    matched_identity = FaceIdentity.objects.create(
                        name=f'Person {n}',
                        is_auto_named=True,
                        ref_embedding=json.dumps(rf['embedding']),
                    )
                    auto_identities.append(matched_identity)
                    auto_embs.append(emb)
                else:
                    # Update running average embedding
                    try:
                        old = np.array(json.loads(matched_identity.ref_embedding), dtype=np.float32)
                        updated = (old + emb) / 2.0
                        updated = updated / (np.linalg.norm(updated) + 1e-8)
                        matched_identity.ref_embedding = json.dumps(updated.tolist())
                        matched_identity.save(update_fields=['ref_embedding'])
                    except Exception:
                        pass

                name = matched_identity.name if matched_identity else 'Unknown'
                if name not in face_name_list:
                    face_name_list.append(name)
                matched_identities.append(matched_identity)

        # Save face crops + DetectedFace rows for photo workflow parity
        if raw_faces and matched_identities:
            faces_dir = Path(settings.MEDIA_ROOT) / 'faces' / 'photos' / str(photo.id)
            faces_dir.mkdir(parents=True, exist_ok=True)

            AUTO_CONFIRM_SIM = getattr(settings, 'FACE_AUTO_CONFIRM_THRESHOLD', 0.75)
            face_frontal_scores = []
            for rf in raw_faces:
                yaw = rf['pose'][0] if rf.get('pose') else 0.0
                score = rf['confidence'] * (1.0 - min(abs(yaw), 90.0) / 90.0)
                face_frontal_scores.append(score)

            best_idx = max(range(len(raw_faces)), key=lambda i: face_frontal_scores[i])
            best_crop_rel = ''

            for idx, (rf, identity) in enumerate(zip(raw_faces, matched_identities)):
                crop_rel = ''
                try:
                    x1, y1, x2, y2 = [int(v) for v in rf['bbox']]
                    pad = 15
                    x1 = max(0, x1 - pad); y1 = max(0, y1 - pad)
                    x2 = min(cv_img.shape[1], x2 + pad); y2 = min(cv_img.shape[0], y2 + pad)
                    crop = cv_img[y1:y2, x1:x2]
                    if crop.size > 0:
                        crop_name = f'face_{idx:03d}.jpg'
                        cv2.imwrite(str(faces_dir / crop_name), crop)
                        crop_rel = f'faces/photos/{photo.id}/{crop_name}'
                except Exception as exc:
                    logger.debug(f'analyze_photo_task: crop error for {photo_id} face {idx}: {exc}')

                face_emb = np.array(rf['embedding'], dtype=np.float32)
                ident_emb = np.array(json.loads(identity.ref_embedding), dtype=np.float32) if identity and identity.ref_embedding else None
                sim = _cosine_sim(face_emb, ident_emb) if ident_emb is not None else 0.0
                initial_status = (
                    DetectedFace.STATUS_CONFIRMED
                    if sim >= AUTO_CONFIRM_SIM
                    else DetectedFace.STATUS_UNREVIEWED
                )

                DetectedFace.objects.create(
                    photo=photo,
                    identity=identity,
                    timestamp=0.0,
                    bbox=json.dumps(rf['bbox']),
                    embedding=json.dumps(rf['embedding']),
                    confidence=rf['confidence'],
                    crop_path=crop_rel,
                    status=initial_status,
                )

                if idx == best_idx and crop_rel:
                    best_crop_rel = crop_rel

            if best_crop_rel:
                for identity in set(matched_identities):
                    if not identity:
                        continue
                    if not identity.thumbnail:
                        identity.thumbnail = best_crop_rel
                        identity.save(update_fields=['thumbnail'])

        # ── Generate thumbnail ────────────────────────────────────────────────
        try:
            thumb = pil_img.copy()
            thumb.thumbnail((480, 480), _PILImage.LANCZOS)
            thumb_rel = f'photos/thumbnails/{photo.id}.jpg'
            thumb_path = Path(settings.MEDIA_ROOT) / thumb_rel
            thumb_path.parent.mkdir(parents=True, exist_ok=True)
            thumb.save(str(thumb_path), 'JPEG', quality=82)
            photo.thumbnail = thumb_rel
        except Exception as exc:
            logger.warning(f'analyze_photo_task: thumbnail error for {photo_id}: {exc}')

        # ── OCR — extract visible text from the image ─────────────────────────
        ocr_text = ''
        try:
            import pytesseract as _tess
            ocr_raw = _tess.image_to_string(pil_img, timeout=30)
            ocr_text = ' '.join(ocr_raw.split())   # collapse whitespace
            logger.info(f'analyze_photo_task: OCR extracted {len(ocr_text)} chars for {photo_id}')
        except ImportError:
            pass  # pytesseract not installed — skip silently
        except Exception as exc:
            logger.warning(f'analyze_photo_task: OCR error for {photo_id}: {exc}')

        # ── Duplicate detection (cosine similarity via pgvector) ─────────────
        is_dup    = False
        dup_of_id = None
        if clip_embedding is not None:
            try:
                from pgvector.django import CosineDistance as _CD
                _dup_threshold = 0.03   # cosine distance ≤ 0.03 ≈ similarity ≥ 0.97
                _dup_match = (
                    Photo.objects
                    .exclude(id=photo.id)
                    .exclude(clip_embedding=None)
                    .filter(status=Photo.STATUS_READY)
                    .annotate(_d=_CD('clip_embedding', clip_embedding))
                    .filter(_d__lte=_dup_threshold)
                    .order_by('_d')
                    .only('id')
                    .first()
                )
                if _dup_match:
                    is_dup    = True
                    dup_of_id = _dup_match.id
                    logger.info(
                        f'analyze_photo_task: duplicate detected for {photo_id} '
                        f'— matches {dup_of_id}'
                    )
            except Exception as exc:
                logger.warning(f'analyze_photo_task: duplicate check failed for {photo_id}: {exc}')

        # ── Save results ──────────────────────────────────────────────────────
        photo.labels                = ', '.join(sorted(labels_set))
        photo.face_count            = face_count
        photo.face_names            = ', '.join(face_name_list)
        photo.scene_description     = scene_description
        photo.clip_embedding        = clip_embedding
        photo.ocr_text              = ocr_text
        photo.width                 = pil_img.width
        photo.height                = pil_img.height
        photo.is_potential_duplicate = is_dup
        photo.duplicate_of_id       = dup_of_id
        photo.status                = Photo.STATUS_READY
        photo.processing_error      = ''
        photo.save(update_fields=[
            'labels', 'face_count', 'face_names', 'scene_description',
            'clip_embedding', 'ocr_text', 'width', 'height', 'thumbnail',
            'is_potential_duplicate', 'duplicate_of_id',
            'status', 'processing_error',
        ])
        logger.info(
            f'analyze_photo_task: done for {photo_id} — '
            f'{len(labels_set)} labels, {face_count} faces, '
            f'CLIP={"yes" if clip_embedding else "no"}, '
            f'OCR={len(ocr_text)} chars, '
            f'desc="{scene_description[:60]}"'
        )

    except Exception as exc:
        logger.error(f'analyze_photo_task: unexpected error for {photo_id}: {exc}', exc_info=True)
        try:
            photo.status = Photo.STATUS_FAILED
            photo.processing_error = str(exc)[:500]
            photo.save(update_fields=['status', 'processing_error'])
        except Exception:
            pass
        raise self.retry(exc=exc)
