"""
Celery tasks for ClipStream.

Queues:
    processing  — HLS encoding (slow, CPU-bound)
    captions    — Whisper transcription (slow, model inference)
    default     — everything else
"""
import logging
import math
import os
import subprocess
import json
from pathlib import Path

from celery import shared_task
from django.conf import settings

logger = logging.getLogger(__name__)


# ── Photo format normaliser ───────────────────────────────────────────────────

def _open_any_photo(img_path: Path, ext: str = ''):
    """
    Open any photo file and return a PIL Image in RGB mode.
    Handles: HEIC/HEIF (pillow-heif), PSD (Pillow built-in), RAW (rawpy),
    TIFF, AVIF, and all standard formats supported by Pillow.

    Falls back to plain Pillow open if specialised libs are not installed.
    """
    from PIL import Image as _PIL
    ext = ext or img_path.suffix.lower().lstrip('.')

    # HEIC / HEIF — requires pillow-heif
    if ext in ('heic', 'heif'):
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            logger.warning('_open_any_photo: pillow-heif not installed; attempting PIL fallback')
        return _PIL.open(str(img_path)).convert('RGB')

    # PSD / PSB — Pillow supports via PsdImagePlugin but needs explicit flatten
    if ext in ('psd', 'psb'):
        import PIL.PsdImagePlugin  # ensure plugin is loaded
        img = _PIL.open(str(img_path))
        # PSD may have multiple layers — flatten to composite
        if hasattr(img, 'layers') and img.layers:
            try:
                # Pillow exposes the merged/composite image as the first frame
                img.seek(0)
            except EOFError:
                pass
        return img.convert('RGB')

    # RAW camera formats — requires rawpy (optional)
    RAW_EXTS = {'cr2', 'cr3', 'nef', 'nrw', 'arw', 'srf', 'sr2',
                'dng', 'orf', 'rw2', 'rwl', 'ptx', 'pef', 'raf', 'x3f'}
    if ext in RAW_EXTS:
        try:
            import rawpy
            import numpy as _np
            with rawpy.imread(str(img_path)) as raw:
                rgb = raw.postprocess(use_camera_wb=True, no_auto_bright=False, output_bps=8)
            return _PIL.fromarray(rgb)
        except ImportError:
            logger.warning('_open_any_photo: rawpy not installed; RAW file may fail to open')
        except Exception as exc:
            logger.warning(f'_open_any_photo: rawpy failed ({exc}); trying PIL fallback')
        return _PIL.open(str(img_path)).convert('RGB')

    # All other formats — standard PIL open
    return _PIL.open(str(img_path)).convert('RGB')


# ── Video processing ──────────────────────────────────────────────────────────

@shared_task(
    bind=True,
    name='videos.tasks.process_video_task',
    queue='processing',
    max_retries=2,
    default_retry_delay=30,
    acks_late=True,
)
def process_video_task(self, video_id: str, skip_ai: bool = False):
    """
    Convert uploaded video to HLS (single or multi-quality) + extract thumbnail.
    Always runs regardless of skip_ai.

    skip_ai=False (default): also triggers Whisper captions + frame analysis afterwards.
    skip_ai=True: HLS + thumbnail only — used for live stream recordings when
                  LIVE_STREAM_AUTO_PROCESS=false. Editor triggers AI manually later.
    """
    from .services import process_video
    try:
        process_video(video_id)

        if not skip_ai:
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

        # Seek thumbnail sprite always runs — it's lightweight and needed for playback UX
        if getattr(settings, 'SEEK_THUMBNAILS_ENABLED', True):
            generate_seek_thumbnails_task.apply_async(
                args=[video_id],
                queue='processing',
                countdown=15,
            )
    except Exception as exc:
        logger.error(f'process_video_task failed for {video_id}: {exc}')
        raise self.retry(exc=exc)


# ── Seek / scrub thumbnail sprite ────────────────────────────────────────────

@shared_task(
    bind=True,
    name='videos.tasks.generate_seek_thumbnails_task',
    queue='processing',
    max_retries=2,
    default_retry_delay=30,
)
def generate_seek_thumbnails_task(self, video_id: str):
    """
    Generate a sprite sheet (single JPEG) from the original video for
    seek-bar hover / scrub preview.  One tile per SEEK_THUMBNAIL_INTERVAL
    seconds, laid out in a grid of SEEK_THUMBNAIL_COLS columns.

    Stores the relative media path in Video.seek_sprite.
    Safe to re-run — overwrites the previous sprite.
    """
    if not getattr(settings, 'SEEK_THUMBNAILS_ENABLED', True):
        return

    from .models import Video

    try:
        video = Video.objects.get(id=video_id)
    except Video.DoesNotExist:
        logger.warning(f'[seek_thumbnails] video {video_id} not found')
        return

    if not video.original_file or not video.original_file.name:
        logger.warning(f'[seek_thumbnails] video {video_id} has no original file — skipping')
        return

    interval = getattr(settings, 'SEEK_THUMBNAIL_INTERVAL', 5)
    width    = getattr(settings, 'SEEK_THUMBNAIL_WIDTH',    160)
    height   = getattr(settings, 'SEEK_THUMBNAIL_HEIGHT',   90)
    cols     = getattr(settings, 'SEEK_THUMBNAIL_COLS',     25)
    quality  = getattr(settings, 'SEEK_THUMBNAIL_QUALITY',  4)

    input_path  = video.original_file.path
    sprite_dir  = os.path.join(settings.MEDIA_ROOT, 'seek_sprites')
    os.makedirs(sprite_dir, exist_ok=True)
    sprite_file = os.path.join(sprite_dir, f'{video_id}.jpg')

    # Build the FFmpeg filter:
    #   fps=1/<interval>       — sample one frame every N seconds
    #   scale=W:H              — resize each frame
    #   tile=COLSxROWS         — stitch into a sprite grid
    # Compute the row count from video duration; fall back to 200 if unknown.
    # FFmpeg only fills as many tiles as it generates — the large ceiling is safe.
    duration = video.duration or 0
    if duration and interval:
        total_frames = math.ceil(duration / interval)
        rows = max(1, math.ceil(total_frames / cols))
    else:
        rows = 200  # safe upper bound (~27 hrs at 5-sec interval, 25 cols)
    vf = f'fps=1/{interval},scale={width}:{height},tile={cols}x{rows}'

    cmd = [
        'ffmpeg',
        '-i',        input_path,
        '-vf',       vf,
        '-frames:v', '1',       # output exactly one image (the completed tile)
        '-q:v',      str(quality),
        '-y',                    # overwrite
        sprite_file,
    ]

    logger.info(f'[seek_thumbnails] generating sprite for {video_id}: {" ".join(cmd)}')
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=600)
    except subprocess.TimeoutExpired:
        logger.error(f'[seek_thumbnails] FFmpeg timed out for {video_id}')
        raise self.retry(exc=Exception('FFmpeg timeout'))

    if result.returncode != 0:
        err = result.stderr.decode('utf-8', errors='replace')[-500:]
        logger.error(f'[seek_thumbnails] FFmpeg failed for {video_id}: {err}')
        raise self.retry(exc=Exception(f'FFmpeg exit {result.returncode}'))

    relative_path = f'seek_sprites/{video_id}.jpg'
    Video.objects.filter(id=video_id).update(seek_sprite=relative_path)
    logger.info(f'[seek_thumbnails] sprite saved → {relative_path}')


# ── Manual upscale (Lanczos → replace source → re-encode / re-analyse) ───────

@shared_task(
    bind=True,
    name='videos.tasks.upscale_video_task',
    queue='processing',
    max_retries=1,
    default_retry_delay=60,
    acks_late=True,
)
def upscale_video_task(self, video_id: str, target_long_edge: int):
    from .upscale import run_video_upscale_pipeline

    try:
        run_video_upscale_pipeline(video_id, int(target_long_edge))
    except Exception as exc:
        logger.error(f'upscale_video_task failed for {video_id}: {exc}')
        raise self.retry(exc=exc)


@shared_task(
    bind=True,
    name='videos.tasks.upscale_photo_task',
    queue='processing',
    max_retries=1,
    default_retry_delay=60,
    acks_late=True,
)
def upscale_photo_task(self, photo_id: str, target_long_edge: int):
    from .upscale import run_photo_upscale_pipeline

    try:
        run_photo_upscale_pipeline(photo_id, int(target_long_edge))
    except Exception as exc:
        logger.error(f'upscale_photo_task failed for {photo_id}: {exc}')
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


