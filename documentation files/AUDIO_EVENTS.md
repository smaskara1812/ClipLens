# Audio Events on X-ray Timeline

This document describes the non-speech audio event feature that is now implemented in ClipStream, how it works end-to-end, and the planned future adaptations.

---

## 1. What is implemented now

### 1.1 User-visible behavior

- Audio events are shown on the **Video X-ray page** as a dedicated **third timeline lane** below Crowd and Overlap.
- Event classes currently supported:
  - `silence`
  - `speech`
  - `music`
  - `applause`
  - `laughter`
  - `cheering`
  - `crowd`
  - `booing`
- Segments are color-coded, clickable, and seek the video to that time range.
- A compact legend with per-label counts is shown under the audio lane.
- If no events exist yet, X-ray shows a non-ambiguous hint: run diarization to
  trigger detection, while noting some videos may still produce zero events.

### 1.2 Trigger behavior (important)

- Audio event detection is intentionally **coupled to diarization**.
- It is triggered as a follow-up from `run_diarization_task`.
- No standalone frontend trigger was added.
- Re-running diarization for a video rebuilds that video's audio events.

### 1.3 Scope guarantees

- No changes were made to the watch player timeline.
- No changes were made to transcript editor behavior.
- No changes were made outside X-ray UI for this feature.

---

## 2. Architecture and flow

```text
Run diarization command
      |
      v
videos.tasks.run_diarization_task  (queue: captions)
      |
      +--> queues detect_audio_events_task (queue: audio)
                    |
                    +--> FFmpeg silencedetect  -> silence spans
                    +--> PANNs CNN14 tagging   -> non-speech event spans
                    |
                    +--> save videos.AudioEvent rows
                              |
                              v
                    videos.views._build_video_xray_context
                              |
                              v
                    videos/templates/videos/video_xray.html
                    (third "Audio" lane + legend)
```

### 2.1 Why this queue split exists

- Diarization and audio tagging are heavy jobs.
- Audio tagging runs on a dedicated `audio` queue so it does not block normal captions/processing tasks.
- `start.sh` starts two Celery workers:
  - `main` worker for `processing,captions,default`
  - `audio` worker for `${AUDIO_EVENTS_QUEUE:-audio}`

---

## 3. Data model

### 3.1 New model: `AudioEvent`

File: `videos/models.py`  
Migration: `videos/migrations/0045_audioevent.py`

Fields:

- `video` FK to `Video`
- `start_seconds`, `end_seconds`
- `label` (choices listed above)
- `confidence` (PANNs confidence; silence from FFmpeg uses `0.0`)
- `source` (`panns` or `ffmpeg`)
- `created_at`

Indexes:

- `(video, start_seconds)`
- `(video, label)`

---

## 4. Detection pipeline details

### 4.1 Silence detection

- Uses FFmpeg filter: `silencedetect`
- Controlled by:
  - `AUDIO_EVENTS_SILENCE_DB` (default `-30`)
  - `AUDIO_EVENTS_SILENCE_MIN_SEC` (default `1.0`)

### 4.2 Non-speech event detection

- Uses `panns-inference` (CNN14 / AudioSet classes).
- Temp audio extraction is done at **32 kHz mono WAV** for PANNs.
- Sliding-window inference:
  - window: `AUDIO_EVENTS_WINDOW_SEC` (default `1.0`)
  - hop: `AUDIO_EVENTS_HOP_SEC` (default `0.5`)
- Events below `AUDIO_EVENTS_MIN_CONFIDENCE` are dropped.
- Very short spans below `AUDIO_EVENTS_MIN_DURATION_SEC` are dropped.
- Neighbor windows of the same label are merged into timeline spans.

### 4.3 Persistence strategy

- Before insert, existing `AudioEvent` rows for the video are deleted.
- New rows are bulk-created.
- This makes reruns deterministic and avoids duplicate overlays.

---

## 5. Configuration reference

Environment variables (in `.env`, mirrored in `.env.example`, consumed in `cliplens/settings.py`):

```env
AUDIO_EVENTS_ENABLED=true
AUDIO_EVENTS_QUEUE=audio
AUDIO_EVENTS_MIN_CONFIDENCE=0.22
AUDIO_EVENTS_MIN_DURATION_SEC=0.35
AUDIO_EVENTS_WINDOW_SEC=1.0
AUDIO_EVENTS_HOP_SEC=0.5
AUDIO_EVENTS_SILENCE_DB=-30
AUDIO_EVENTS_SILENCE_MIN_SEC=1.0
AUDIO_EVENTS_DEVICE=cpu
```

### 5.1 CPU/GPU notes

- Default is CPU (`AUDIO_EVENTS_DEVICE=cpu`).
- On CUDA-capable machines, set `AUDIO_EVENTS_DEVICE=cuda`.
- CUDA mode requires compatible NVIDIA driver + CUDA-enabled PyTorch stack.
- If CUDA is unavailable, keep CPU.

---

## 6. Dependencies

Added in `requirements.txt`:

- `panns-inference==0.1.1`
- `soundfile==0.12.1`
- `librosa==0.10.2.post1`

Model weights are downloaded on first run (lazy download), as intended.

---

## 7. Operational notes and troubleshooting

### 7.1 Common checks

1. Ensure venv is active before running Django/Celery commands.
2. Run migrations:
   - `python manage.py migrate`
3. Start workers via `./start.sh` and confirm both `main` + `audio` workers are up.
4. Re-run diarization for a video.
5. Open X-ray and verify Audio lane appears.

### 7.2 If lane remains empty

- Confirm `AUDIO_EVENTS_ENABLED=true`.
- Confirm `audio` worker is running and consuming the `audio` queue.
- Confirm dependencies installed (`pip install -r requirements.txt`).
- Check logs for `detect_audio_events_task` failures.

---

## 8. Future adaptations (planned)

These are intentionally deferred to later phases.

### 8.1 Audio semantics expansion

- Emotion tags (e.g., excited/calm/tense) on speech windows.
- Loudness-derived highlights (peaks, spikes, emphasis windows).
- Optional genre-style background classification (speech-only / speech+music / ambience).

### 8.2 Better event quality controls

- Per-label confidence thresholds from admin settings.
- Label enable/disable toggles per deployment.
- Channel-specific tuning profiles (studio podcast vs live crowd recordings).

### 8.3 UX improvements on X-ray

- Label filters (show only applause/music/etc).
- Hover cards with confidence + source metadata.
- Quick jump controls by event type (next applause, next silence).

### 8.4 Performance and infrastructure

- Dedicated GPU worker profile for audio queue.
- Adaptive batch sizing based on available RAM/VRAM.
- Optional caching of extracted 32 kHz WAV for repeated reruns.

### 8.5 Product-level extensions (optional)

- Export event markers as JSON/VTT sidecar.
- Feed events into auto-clipping heuristics.
- Use audio events as search facets in global discovery later (if desired).

---

## 9. Current status summary

- Implemented and active.
- Triggered via diarization workflow only.
- Rendered only in X-ray as requested.
- Architected for future expansion without breaking existing player/transcript surfaces.