def _recalc_ref_embedding(identity):
    """
    Recalculate FaceIdentity.ref_embedding from its DetectedFace rows,
    excluding rejected faces and weighting confirmed faces 2× vs unreviewed 1×.
    Saves the result to the identity and returns the new numpy array, or None
    if no eligible faces exist.
    """
    from .models import DetectedFace as _DF
    rows = list(
        _DF.objects
        .filter(identity=identity)
        .exclude(status=_DF.STATUS_REJECTED)
        .exclude(embedding='')
        .values('status', 'embedding')
    )
    if not rows:
        return None
    embeddings = []
    weights = []
    for row in rows:
        try:
            emb = np.array(json.loads(row['embedding']), dtype=np.float32)
            w = 2.0 if row['status'] == _DF.STATUS_CONFIRMED else 1.0
            embeddings.append(emb)
            weights.append(w)
        except Exception:
            continue
    if not embeddings:
        return None
    weights = np.array(weights, dtype=np.float32)
    stacked = np.stack(embeddings, axis=0)
    new_emb = np.average(stacked, axis=0, weights=weights)
    norm = np.linalg.norm(new_emb)
    if norm > 1e-8:
        new_emb = new_emb / norm
    identity.ref_embedding = json.dumps(new_emb.tolist())
    identity.save(update_fields=['ref_embedding'])
    return new_emb


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

    scene_change_enabled   = getattr(settings, 'SCENE_CHANGE_ENABLED',   True)
    scene_change_threshold = getattr(settings, 'SCENE_CHANGE_THRESHOLD', 0.35)
    scene_change_min_gap   = getattr(settings, 'SCENE_CHANGE_MIN_GAP',   0.5)

    tmp_dir = None
    try:
        # ── Step 1: extract frames ────────────────────────────────────────────
        tmp_dir = tempfile.mkdtemp(prefix='fs_frames_')
        frame_pattern = os.path.join(tmp_dir, 'frame_%05d.jpg')

        # Build the -vf filter.
        # Scene-change mode:  fires on every hard cut (pixel diff > threshold)
        #   OR every FRAME_INTERVAL_SECONDS — whichever comes first.
        # showinfo is appended so FFmpeg writes pts_time:X.XXX for every output
        #   frame to stderr; we parse that to get real timestamps instead of
        #   inferring them from the sequential frame index.
        if scene_change_enabled:
            # eq(n,0)                       → always keep the very first frame
            # gt(scene,T)                   → keep any hard-cut frame
            # gte(t-prev_selected_t, N)     → keep a frame every N seconds
            #   (prev_selected_t is a built-in select-filter variable — the
            #    timestamp of the last selected frame, NaN before the first one)
            # NOTE: 'fr' is NOT available in select expressions — use 't' / 'n'.
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
            logger.error(
                f'analyze_video_frames_task: ffmpeg failed for {video_id}: '
                f'{result.stderr[-400:]}'
            )
            return

        frame_files = sorted(Path(tmp_dir).glob('frame_*.jpg'))
        if not frame_files:
            logger.info(f'analyze_video_frames_task: no frames extracted for {video_id}')
            return

        # ── Parse actual timestamps from showinfo stderr output ───────────────
        # showinfo writes a line per frame containing pts_time:X.XXXXXX
        import re as _re
        raw_timestamps = [
            float(m.group(1))
            for m in (_re.search(r'pts_time:(\S+)', ln) for ln in result.stderr.splitlines())
            if m
        ]

        if len(raw_timestamps) == len(frame_files):
            frame_timestamps = raw_timestamps
        else:
            # Fallback: infer from sequential index (old behaviour)
            logger.warning(
                f'analyze_video_frames_task: showinfo timestamp count '
                f'({len(raw_timestamps)}) != frame file count ({len(frame_files)}) '
                f'for {video_id} — falling back to interval-based timestamps'
            )
            frame_timestamps = [float(i * interval) for i in range(len(frame_files))]

        # ── Step 1b: deduplicate near-adjacent frames ─────────────────────────
        # A scene-cut frame that lands within scene_change_min_gap seconds of a
        # regular interval frame is redundant — drop it (and its file) to avoid
        # double-processing the same visual content.
        kept_files: list = []
        kept_timestamps: list = []
        last_kept_t = -999.0
        for f, t in zip(frame_files, frame_timestamps):
            if t - last_kept_t >= scene_change_min_gap:
                kept_files.append(f)
                kept_timestamps.append(t)
                last_kept_t = t
            else:
                try:
                    Path(f).unlink(missing_ok=True)
                except OSError:
                    pass

        frame_files      = kept_files
        frame_timestamps = kept_timestamps

        logger.info(
            f'analyze_video_frames_task: {len(frame_files)} frames for {video_id} '
            f'(scene-change extraction: {scene_change_enabled})'
        )

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

        for idx, (frame_path, timestamp) in enumerate(zip(frame_files, frame_timestamps)):

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
                        # ── Quality gates ─────────────────────────────────────
                        # 1. Detection confidence (InsightFace score 0–1)
                        if face.det_score < getattr(settings, 'FACE_MIN_DET_SCORE', 0.65):
                            continue
                        # 2. Face size — skip tiny faces (crowd, background)
                        x1, y1, x2, y2 = face.bbox
                        face_area = max(0, x2 - x1) * max(0, y2 - y1)
                        if face_area < getattr(settings, 'FACE_MIN_AREA_PX', 3600):  # 60×60 px
                            continue
                        # 3. Pose — skip extreme profiles / back-of-head shots
                        try:
                            pose = face.pose.tolist() if face.pose is not None else [0.0, 0.0, 0.0]
                        except Exception:
                            pose = [0.0, 0.0, 0.0]
                        if abs(pose[0]) > getattr(settings, 'FACE_MAX_YAW_DEG', 60):
                            continue
                        # ──────────────────────────────────────────────────────
                        emb = face.embedding.tolist() if face.embedding is not None else None
                        if emb is None:
                            continue
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
                    _recalc_ref_embedding(matched)
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

            # Auto-confirm using the identity's ref_embedding (running average across
            # all analyses) rather than the ephemeral per-video cluster centroid.
            # This keeps confirmation consistent across re-analyses and matches
            # the same logic used in analyze_photo_task and auto_confirm_similar.
            face_emb = np.array(rf['embedding'], dtype=np.float32)
            identity = identities[cluster_id]
            try:
                ref_arr = np.array(json.loads(identity.ref_embedding), dtype=np.float32) if identity.ref_embedding else None
            except Exception:
                ref_arr = None
            sim = _cosine_sim(face_emb, ref_arr if ref_arr is not None else cluster_means[cluster_id])
            initial_status = (
                DetectedFace.STATUS_CONFIRMED
                if sim >= AUTO_CONFIRM_SIM
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

        # Best-frontal crop → FaceIdentity.thumbnail. Auto-named identities always
        # get an updated representative crop from this video; user-tagged identities
        # keep their existing thumbnail when re-matched so uploads don’t replace it.
        for cluster_id, best_face_idx in identity_best_crop_idx.items():
            best_crop = saved_crop_paths.get(best_face_idx, '')
            if not best_crop:
                continue
            ident = identities[cluster_id]
            if ident.is_auto_named or not (ident.thumbnail or '').strip():
                ident.thumbnail = best_crop
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
        'vi': 'Vietnamese', 'th': 'Thai', 'id': 'Indonesian', 'ms': 'Malay',
        'cs': 'Czech', 'ro': 'Romanian', 'hu': 'Hungarian', 'bg': 'Bulgarian',
        'hr': 'Croatian', 'sk': 'Slovak', 'el': 'Greek', 'he': 'Hebrew',
        'bn': 'Bengali', 'ta': 'Tamil', 'te': 'Telugu', 'ur': 'Urdu',
        'fa': 'Persian', 'sw': 'Swahili',
    }
    return _MAP.get(code, code.upper())


# ── NLLB Translation helpers ───────────────────────────────────────────────────

# BCP-47 → FLORES-200 code mapping for NLLB-200
_BCP47_TO_FLORES = {
    'en': 'eng_Latn', 'fr': 'fra_Latn', 'es': 'spa_Latn', 'de': 'deu_Latn',
    'it': 'ita_Latn', 'pt': 'por_Latn', 'ru': 'rus_Cyrl', 'ja': 'jpn_Jpan',
    'ko': 'kor_Hang', 'zh': 'zho_Hans', 'ar': 'arb_Arab', 'hi': 'hin_Deva',
    'nl': 'nld_Latn', 'pl': 'pol_Latn', 'tr': 'tur_Latn', 'sv': 'swe_Latn',
    'da': 'dan_Latn', 'fi': 'fin_Latn', 'nb': 'nob_Latn', 'uk': 'ukr_Cyrl',
    'vi': 'vie_Latn', 'th': 'tha_Thai', 'id': 'ind_Latn', 'ms': 'zsm_Latn',
    'cs': 'ces_Latn', 'ro': 'ron_Latn', 'hu': 'hun_Latn', 'bg': 'bul_Cyrl',
    'hr': 'hrv_Latn', 'sk': 'slk_Latn', 'el': 'ell_Grek', 'he': 'heb_Hebr',
    'bn': 'ben_Beng', 'ta': 'tam_Taml', 'te': 'tel_Telu', 'ur': 'urd_Arab',
    'fa': 'pes_Arab', 'sw': 'swh_Latn',
}

# Module-level cache so the model loads once per worker process
_nllb_model     = None
_nllb_tokenizer = None


def _get_nllb():
    global _nllb_model, _nllb_tokenizer
    if _nllb_model is None:
        from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
        import torch
        model_name = getattr(settings, 'NLLB_MODEL', 'facebook/nllb-200-distilled-600M')
        device     = getattr(settings, 'TRANSLATION_DEVICE', 'cpu')
        logger.info(f'[translation] loading NLLB model {model_name} on {device}')
        _nllb_tokenizer = AutoTokenizer.from_pretrained(model_name)
        _nllb_model     = AutoModelForSeq2SeqLM.from_pretrained(model_name)
        if device != 'cpu':
            _nllb_model = _nllb_model.to(device)
        _nllb_model.eval()
        logger.info('[translation] NLLB model loaded')
    return _nllb_model, _nllb_tokenizer


def _translate_batch(texts, src_flores, tgt_flores, batch_size=32):
    """Translate a list of strings from src_flores to tgt_flores using NLLB."""
    import torch
    model, tokenizer = _get_nllb()
    device = next(model.parameters()).device
    results = []
    for i in range(0, len(texts), batch_size):
        chunk = texts[i:i + batch_size]
        tokenizer.src_lang = src_flores
        encoded = tokenizer(chunk, return_tensors='pt', padding=True,
                            truncation=True, max_length=512).to(device)
        with torch.no_grad():
            generated = model.generate(
                **encoded,
                forced_bos_token_id=tokenizer.convert_tokens_to_ids(tgt_flores),
                max_length=512,
                num_beams=4,
            )
        results.extend(tokenizer.batch_decode(generated, skip_special_tokens=True))
    return results


def _parse_vtt(content):
    """Parse WebVTT content into list of (start, end, text) tuples."""
    import re
    cues = []
    for block in re.split(r'\n\n+', content.strip()):
        lines = [l for l in block.strip().splitlines() if l.strip()]
        ts_idx = next((i for i, l in enumerate(lines) if '-->' in l), None)
        if ts_idx is None:
            continue
        m = re.match(r'(\S+)\s+-->\s+(\S+)', lines[ts_idx])
        if not m:
            continue
        text = '\n'.join(lines[ts_idx + 1:]).strip()
        if text:
            cues.append((m.group(1), m.group(2), text))
    return cues


def _build_vtt(cues):
    """Rebuild WebVTT from (start, end, text) tuples."""
    lines = ['WEBVTT', '']
    for i, (start, end, text) in enumerate(cues, 1):
        lines += [str(i), f'{start} --> {end}', text, '']
    return '\n'.join(lines)


# ── Subtitle translation task (NLLB-200) ──────────────────────────────────────

@shared_task(
    bind=True,
    name='videos.tasks.translate_subtitles_task',
    queue='translation',
    max_retries=2,
    acks_late=True,
    soft_time_limit=1800,
    time_limit=2100,
)
def translate_subtitles_task(self, video_id: str, target_languages: list,
                              source_subtitle_id: int = None):
    """
    Translate the source subtitle of a video into each target language using
    facebook/nllb-200-distilled-600M.

    Pipeline:
        1. Load source Subtitle VTT (Whisper-generated, or the specified one)
        2. Parse VTT cues
        3. For each target language: batch-translate cue texts via NLLB
        4. Rebuild VTT and save a new Subtitle record (is_translation=True)
    """
    from .models import Video, Subtitle
    from django.core.files.base import ContentFile

    if not getattr(settings, 'TRANSLATION_ENABLED', True):
        logger.info('[translation] disabled via settings, skipping')
        return

    try:
        video = Video.objects.get(id=video_id)
    except Video.DoesNotExist:
        logger.warning(f'[translation] video {video_id} not found')
        return

    # ── Step 1: find source subtitle ─────────────────────────────────────────
    if source_subtitle_id:
        try:
            source_sub = Subtitle.objects.get(id=source_subtitle_id, video=video)
        except Subtitle.DoesNotExist:
            logger.warning(f'[translation] source subtitle {source_subtitle_id} not found')
            return
    else:
        source_sub = (
            video.subtitles.filter(is_auto_generated=True, is_translation=False)
                           .order_by('created_at').first()
        )
        if not source_sub:
            source_sub = video.subtitles.filter(is_translation=False).order_by('created_at').first()
        if not source_sub:
            logger.warning(f'[translation] no source subtitle for video {video_id}')
            return

    src_bcp47  = source_sub.language
    src_flores = _BCP47_TO_FLORES.get(src_bcp47)
    if not src_flores:
        logger.warning(f'[translation] unsupported source language {src_bcp47}')
        return

    # ── Step 2: parse VTT ────────────────────────────────────────────────────
    try:
        with source_sub.file.open('r') as f:
            vtt_content = f.read()
    except Exception as exc:
        logger.error(f'[translation] could not read source subtitle: {exc}')
        return

    cues = _parse_vtt(vtt_content)
    if not cues:
        logger.warning(f'[translation] no cues in source subtitle {source_sub.id}')
        return

    cue_texts = [text for _, _, text in cues]

    batch_size = getattr(settings, 'TRANSLATION_BATCH_SIZE', 32)

    # ── Step 3 & 4: translate and save per target language ───────────────────
    for lang in target_languages:
        if lang == src_bcp47:
            continue  # skip same-language "translation"

        tgt_flores = _BCP47_TO_FLORES.get(lang)
        if not tgt_flores:
            logger.warning(f'[translation] unsupported target language {lang}, skipping')
            continue

        logger.info(f'[translation] {src_bcp47} → {lang} for video {video_id}')
        try:
            translated_texts = _translate_batch(cue_texts, src_flores, tgt_flores, batch_size)
        except Exception as exc:
            logger.error(f'[translation] failed {src_bcp47}→{lang} for {video_id}: {exc}')
            continue

        translated_cues = [
            (start, end, trans)
            for (start, end, _), trans in zip(cues, translated_texts)
        ]
        translated_vtt = _build_vtt(translated_cues)

        # Delete any existing auto-translated subtitle for this language
        Subtitle.objects.filter(
            video=video, language=lang, is_auto_generated=True, is_translation=True
        ).delete()

        sub = Subtitle(
            video=video,
            language=lang,
            language_label=_lang_label(lang),
            format=Subtitle.FORMAT_VTT,
            is_auto_generated=True,
            is_translation=True,
            source_language=src_bcp47,
        )
        sub.file.save(
            f'{video_id}_{lang}_translated.vtt',
            ContentFile(translated_vtt.encode('utf-8')),
            save=False,
        )
        sub.save()
        logger.info(f'[translation] saved {lang} subtitle for video {video_id} (id={sub.id})')

    logger.info(f'[translation] done for video {video_id}')


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


# ── EXIF helper ──────────────────────────────────────────────────────────────

def _extract_photo_exif(img_path):
    """
    Extract EXIF metadata from a photo using Pillow.
    Returns (exif_dict, taken_at_datetime | None).
    Only stores human-readable fields — skips binary blobs and MakerNote.
    """
    import datetime
    from PIL import Image as _PILImg
    from PIL.ExifTags import TAGS, GPSTAGS

    KEEP_TAGS = {
        'Make', 'Model', 'LensMake', 'LensModel', 'Software',
        'DateTimeOriginal', 'DateTime', 'DateTimeDigitized',
        'FocalLength', 'FNumber', 'ISOSpeedRatings', 'ExposureTime',
        'ExposureProgram', 'Flash', 'WhiteBalance', 'MeteringMode',
        'Orientation', 'PixelXDimension', 'PixelYDimension',
        'GPSInfo',
    }
    try:
        # Register HEIC opener if available so EXIF can be read from HEIC files
        ext = Path(img_path).suffix.lower().lstrip('.')
        if ext in ('heic', 'heif'):
            try:
                import pillow_heif
                pillow_heif.register_heif_opener()
            except ImportError:
                pass
        img = _PILImg.open(img_path)
        raw = img.getexif()
        if not raw:
            return {}, None
    except Exception:
        return {}, None

    exif = {}
    taken_at = None

    def _json_safe(v):
        # Pillow EXIF can include bytes (esp. GPS). JSONField can't.
        if isinstance(v, memoryview):
            v = v.tobytes()
        if isinstance(v, (bytes, bytearray)):
            if len(v) == 1:
                # Common for GPSAltitudeRef (0/1) and similar flags.
                return int(v[0])
            try:
                s = v.decode('utf-8', errors='replace')
                # Postgres JSON cannot contain NUL (\u0000).
                s = s.replace('\x00', '')
                return s[:200]
            except Exception:
                return v.hex()[:400]
        if isinstance(v, (tuple, list)):
            return [_json_safe(x) for x in v]
        if isinstance(v, dict):
            return {str(k): _json_safe(val) for k, val in v.items()}
        if isinstance(v, str):
            return v.replace('\x00', '')  # keep caller’s truncation rules
        # Last resort: stringify, but ensure no NULs sneak in.
        try:
            s = str(v)
        except Exception:
            return None
        return s.replace('\x00', '')[:200]
        return v

    for tag_id, value in raw.items():
        tag = TAGS.get(tag_id, None)
        if not tag or tag not in KEEP_TAGS:
            continue
        if tag == 'GPSInfo':
            try:
                gps_raw = raw.get_ifd(tag_id)
                gps = {}
                for gps_id, gps_val in gps_raw.items():
                    gps_tag = GPSTAGS.get(gps_id, str(gps_id))
                    if isinstance(gps_val, (tuple, list)):
                        gps[gps_tag] = [
                            float(v) if hasattr(v, 'numerator') else _json_safe(v)
                            for v in gps_val
                        ]
                    elif hasattr(gps_val, 'numerator'):
                        gps[gps_tag] = float(gps_val)
                    else:
                        gps[gps_tag] = _json_safe(gps_val)
                if gps:
                    exif['GPSInfo'] = gps
            except Exception:
                pass
        elif tag in ('DateTimeOriginal', 'DateTime', 'DateTimeDigitized'):
            try:
                raw_dt = str(value)
                dt = None
                # Common EXIF format
                try:
                    dt = datetime.datetime.strptime(raw_dt, '%Y:%m:%d %H:%M:%S')
                except Exception:
                    dt = None
                # Some pipelines/devices store ISO-ish strings already
                if dt is None:
                    try:
                        dt = datetime.datetime.fromisoformat(raw_dt.replace('Z', '+00:00'))
                    except Exception:
                        dt = None
                if dt is None:
                    raise ValueError('unparseable datetime')
                exif[tag] = dt.isoformat()
                # iPhone often provides DateTime (not DateTimeOriginal).
                # Use the first available datetime as taken_at.
                if taken_at is None:
                    # If dt is naive, assume UTC. If aware, normalize to UTC.
                    if dt.tzinfo is None:
                        taken_at = dt.replace(tzinfo=datetime.timezone.utc)
                    else:
                        taken_at = dt.astimezone(datetime.timezone.utc)
            except (ValueError, TypeError):
                exif[tag] = _json_safe(str(value))[:200]
        elif hasattr(value, 'numerator'):
            exif[tag] = float(value)
        elif isinstance(value, (int, float, bool)):
            exif[tag] = value
        elif isinstance(value, str):
            exif[tag] = _json_safe(value)[:200]
        elif isinstance(value, (bytes, bytearray)):
            exif[tag] = _json_safe(value)
        # skip bytes / unknown types

    return exif, taken_at


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

        # ── Extract EXIF (before converting to RGB which strips EXIF) ─────────
        exif_data, taken_at = _extract_photo_exif(img_path)

        # ── Normalise image to PIL RGB — handles HEIC, PSD, RAW, TIFF, etc. ──
        img_ext = img_path.suffix.lower().lstrip('.')
        pil_img = _open_any_photo(img_path, img_ext)

        # ── Run inference ─────────────────────────────────────────────────────
        # For special formats that YOLO can't open directly, use the PIL array
        _yolo_source: str | np.ndarray
        if img_ext in {'heic', 'heif', 'psd', 'psb', 'avif',
                       'cr2', 'cr3', 'nef', 'nrw', 'arw', 'srf', 'sr2',
                       'dng', 'orf', 'rw2', 'rwl', 'ptx', 'pef', 'raf', 'x3f'}:
            import numpy as _np2
            _yolo_source = _np2.array(pil_img)[:, :, ::-1].copy()  # RGB→BGR for YOLO
        else:
            _yolo_source = str(img_path)

        # YOLO
        yolo_results = yolo.predict(source=_yolo_source, verbose=False, conf=0.40, iou=0.45)
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
            # Cross-source threshold is slightly lower than intra-video (0.50) because
            # photo vs video appearance can differ meaningfully in lighting/angle/resolution.
            AUTO_THRESHOLD  = 0.45

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
                    # Recalculate embedding weighted by review status
                    try:
                        _recalc_ref_embedding(matched_identity)
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

            # Pre-compute frontal score per face for best-crop selection
            face_frontal_scores = []
            for rf in raw_faces:
                yaw = rf['pose'][0] if rf.get('pose') else 0.0
                score = rf['confidence'] * (1.0 - min(abs(yaw), 90.0) / 90.0)
                face_frontal_scores.append(score)

            # Track the best (most-frontal) crop PER IDENTITY, not per photo.
            # Previously this used a single photo-level best_idx, which caused all
            # identities in a multi-face photo to get the same thumbnail.
            identity_best: dict = {}  # identity.pk -> (frontal_score, crop_rel)

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

                # Track best crop for this specific identity
                if identity and crop_rel:
                    score = face_frontal_scores[idx]
                    prev = identity_best.get(identity.pk)
                    if prev is None or score > prev[0]:
                        identity_best[identity.pk] = (score, crop_rel)

            # Assign each identity its own best crop as thumbnail (only if unset)
            for identity in set(matched_identities):
                if not identity:
                    continue
                best = identity_best.get(identity.pk)
                if best and not identity.thumbnail:
                    identity.thumbnail = best[1]
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
        photo.exif_data             = exif_data if exif_data else None
        photo.taken_at              = taken_at

        # Extract GPS into indexed lat/lng fields for fast geo queries
        try:
            gps = photo.exif_data.get('GPSInfo', {}) if photo.exif_data else {}
            if gps and 'GPSLatitude' in gps and 'GPSLongitude' in gps:
                def _dms(arr, ref):
                    d, m, s = arr[0], arr[1], arr[2]
                    dec = d + m/60 + s/3600
                    return -dec if ref in ('S', 'W') else dec
                photo.latitude  = round(_dms(gps['GPSLatitude'],  gps.get('GPSLatitudeRef',  'N')), 7)
                photo.longitude = round(_dms(gps['GPSLongitude'], gps.get('GPSLongitudeRef', 'E')), 7)
                # Auto-assign nearest named place (inline to avoid circular import)
                from .models import NamedPlace as _NP
                import math as _m
                def _hav(lat1, lon1, lat2, lon2):
                    R = 6_371_000
                    p1, p2 = _m.radians(lat1), _m.radians(lat2)
                    dp = _m.radians(lat2 - lat1)
                    dl = _m.radians(lon2 - lon1)
                    a = _m.sin(dp/2)**2 + _m.cos(p1)*_m.cos(p2)*_m.sin(dl/2)**2
                    return 2 * R * _m.asin(_m.sqrt(a))
                best, best_dist = None, float('inf')
                for place in _NP.objects.all():
                    dist = _hav(photo.latitude, photo.longitude, place.latitude, place.longitude)
                    if dist <= place.radius_meters and dist < best_dist:
                        best, best_dist = place, dist
                photo.named_place = best
        except Exception:
            pass

        photo.status                = Photo.STATUS_READY
        photo.processing_error      = ''
        photo.save(update_fields=[
            'labels', 'face_count', 'face_names', 'scene_description',
            'clip_embedding', 'ocr_text', 'width', 'height', 'thumbnail',
            'is_potential_duplicate', 'duplicate_of_id',
            'exif_data', 'taken_at',
            'latitude', 'longitude', 'named_place',
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


# ── Speaker Diarization task ──────────────────────────────────────────────────

def _upsert_speaker_face_suggestions(video, segments: list) -> int:
    """
    Per-video speaker→face suggestions from temporal overlap (pending only).
    Tuned to reduce false links from short interjections between longer turns.
    """
    from .models import DetectedFace, SpeakerFaceSuggestion

    if not segments:
        return 0

    face_window_half = float(getattr(settings, 'VOICE_FACE_WINDOW_HALF_SECONDS', 0.75))
    min_seg_seconds = float(getattr(settings, 'VOICE_FACE_MIN_SEGMENT_SECONDS', 1.25))
    merge_gap_seconds = float(getattr(settings, 'VOICE_FACE_MERGE_GAP_SECONDS', 0.5))
    min_overlap_seconds = float(getattr(settings, 'VOICE_FACE_MIN_OVERLAP_SECONDS', 1.5))
    min_overlap_hits = int(getattr(settings, 'VOICE_FACE_MIN_HITS', 2))
    min_score = float(getattr(settings, 'VOICE_FACE_MIN_SCORE', 0.18))

    face_rows = list(
        DetectedFace.objects
        .filter(video=video, identity__isnull=False)
        .exclude(identity__name='')
        .values('identity_id', 'timestamp')
    )
    if not face_rows:
        return 0

    face_windows = {}
    for row in face_rows:
        fid = row['identity_id']
        ts = float(row['timestamp'] or 0.0)
        face_windows.setdefault(fid, []).append((max(0.0, ts - face_window_half), ts + face_window_half))

    speaker_segments = {}
    for seg in segments:
        si = getattr(seg, 'speaker_identity', None)
        if not si:
            continue
        # Skip speakers that are already matched/identified (not auto-named)
        if not si.is_auto_named:
            continue
        s_start = float(seg.start_seconds or 0.0)
        s_end = float(seg.end_seconds or 0.0)
        if (s_end - s_start) < min_seg_seconds:
            continue
        speaker_segments.setdefault(si.pk, []).append((s_start, s_end))

    merged_by_speaker = {}
    for speaker_id, spans in speaker_segments.items():
        if not spans:
            continue
        spans = sorted(spans, key=lambda x: x[0])
        merged = [list(spans[0])]
        for s_start, s_end in spans[1:]:
            prev_start, prev_end = merged[-1]
            if s_start <= (prev_end + merge_gap_seconds):
                merged[-1][1] = max(prev_end, s_end)
            else:
                merged.append([s_start, s_end])
        merged_by_speaker[speaker_id] = [(a, b) for a, b in merged]

    upserts = 0
    for speaker_id, spans in merged_by_speaker.items():
        total_speech = sum(max(0.0, e - s) for s, e in spans)
        if total_speech <= 0:
            continue

        for face_id, windows in face_windows.items():
            overlap = 0.0
            hits = 0
            for s_start, s_end in spans:
                for f_start, f_end in windows:
                    ov = min(s_end, f_end) - max(s_start, f_start)
                    if ov > 0:
                        overlap += ov
                        hits += 1
            if overlap < min_overlap_seconds or hits < min_overlap_hits:
                continue

            score = overlap / max(total_speech, 0.001)
            if score < min_score:
                continue

            evidence = {
                'heuristic': 'temporal_overlap',
                'overlap_seconds': round(overlap, 3),
                'speaker_total_seconds': round(total_speech, 3),
                'overlap_ratio': round(score, 4),
                'hit_windows': hits,
                'filters': {
                    'min_segment_seconds': min_seg_seconds,
                    'merge_gap_seconds': merge_gap_seconds,
                    'min_overlap_seconds': min_overlap_seconds,
                    'min_hits': min_overlap_hits,
                    'min_score': min_score,
                    'face_window_half_seconds': face_window_half,
                },
            }
            sug, created = SpeakerFaceSuggestion.objects.get_or_create(
                speaker_identity_id=speaker_id,
                face_identity_id=face_id,
                video=video,
                defaults={
                    'score': float(score),
                    'overlap_seconds': float(overlap),
                    'evidence': evidence,
                    'status': SpeakerFaceSuggestion.STATUS_PENDING,
                },
            )
            if not created and sug.status != SpeakerFaceSuggestion.STATUS_PENDING:
                continue
            if not created:
                sug.score = float(score)
                sug.overlap_seconds = float(overlap)
                sug.evidence = evidence
                sug.decided_at = None
                sug.save(update_fields=['score', 'overlap_seconds', 'evidence', 'updated_at', 'decided_at'])
            upserts += 1
    return upserts


@shared_task(
    bind=True,
    name='videos.tasks.run_diarization_task',
    queue='captions',
    max_retries=1,
    acks_late=True,
)
def run_diarization_task(self, video_id: str):
    """
    Run speaker diarization on a video using pyannote.audio.
    Assigns a speaker_label (e.g. SPEAKER_00) to every VideoSegment.

    Requirements:
      - pip install pyannote.audio
      - HF_TOKEN set in .env (HuggingFace token with access to pyannote models)
      - Accept terms at https://hf.co/pyannote/speaker-diarization-3.1
    """
    from pathlib import Path
    from .models import Video, VideoSegment

    hf_token = getattr(settings, 'HF_TOKEN', '')
    if not hf_token:
        logger.error('run_diarization_task: HF_TOKEN not set — cannot load pyannote models')
        return {'error': 'HF_TOKEN not configured. Set it in .env and restart Celery.'}

    try:
        video = Video.objects.get(id=video_id)
    except Video.DoesNotExist:
        logger.warning(f'run_diarization_task: video {video_id} not found')
        return

    segments = list(VideoSegment.objects.filter(video=video).order_by('start_seconds'))
    if not segments:
        # Keep diarization semantics clear (needs transcript), but still trigger
        # audio-event detection so non-speech clips (applause/music-only) can
        # populate the X-ray audio lane.
        audio_queued = False
        if getattr(settings, 'AUDIO_EVENTS_ENABLED', True):
            try:
                queue = getattr(settings, 'AUDIO_EVENTS_QUEUE', 'audio')
                detect_audio_events_task.apply_async(args=[str(video.id)], queue=queue)
                audio_queued = True
                logger.info(
                    f'run_diarization_task: no transcript segments for {video.id}; '
                    f'queued detect_audio_events_task on queue "{queue}"'
                )
            except Exception as ae_exc:
                logger.warning(
                    f'run_diarization_task: could not enqueue audio-event task '
                    f'for {video.id} after no-segment diarization skip — {ae_exc}'
                )
        return {
            'error': 'No transcript segments found. Diarization skipped.',
            'audio_events_queued': audio_queued,
        }

    # ── Prepare audio file ────────────────────────────────────────────────────
    audio_dir  = Path(settings.MEDIA_ROOT) / 'audio'
    audio_path = audio_dir / f'{video_id}.wav'

    if not audio_path.exists():
        audio_dir.mkdir(parents=True, exist_ok=True)
        source = None
        if video.original_file and video.original_file.name:
            candidate = Path(settings.MEDIA_ROOT) / video.original_file.name
            if candidate.exists():
                source = candidate
        if source is None:
            logger.error(f'run_diarization_task: no audio source for {video_id}')
            return {'error': 'No source file found. Upload or re-process the video first.'}

        ffmpeg = getattr(settings, 'FFMPEG_PATH', 'ffmpeg')
        result = subprocess.run(
            [ffmpeg, '-y', '-i', str(source),
             '-ar', '16000', '-ac', '1', '-f', 'wav', str(audio_path)],
            capture_output=True,
        )
        if result.returncode != 0:
            logger.error(f'run_diarization_task: ffmpeg failed: {result.stderr.decode()[:400]}')
            return {'error': 'Audio extraction failed. Check FFmpeg logs.'}

    # ── Run pyannote diarization ───────────────────────────────────────────────
    try:
        import torch
        from pyannote.audio import Pipeline as _Pipeline

        pipeline = _Pipeline.from_pretrained(
            'pyannote/speaker-diarization-3.1',
            token=hf_token,
        )
        device = getattr(settings, 'WHISPER_DEVICE', 'cpu')
        if device == 'cuda' and torch.cuda.is_available():
            pipeline.to(torch.device('cuda'))

        raw = pipeline(str(audio_path))
        # Unwrap whatever pyannote returns — version differences:
        #   3.1.x → Annotation directly (has itertracks)
        #   3.3.x → DiarizeOutput dataclass (use vars() to walk fields)
        #   some builds → NamedTuple (use _asdict() to walk fields)
        logger.info(f'run_diarization_task: pyannote output type={type(raw).__name__}')
        if hasattr(raw, 'itertracks'):
            annotation = raw
        else:
            # Collect all field values from dataclass (__dict__) or namedtuple (_asdict)
            if hasattr(raw, '__dict__'):
                candidates = list(vars(raw).values())
            elif hasattr(raw, '_fields'):
                candidates = list(raw._asdict().values())
            else:
                candidates = []
            annotation = next((v for v in candidates if hasattr(v, 'itertracks')), None)
            if annotation is None:
                field_types = {k: type(v).__name__ for k, v in (vars(raw) if hasattr(raw, '__dict__') else {}).items()}
                raise RuntimeError(
                    f'Cannot find Annotation in pyannote {type(raw).__name__}. '
                    f'Fields: {field_types}'
                )
        speaker_segments = [
            (turn.start, turn.end, label)
            for turn, _, label in annotation.itertracks(yield_label=True)
        ]

    except ImportError:
        return {'error': 'pyannote.audio not installed. Run: pip install pyannote.audio'}
    except Exception as exc:
        logger.error(f'run_diarization_task: pyannote failed for {video_id}: {exc}', exc_info=True)
        raise self.retry(exc=exc)

    # ── Remap speaker labels to globally unique numbers ───────────────────────
    # pyannote always starts from SPEAKER_00 per-run. We find the global max
    # already stored and continue from there so labels are unique across videos.
    import re as _re
    existing_labels = (
        VideoSegment.objects
        .exclude(speaker_label='')
        .exclude(video=video)           # ignore this video's own old labels if re-running
        .values_list('speaker_label', flat=True)
        .distinct()
    )
    max_existing = -1
    for lbl in existing_labels:
        m = _re.search(r'(\d+)$', lbl)
        if m:
            max_existing = max(max_existing, int(m.group(1)))

    # Build a remap: pyannote's local SPEAKER_00 → global SPEAKER_<max+1>, etc.
    local_speakers = sorted({s[2] for s in speaker_segments})
    remap = {
        local: f'SPEAKER_{max_existing + 1 + i:02d}'
        for i, local in enumerate(local_speakers)
    }
    logger.info(f'run_diarization_task: speaker remap {remap}')

    # ── Match each segment to dominant speaker by overlap ─────────────────────
    updated = 0
    for seg in segments:
        best_label   = ''
        best_overlap = 0.0
        for s_start, s_end, label in speaker_segments:
            overlap = min(seg.end_seconds, s_end) - max(seg.start_seconds, s_start)
            if overlap > best_overlap:
                best_overlap = overlap
                best_label   = label
        if best_label:
            seg.speaker_label = remap[best_label]
            updated += 1

    VideoSegment.objects.bulk_update(segments, ['speaker_label'])
    logger.info(f'run_diarization_task: labelled {updated}/{len(segments)} segments for {video_id}')

    # ── Per-speaker stats (for role heuristic) ────────────────────────────────
    from videos.models import SpeakerIdentity  # local import avoids circular
    import numpy as np

    global_labels = list(remap.values())  # e.g. ['SPEAKER_02', 'SPEAKER_03']

    # key: global_label → {'total_duration': float, 'seg_count': int}
    speaker_stats: dict = {}
    for seg in segments:
        lbl = seg.speaker_label
        if not lbl:
            continue
        dur = (seg.end_seconds or 0) - (seg.start_seconds or 0)
        if lbl not in speaker_stats:
            speaker_stats[lbl] = {'total_duration': 0.0, 'seg_count': 0}
        speaker_stats[lbl]['total_duration'] += max(dur, 0)
        speaker_stats[lbl]['seg_count'] += 1

    # Auto-role heuristic thresholds (only applied to auto-named identities)
    NARRATOR_AVG_DURATION   = 10.0   # avg segment ≥ 10s → narrator candidate
    BACKGROUND_TOTAL        = 20.0   # total speaking time < 20s → background
    BACKGROUND_AVG_DURATION = 2.0    # avg segment < 2s → background

    # ── Extract speaker embeddings via wespeaker ───────────────────────────────
    # pyannote/wespeaker-voxceleb-resnet34-LM is already downloaded as an
    # internal dependency of the speaker-diarization-3.1 pipeline.
    # We load it separately here to compute per-speaker mean embeddings used
    # for cross-video speaker matching.
    SPEAKER_MATCH_THRESHOLD = getattr(settings, 'SPEAKER_MATCH_THRESHOLD', 0.75)

    speaker_embeddings: dict = {}  # global_label → np.ndarray(256,) normalised
    try:
        from pyannote.audio import Model, Inference
        from pyannote.audio import Audio as _PyannoteAudio
        from pyannote.core import Segment as _Segment

        emb_model = Model.from_pretrained(
            'pyannote/wespeaker-voxceleb-resnet34-LM',
            use_auth_token=hf_token,
        )
        inference  = Inference(emb_model, window='whole')
        audio_io   = _PyannoteAudio(sample_rate=16000, mono='downmix')

        # Build reverse map: global_label → list of (start, end) raw audio spans
        label_spans: dict = {}
        for s_start, s_end, local_lbl in speaker_segments:
            glbl = remap[local_lbl]
            label_spans.setdefault(glbl, []).append((s_start, s_end))

        for glbl, spans in label_spans.items():
            embeddings = []
            for s_start, s_end in spans:
                if s_end - s_start < 0.5:          # skip sub-500ms slivers
                    continue
                try:
                    waveform, sr = audio_io.crop(str(audio_path), _Segment(s_start, s_end))
                    emb = inference({'waveform': waveform, 'sample_rate': sr})
                    emb = np.array(emb).flatten()
                    if emb.shape[0] == 256:          # sanity-check dimension
                        embeddings.append(emb)
                except Exception:
                    continue
            if embeddings:
                mean_emb = np.mean(embeddings, axis=0)
                norm = np.linalg.norm(mean_emb)
                if norm > 0:
                    speaker_embeddings[glbl] = mean_emb / norm  # L2-normalised
        logger.info(
            f'run_diarization_task: computed embeddings for '
            f'{len(speaker_embeddings)}/{len(global_labels)} speakers'
        )
    except Exception as emb_exc:
        logger.warning(
            f'run_diarization_task: embedding extraction skipped — {emb_exc}'
        )

    # ── Create / link SpeakerIdentity records ─────────────────────────────────
    from pgvector.django import CosineDistance

    def _apply_role_heuristic(si: SpeakerIdentity, lbl: str) -> None:
        """Set role on a freshly created (auto-named) SpeakerIdentity."""
        stats     = speaker_stats.get(lbl, {})
        total_dur = stats.get('total_duration', 0)
        seg_count = stats.get('seg_count', 1) or 1
        avg_dur   = total_dur / seg_count
        if total_dur < BACKGROUND_TOTAL or avg_dur < BACKGROUND_AVG_DURATION:
            si.role = SpeakerIdentity.ROLE_BACKGROUND
        elif avg_dur >= NARRATOR_AVG_DURATION:
            si.role = SpeakerIdentity.ROLE_NARRATOR
        # else: keep default ROLE_SPEAKER

    label_to_identity: dict = {}
    match_log: list = []

    for lbl in global_labels:
        embedding = speaker_embeddings.get(lbl)

        # ── Priority 1: embedding-based cross-video match ──────────────────────
        matched = None
        if embedding is not None:
            # cosine distance = 1 - cosine_similarity, so dist < (1-threshold) means match
            dist_threshold = 1.0 - SPEAKER_MATCH_THRESHOLD
            candidate = (
                SpeakerIdentity.objects
                .filter(speaker_embedding__isnull=False)
                .annotate(dist=CosineDistance('speaker_embedding', embedding.tolist()))
                .filter(dist__lt=dist_threshold)
                .order_by('dist')
                .first()
            )
            if candidate:
                matched = candidate
                sim = round(1.0 - float(candidate.dist), 3)
                match_log.append(f'{lbl} → "{candidate.name}" (sim={sim})')
                logger.info(
                    f'run_diarization_task: cross-video match {lbl} → '
                    f'"{candidate.name}" (cosine_sim={sim})'
                )
                # Update stored embedding: rolling mean keeps it current
                old_emb = np.array(candidate.speaker_embedding)
                new_emb = (old_emb + embedding) / 2.0
                norm    = np.linalg.norm(new_emb)
                if norm > 0:
                    candidate.speaker_embedding = (new_emb / norm).tolist()
                    candidate.save(update_fields=['speaker_embedding'])

        # ── Priority 2: same global label already linked (re-run safety) ───────
        if matched is None:
            existing_seg = (
                VideoSegment.objects
                .filter(speaker_label=lbl, speaker_identity__isnull=False)
                .exclude(video=video)
                .select_related('speaker_identity')
                .first()
            )
            if existing_seg:
                matched = existing_seg.speaker_identity
                match_log.append(f'{lbl} → "{matched.name}" (same label, re-run)')
                # Backfill embedding if we have one and it's missing
                if embedding is not None and matched.speaker_embedding is None:
                    matched.speaker_embedding = embedding.tolist()
                    matched.save(update_fields=['speaker_embedding'])

        if matched is not None:
            label_to_identity[lbl] = matched
            continue

        # ── Priority 3: create new SpeakerIdentity ─────────────────────────────
        m = _re.search(r'(\d+)$', lbl)
        num_str = f'Speaker {int(m.group(1)) + 1}' if m else lbl
        si = SpeakerIdentity(
            name=num_str,
            is_auto_named=True,
            role=SpeakerIdentity.ROLE_SPEAKER,
        )
        _apply_role_heuristic(si, lbl)
        if embedding is not None:
            si.speaker_embedding = embedding.tolist()
        si.save()
        match_log.append(f'{lbl} → NEW "{si.name}" (id={si.pk})')
        label_to_identity[lbl] = si

    logger.info(f'run_diarization_task: identity resolution: {match_log}')

    # ── Attach speaker_identity FK to each segment ────────────────────────────
    for seg in segments:
        if seg.speaker_label and seg.speaker_label in label_to_identity:
            seg.speaker_identity = label_to_identity[seg.speaker_label]
    VideoSegment.objects.bulk_update(segments, ['speaker_identity'])
    logger.info(
        f'run_diarization_task: linked {len(label_to_identity)} '
        f'SpeakerIdentity records for {video_id}'
    )

    suggestions_created = 0
    try:
        suggestions_created = _upsert_speaker_face_suggestions(video, segments)
        if suggestions_created:
            logger.info(
                f'run_diarization_task: upserted {suggestions_created} '
                f'speaker-face suggestion(s) for {video_id}'
            )
    except Exception as sugg_exc:
        logger.warning(f'run_diarization_task: suggestion generation skipped — {sugg_exc}')

    # ── Kick off non-speech audio event detection (applause/laughter/music…) ──
    # Runs on its own queue so heavy PANNs inference doesn't block the caller.
    # Guarded so any failure here never breaks the diarization result payload.
    if getattr(settings, 'AUDIO_EVENTS_ENABLED', True):
        try:
            queue = getattr(settings, 'AUDIO_EVENTS_QUEUE', 'audio')
            detect_audio_events_task.apply_async(args=[str(video.id)], queue=queue)
            logger.info(
                f'run_diarization_task: queued detect_audio_events_task '
                f'for {video.id} on queue "{queue}"'
            )
        except Exception as ae_exc:
            logger.warning(
                f'run_diarization_task: could not enqueue audio-event task '
                f'for {video.id} — {ae_exc}'
            )

    return {
        'ok':               True,
        'segments_updated': updated,
        'speakers':         len(local_speakers),
        'labels':           list(remap.values()),
        'suggestions':      suggestions_created,
        'identities': [
            {
                'id':      si.pk,
                'name':    si.name,
                'role':    si.role,
                'matched': any(lbl in m for m in match_log if '→ NEW' not in m),
            }
            for lbl, si in label_to_identity.items()
        ],
    }


# ── Audio event detection (non-speech) ───────────────────────────────────────
# Detects applause, laughter, music, cheering, crowd noise, booing, generic
# "speech" windows via PANNs CNN14 (AudioSet tagger) and silent spans via
# FFmpeg's built-in `silencedetect` filter.
#
# Stored in the `AudioEvent` table.  Powers the third lane on the video X-ray
# page alongside Crowd / Overlap.  Runs on a dedicated `audio` Celery queue so
# it doesn't block diarization or caption jobs (see start.sh).

# AudioSet class name → our short label.  Keys are exact PANNs / AudioSet
# English names; anything not in this map is ignored.
_PANNS_CLASS_MAP = {
    'Applause':                           'applause',
    'Laughter':                           'laughter',
    'Giggle':                             'laughter',
    'Chuckle, chortle':                   'laughter',
    'Cheering':                           'cheering',
    'Crowd':                              'crowd',
    'Hubbub, speech noise, speech babble':'crowd',
    'Booing':                             'booing',
    'Music':                              'music',
    'Speech':                             'speech',
}


def _detect_silence_spans_ffmpeg(wav_path: str, silence_db: float,
                                 min_silence_sec: float) -> list[tuple[float, float]]:
    """
    Run `ffmpeg -af silencedetect` on the WAV and parse stderr for
    `silence_start` / `silence_end` lines.  Returns a list of (start, end)
    tuples in seconds.  Non-fatal on failure: returns [] and logs a warning.
    """
    import re

    cmd = [
        settings.FFMPEG_PATH or 'ffmpeg',
        '-hide_banner', '-nostats',
        '-i', wav_path,
        '-af', f'silencedetect=noise={silence_db}dB:d={min_silence_sec}',
        '-f', 'null', '-',
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as exc:
        logger.warning(f'_detect_silence_spans_ffmpeg: subprocess failed — {exc}')
        return []

    stderr = proc.stderr or ''
    starts = [float(m.group(1)) for m in re.finditer(r'silence_start:\s*(-?\d+\.?\d*)', stderr)]
    ends   = [float(m.group(1)) for m in re.finditer(r'silence_end:\s*(-?\d+\.?\d*)',   stderr)]

    spans: list[tuple[float, float]] = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        if e is not None and e > s:
            spans.append((max(0.0, s), e))
    return spans


def _extract_audio_wav_at_rate(source_path: str, out_path: str, sample_rate: int) -> bool:
    """
    Mono WAV extractor with configurable sample rate.  PANNs needs 32 kHz;
    Whisper needs 16 kHz.  We keep them separate so neither pipeline
    accidentally resamples the other's file.
    """
    cmd = [
        settings.FFMPEG_PATH or 'ffmpeg',
        '-i', str(source_path),
        '-vn',
        '-acodec', 'pcm_s16le',
        '-ar', str(sample_rate),
        '-ac', '1',
        '-y',
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    return result.returncode == 0


def _ensure_panns_assets() -> dict[str, str]:
    """
    Make panns_inference assets available without relying on shell `wget`.

    The upstream package downloads both class labels and model checkpoint via
    `os.system('wget ...')`, which fails on systems where wget is not installed
    (common on macOS). We pre-download those files with Python stdlib so
    `import panns_inference` and `AudioTagging(...)` can proceed normally.
    """
    import shutil
    import time
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    def _download_with_retries(url: str, out_path: Path, *, min_size: int,
                               attempts: int = 4, timeout_sec: int = 90) -> None:
        """
        Download a file with retry/backoff and atomic replace.
        Raises RuntimeError if all attempts fail or size is too small.
        """
        out_path.parent.mkdir(parents=True, exist_ok=True)
        last_err = None
        tmp_path = out_path.with_suffix(out_path.suffix + '.part')
        for i in range(1, attempts + 1):
            try:
                req = Request(url, headers={'User-Agent': 'ClipLens/1.0'})
                with urlopen(req, timeout=timeout_sec) as r, open(tmp_path, 'wb') as f:
                    shutil.copyfileobj(r, f, length=1024 * 1024)
                size = tmp_path.stat().st_size if tmp_path.exists() else 0
                if size < min_size:
                    raise RuntimeError(
                        f'download too small ({size} bytes, expected >= {min_size})'
                    )
                tmp_path.replace(out_path)
                return
            except (HTTPError, URLError, TimeoutError, RuntimeError, OSError) as exc:
                last_err = exc
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except Exception:
                    pass
                if i < attempts:
                    sleep_s = min(30, 2 ** i)
                    logger.warning(
                        f'_ensure_panns_assets: download retry {i}/{attempts} failed for '
                        f'{out_path.name}: {exc} (sleep {sleep_s}s)'
                    )
                    time.sleep(sleep_s)
        raise RuntimeError(f'failed downloading {out_path.name}: {last_err}')

    # Use MEDIA_ROOT as HOME for panns_inference so its internal hardcoded
    # `Path.home()/panns_data/...` paths stay writable across environments
    # where the process home directory may be read-only.
    media_home = str(Path(settings.MEDIA_ROOT).resolve())
    os.environ['HOME'] = media_home

    data_dir = Path(media_home) / 'panns_data'
    data_dir.mkdir(parents=True, exist_ok=True)

    labels_path = data_dir / 'class_labels_indices.csv'
    ckpt_path = data_dir / 'Cnn14_mAP=0.431.pth'

    if not labels_path.exists() or labels_path.stat().st_size < 10_000:
        labels_url = (
            'https://storage.googleapis.com/us_audioset/'
            'youtube_corpus/v1/csv/class_labels_indices.csv'
        )
        logger.info(f'_ensure_panns_assets: downloading labels to {labels_path}')
        _download_with_retries(labels_url, labels_path, min_size=10_000)

    # Upstream checks for ~300 MB minimum; mirror that so partial files are repaired.
    if not ckpt_path.exists() or ckpt_path.stat().st_size < 300_000_000:
        ckpt_url = 'https://zenodo.org/record/3987831/files/Cnn14_mAP%3D0.431.pth?download=1'
        logger.info(f'_ensure_panns_assets: downloading checkpoint to {ckpt_path}')
        _download_with_retries(ckpt_url, ckpt_path, min_size=300_000_000)

    return {'labels_path': str(labels_path), 'checkpoint_path': str(ckpt_path)}


def _collapse_same_label_windows(windows: list[tuple[float, float, str, float]],
                                 merge_gap: float) -> list[tuple[float, float, str, float]]:
    """
    Merge consecutive windows that share the same label.  `windows` is a
    list of (start, end, label, confidence) sorted by start.  Gaps ≤ `merge_gap`
    seconds between same-label windows are bridged; confidence is the mean of
    the merged windows.
    """
    if not windows:
        return []
    merged: list[list] = []
    for start, end, label, conf in windows:
        if merged and merged[-1][2] == label and start - merged[-1][1] <= merge_gap:
            prev = merged[-1]
            prev[1] = max(prev[1], end)
            prev[3] = (prev[3] * prev[4] + conf) / (prev[4] + 1)
            prev[4] += 1
        else:
            merged.append([start, end, label, conf, 1])
    return [(s, e, lbl, c) for s, e, lbl, c, _n in merged]


@shared_task(
    bind=True,
    name='videos.tasks.detect_audio_events_task',
    queue='audio',
    max_retries=3,
    default_retry_delay=120,
    acks_late=True,
)
def detect_audio_events_task(self, video_id: str):
    """
    Detect non-speech audio events and silence spans for a video.

    Pipeline:
        1. Skip if feature disabled, no video, no audio stream.
        2. Wipe any existing AudioEvent rows for this video (idempotent re-runs).
        3. Extract a 32 kHz mono WAV to temp.
        4. FFmpeg silencedetect → 'silence' spans.
        5. PANNs CNN14 sliding-window tagging → applause/laughter/music/etc.
        6. Bulk-create AudioEvent rows.
        7. Delete temp WAV.

    Never raises on library/weight errors — logs and returns an error dict so
    diarization results stay intact.  Re-queues once on transient exceptions.
    """
    import tempfile
    from .models import Video, AudioEvent

    if not getattr(settings, 'AUDIO_EVENTS_ENABLED', True):
        logger.info(f'detect_audio_events_task: AUDIO_EVENTS_ENABLED=False; skipping {video_id}')
        return {'ok': False, 'skipped': 'disabled'}

    try:
        video = Video.objects.get(id=video_id)
    except Video.DoesNotExist:
        logger.warning(f'detect_audio_events_task: video {video_id} not found')
        return {'ok': False, 'error': 'video-not-found'}

    source_path = Path(settings.MEDIA_ROOT) / video.original_file.name
    if not video.original_file or not video.original_file.name or not source_path.exists():
        logger.warning(f'detect_audio_events_task: source file missing for {video_id}')
        return {'ok': False, 'error': 'source-missing'}

    if not _has_audio_stream(str(source_path)):
        logger.info(f'detect_audio_events_task: no audio stream in {video_id}; skipping')
        return {'ok': False, 'skipped': 'no-audio'}

    min_conf        = float(getattr(settings, 'AUDIO_EVENTS_MIN_CONFIDENCE',   0.30))
    min_dur         = float(getattr(settings, 'AUDIO_EVENTS_MIN_DURATION_SEC', 0.50))
    window_sec      = float(getattr(settings, 'AUDIO_EVENTS_WINDOW_SEC',       1.0))
    hop_sec         = float(getattr(settings, 'AUDIO_EVENTS_HOP_SEC',          0.5))
    silence_db      = float(getattr(settings, 'AUDIO_EVENTS_SILENCE_DB',       -30))
    min_silence_sec = float(getattr(settings, 'AUDIO_EVENTS_SILENCE_MIN_SEC',  1.0))
    device          = str(getattr(settings,   'AUDIO_EVENTS_DEVICE',           'cpu'))
    # Music is noisier than speech in AudioSet on real-world dialog videos.
    # Keep a stricter floor so incidental background tones don't flood the lane.
    music_min_conf  = max(min_conf + 0.12, 0.34)

    logger.info(f'detect_audio_events_task: starting for {video_id}')

    tmp_wav = None
    try:
        tmp_wav = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        tmp_wav.close()

        # ── Step 3: extract 32 kHz mono WAV ───────────────────────────────────
        # 32 kHz is PANNs CNN14's native sample rate.  Keeping a dedicated
        # extraction here avoids disturbing the 16 kHz WAV used by Whisper
        # and pyannote in `run_diarization_task`.
        if not _extract_audio_wav_at_rate(str(source_path), tmp_wav.name, sample_rate=32000):
            logger.error(f'detect_audio_events_task: ffmpeg 32 kHz extraction failed for {video_id}')
            return {'ok': False, 'error': 'ffmpeg-failed'}

        # ── Step 4: silence spans (FFmpeg, deterministic, no ML) ──────────────
        silence_spans = _detect_silence_spans_ffmpeg(tmp_wav.name, silence_db, min_silence_sec)
        logger.info(f'detect_audio_events_task: {len(silence_spans)} silence span(s) for {video_id}')

        # ── Step 5: PANNs tagging for non-speech events ───────────────────────
        panns_spans: list[tuple[float, float, str, float]] = []
        try:
            assets = _ensure_panns_assets()
            import numpy as np
            import soundfile as sf
            from panns_inference import AudioTagging, labels as panns_labels  # first run downloads ~150 MB
        except ImportError as imp_exc:
            logger.warning(
                f'detect_audio_events_task: panns_inference unavailable — '
                f'silence-only mode for {video_id}: {imp_exc}'
            )
            panns_labels = None
        except Exception as assets_exc:
            # Asset download failures are usually transient network issues (e.g. 503).
            # Raise so the task-level retry policy can recover automatically.
            raise RuntimeError(f'PANNs asset bootstrap failed: {assets_exc}') from assets_exc
        else:
            try:
                waveform, sr = sf.read(tmp_wav.name, dtype='float32', always_2d=False)
                if waveform.ndim > 1:
                    waveform = waveform.mean(axis=1)

                # PANNs expects 32 kHz float32 mono.  We extracted at 32 kHz, but
                # guard against odd inputs just in case.
                if sr != 32000:
                    logger.warning(
                        f'detect_audio_events_task: expected 32kHz WAV, got {sr}Hz — '
                        f'skipping PANNs pass for {video_id}'
                    )
                else:
                    win_samples = max(1, int(window_sec * sr))
                    hop_samples = max(1, int(hop_sec    * sr))
                    total       = len(waveform)

                    # Build the PANNs class → our-label lookup once.  PANNs ships
                    # `labels` as a list of 527 strings aligned with the output.
                    class_to_label: dict[int, str] = {}
                    for i, cls_name in enumerate(panns_labels):
                        if cls_name in _PANNS_CLASS_MAP:
                            class_to_label[i] = _PANNS_CLASS_MAP[cls_name]
                    if not class_to_label:
                        logger.warning(
                            f'detect_audio_events_task: no PANNs classes matched our label map '
                            f'— PANNs version changed? Skipping model for {video_id}'
                        )
                    else:
                        tagger = AudioTagging(
                            checkpoint_path=assets['checkpoint_path'],
                            device=device,
                        )

                        raw_windows: list[tuple[float, float, str, float]] = []
                        pos = 0
                        # Batch windows to keep RAM under ~1 GB on CPU.
                        BATCH = 32
                        batch_chunks: list[np.ndarray] = []
                        batch_times: list[tuple[float, float]] = []

                        def _flush(batch_chunks, batch_times):
                            if not batch_chunks:
                                return
                            batch = np.stack(batch_chunks, axis=0)
                            clipwise, _ = tagger.inference(batch)  # (N, 527)
                            for row_i, probs in enumerate(clipwise):
                                # Multi-label mode: keep every mapped label above
                                # threshold, not just top-1. This improves recall
                                # for short transition music and mixed scenes.
                                # If multiple AudioSet classes map to same label
                                # (e.g. Giggle + Laughter), keep the best prob.
                                per_label_best: dict[str, float] = {}
                                for cls_i, lbl in class_to_label.items():
                                    p = float(probs[cls_i])
                                    threshold = music_min_conf if lbl == 'music' else min_conf
                                    if p < threshold:
                                        continue
                                    prev = per_label_best.get(lbl)
                                    if prev is None or p > prev:
                                        per_label_best[lbl] = p
                                # Prevent speech/music co-tagging in the same window.
                                # Keep whichever class is stronger for this window.
                                if 'speech' in per_label_best and 'music' in per_label_best:
                                    if per_label_best['speech'] >= per_label_best['music']:
                                        per_label_best.pop('music', None)
                                    else:
                                        per_label_best.pop('speech', None)
                                if per_label_best:
                                    s, e = batch_times[row_i]
                                    for lbl, p in per_label_best.items():
                                        raw_windows.append((s, e, lbl, p))

                        while pos < total:
                            end = pos + win_samples
                            chunk = waveform[pos:end]
                            if len(chunk) < win_samples:
                                chunk = np.pad(chunk, (0, win_samples - len(chunk)), mode='constant')
                            batch_chunks.append(chunk)
                            batch_times.append((pos / sr, min(end, total) / sr))
                            if len(batch_chunks) >= BATCH:
                                _flush(batch_chunks, batch_times)
                                batch_chunks, batch_times = [], []
                            pos += hop_samples
                        _flush(batch_chunks, batch_times)

                        # Merge neighbouring same-label windows (≤ 1 hop gap).
                        panns_spans = _collapse_same_label_windows(
                            sorted(raw_windows, key=lambda w: w[0]),
                            merge_gap=hop_sec * 1.5,
                        )
                        panns_spans = [s for s in panns_spans if (s[1] - s[0]) >= min_dur]
                        logger.info(
                            f'detect_audio_events_task: PANNs produced {len(panns_spans)} span(s) '
                            f'for {video_id}'
                        )
            except Exception as panns_exc:
                logger.error(
                    f'detect_audio_events_task: PANNs inference failed for {video_id} — '
                    f'{panns_exc}', exc_info=True,
                )
                panns_spans = []

        # ── Step 6: wipe old events and bulk-insert new ones ──────────────────
        AudioEvent.objects.filter(video=video).delete()

        to_create: list[AudioEvent] = []
        for s, e in silence_spans:
            if (e - s) < min_dur:
                continue
            to_create.append(AudioEvent(
                video=video,
                start_seconds=float(s),
                end_seconds=float(e),
                label=AudioEvent.LABEL_SILENCE,
                confidence=0.0,
                source=AudioEvent.SOURCE_FFMPEG,
            ))
        for s, e, lbl, conf in panns_spans:
            to_create.append(AudioEvent(
                video=video,
                start_seconds=float(s),
                end_seconds=float(e),
                label=lbl,
                confidence=float(conf),
                source=AudioEvent.SOURCE_PANNS,
            ))

        if to_create:
            AudioEvent.objects.bulk_create(to_create, batch_size=500)

        counts: dict[str, int] = {}
        for ev in to_create:
            counts[ev.label] = counts.get(ev.label, 0) + 1
        logger.info(
            f'detect_audio_events_task: stored {len(to_create)} event(s) for {video_id} — {counts}'
        )
        return {'ok': True, 'total': len(to_create), 'by_label': counts}

    except Exception as exc:
        logger.error(f'detect_audio_events_task failed for {video_id}: {exc}', exc_info=True)
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            return {'ok': False, 'error': str(exc)[:300]}
    finally:
        if tmp_wav and os.path.exists(tmp_wav.name):
            try:
                os.unlink(tmp_wav.name)
            except Exception:
                pass


# ─── Live Stream Post-Processing ──────────────────────────────────────────────

@shared_task(bind=True, name='videos.tasks.run_live_ffmpeg',
             queue='live', time_limit=10800)   # 3hr hard cap; task blocks for stream duration
def run_live_ffmpeg(self, live_stream_id, stream_key):
    """
    Runs FFmpeg for the duration of a live stream.
    Reads RTMP from mediamtx, outputs HLS segments for viewers + records MP4.
    This task BLOCKS until the stream ends (OBS disconnects → FFmpeg exits naturally).
    Runs in the dedicated 'live' Celery queue so it never starves other workers.
    """
    import os
    import subprocess
    from django.conf import settings
    from .models import LiveStream

    try:
        live = LiveStream.objects.get(id=live_stream_id)
    except LiveStream.DoesNotExist:
        logger.error(f'[live-ffmpeg] LiveStream {live_stream_id} not found')
        return

    # Each stream uses its own directory (live_stream_id) — never collides with past streams
    media_dir   = os.path.join(settings.LIVE_MEDIA_ROOT, str(live_stream_id))
    hls_path    = os.path.join(media_dir, 'live.m3u8')
    seg_pattern = os.path.join(media_dir, 'seg%03d.ts')
    rec_path    = os.path.join(media_dir, 'recording.mp4')
    rtmp_url    = f'rtmp://127.0.0.1:1935/live/{stream_key}'

    os.makedirs(media_dir, exist_ok=True)

    cmd = [
        'ffmpeg', '-y',                   # overwrite outputs without prompting
        '-loglevel', 'warning',           # global — must come before -i
        '-fflags', '+genpts',                 # regenerate PTS from DTS for reordered RTMP frames
        '-i', rtmp_url,
        # ── HLS output — live viewer stream ──────────────────────────────
        '-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'zerolatency',
        '-g', '48', '-keyint_min', '48',  # keyframe every 2s (matches segment duration)
        '-sc_threshold', '0',             # no scene-cut keyframes — keeps segments clean
        '-c:a', 'aac', '-b:a', '128k', '-ar', '44100',
        '-f', 'hls',
        '-hls_time', '2',                 # 2-second segments — lower live latency
        '-hls_list_size', '5',            # keep last 5 segments (~10s) in playlist
        '-hls_flags', 'delete_segments+append_list+independent_segments',
        '-hls_segment_filename', seg_pattern,
        hls_path,
        # ── MP4 recording output ──────────────────────────────────────────
        # Re-encode (not stream-copy) so timestamps are reset to 0 and the
        # duration in the moov atom matches actual content length.
        # RTMP timestamps reflect OBS uptime, not stream duration — stream-copy
        # would preserve those raw timestamps, causing duration mismatches.
        '-c:v', 'libx264', '-preset', 'veryfast',
        '-c:a', 'aac', '-b:a', '128k',
        '-movflags', '+faststart',
        rec_path,
    ]

    logger.info(f'[live-ffmpeg] Starting FFmpeg for stream_key={stream_key}')
    proc = subprocess.Popen(cmd)
    proc.wait()   # blocks here until OBS disconnects → FFmpeg exits naturally

    logger.info(f'[live-ffmpeg] FFmpeg finished for stream_key={stream_key} (exit={proc.returncode})')

    # FFmpeg has fully exited — MP4 is flushed and closed (including +faststart rewrite).
    # NOW safe to mark stream as processing and queue the recording task.
    # This is the authoritative status transition: live → processing.
    # stream_on_unpublish does NOT change status to avoid false "ended" on brief hiccups.
    from django.utils import timezone as _tz
    LiveStream.objects.filter(id=live_stream_id, status=LiveStream.STATUS_LIVE).update(
        status=LiveStream.STATUS_PROCESSING,
        ended_at=_tz.now(),
    )
    process_livestream_recording.delay(live_stream_id)


@shared_task(bind=True, name='videos.tasks.process_livestream_recording',
             queue='processing', max_retries=1, soft_time_limit=7200, time_limit=7500)
def process_livestream_recording(self, live_stream_id):
    """
    Called after a live stream ends.
    Creates a Video record from the recording and runs the full AI pipeline.
    """
    import os
    from django.conf import settings
    from django.utils import timezone
    from .models import LiveStream, Video

    logger.info(f'[livestream] Starting post-processing for LiveStream id={live_stream_id}')

    try:
        live = LiveStream.objects.select_related('channel', 'stream_key').get(id=live_stream_id)
    except LiveStream.DoesNotExist:
        logger.error(f'[livestream] LiveStream {live_stream_id} not found')
        return

    recording_abs = os.path.join(settings.MEDIA_ROOT, live.recording_path)

    # Wait up to 30s for FFmpeg to finish writing the recording
    import time
    for _ in range(6):
        if os.path.exists(recording_abs) and os.path.getsize(recording_abs) > 0:
            break
        time.sleep(5)
    else:
        logger.error(f'[livestream] Recording file not found: {recording_abs}')
        live.status = LiveStream.STATUS_ENDED
        live.save(update_fields=['status'])
        return

    # Create the Video record
    channel = live.channel
    title = live.title or f'Live Stream — {live.started_at.strftime("%Y-%m-%d %H:%M")}'

    file_size = os.path.getsize(recording_abs)
    recording_filename = os.path.basename(recording_abs)

    video = Video(
        channel=channel,
        title=title,
        status=Video.STATUS_PENDING,
        original_filename=recording_filename,
        file_size=file_size,
        uploaded_by=channel.owner.username if channel.owner else 'livestream',
    )
    # Set FileField name directly — path relative to MEDIA_ROOT, no file copy needed
    video.original_file.name = live.recording_path
    video.save()

    live.video = video
    live.status = LiveStream.STATUS_READY
    live.save(update_fields=['video', 'status'])

    auto_process = getattr(settings, 'LIVE_STREAM_AUTO_PROCESS', True)

    if auto_process:
        logger.info(f'[livestream] Auto-processing ON — queuing full pipeline for Video id={video.id}')
        process_video_task.delay(str(video.id), skip_ai=False)
    else:
        # HLS encoding + thumbnail always run so the video is watchable.
        # AI (Whisper, YOLO, CLIP, faces, speakers) is skipped — editor triggers manually.
        logger.info(f'[livestream] Auto-processing OFF — queuing HLS+thumbnail only for Video id={video.id}')
        process_video_task.delay(str(video.id), skip_ai=True)
