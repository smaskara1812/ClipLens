---

## name: ClipLens — Complete Project Overview
description: Full technical overview — what it is, architecture, all AI models, workers, tasks, database models, key features
type: reference
originSessionId: 743862a7-a0d3-4c05-82b0-19a5235487a1

## What is ClipLens?

ClipLens is a **self-hosted video and photo asset management platform** built with Django. Think of it as a private YouTube + Google Photos combined, with deep AI analysis baked in.

**Primary use case**: Internal media library for organisations — upload videos and photos, have them automatically transcribed, tagged, face-recognised, scene-described, and made fully searchable. Also supports live streaming via OBS.

**Key principle**: Everything runs on your own server. No third-party APIs (except HuggingFace model downloads). All AI inference is local.

---

## Tech Stack


| Layer          | Technology                                                   |
| -------------- | ------------------------------------------------------------ |
| Web framework  | Django 4.2                                                   |
| Database       | PostgreSQL (with `pgvector` + `pg_trgm` extensions)          |
| Cache / broker | Redis                                                        |
| Task queue     | Celery 5.6.3                                                 |
| Web server     | Gunicorn (behind Nginx)                                      |
| Frontend       | Server-rendered Django templates + vanilla JS (no React/Vue) |
| Live streaming | mediamtx v1.9.3 (RTMP) + FFmpeg (HLS segments)               |
| AI inference   | All local — see AI Models section                            |


---

## Project Structure

```
/var/www/cliplens/
├── cliplens/           ← Django project config (settings.py, urls.py, celery.py)
├── videos/             ← Single Django app — ALL models, views, tasks, templates
│   ├── models.py       ← All database models (~1600 lines)
│   ├── views.py        ← All views and API endpoints (~8600 lines)
│   ├── tasks.py        ← All Celery background tasks (~3400 lines)
│   ├── services.py     ← Video processing service (FFmpeg HLS encoding)
│   ├── urls.py         ← URL routing
│   ├── middleware.py   ← LoginRequired + HLS headers middleware
│   └── templates/      ← All HTML templates
├── media/              ← Uploaded files, HLS segments, thumbnails, face crops
├── staticfiles/        ← Collected static files (CSS, JS, icons)
├── venv/               ← Python virtual environment
├── mediamtx.yml        ← RTMP server configuration
└── .env                ← Environment variables (NOT in git)
```

---

## Database Models (videos/models.py)

### Users & Channels


| Model                 | Purpose                                                                     |
| --------------------- | --------------------------------------------------------------------------- |
| `UserProfile`         | Extends Django User — adds `role` (superadmin / editor / viewer)            |
| `Channel`             | A content channel. Has owner (User FK), editors (M2M), avatar, banner, slug |
| `ChannelLink`         | Social/external links shown on channel page                                 |
| `ChannelSubscription` | User subscribing to a channel                                               |


### Content


| Model        | Purpose                                                                                                                                                   |
| ------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Video`      | Core model. Stores file path, HLS path, duration, resolution, status, visibility, thumbnail, tags, description, `ai_summary` (Ollama), soft-delete fields |
| `Photo`      | Photo DAM. Similar to Video — file, thumbnail, CLIP embedding, face data, EXIF, GPS                                                                       |
| `Category`   | Video/photo categories with slug                                                                                                                          |
| `NamedPlace` | Curated real-world locations. Videos/Photos can be tagged to a place via FK + lat/lon                                                                     |


### Video Intelligence


| Model          | Purpose                                                                                                                          |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `VideoFrame`   | One row per sampled frame. Stores `labels` (YOLO), `description` (BLIP/Florence-2), `clip_embedding` (512-dim vector), timestamp |
| `VideoSegment` | One row per transcript segment (Whisper). Stores `text`, `start_seconds`, `end_seconds`, `speaker_label`                         |
| `VideoChapter` | Named chapters with timestamps — auto-detected or manual                                                                         |
| `VideoMoment`  | Highlighted moments in a video (manual or auto)                                                                                  |
| `EndScreen`    | End-screen cards shown in the player                                                                                             |
| `AudioEvent`   | Non-speech audio events (applause, music, laughter etc.) detected by PANNs                                                       |
| `AudioTrack`   | Alternative audio tracks (e.g. dubbed versions)                                                                                  |


### People


| Model                   | Purpose                                                                                                      |
| ----------------------- | ------------------------------------------------------------------------------------------------------------ |
| `FaceIdentity`          | One person. Has `name`, `embedding` (512-dim), confirmed/unconfirmed status                                  |
| `DetectedFace`          | One face crop from a video frame or photo. FK to `FaceIdentity`, stores `crop_path`, `embedding`, confidence |
| `FaceIdentityNickname`  | Alternative names for a FaceIdentity                                                                         |
| `SpeakerIdentity`       | One speaker (voice). Has `name`, `embedding` (wespeaker)                                                     |
| `SpeakerFaceSuggestion` | AI-suggested face↔speaker matches for human confirmation                                                     |


### Subtitles & Translation


| Model      | Purpose                                                               |
| ---------- | --------------------------------------------------------------------- |
| `Subtitle` | One subtitle track per video per language. Stores VTT content as text |


### Organisation


| Model                     | Purpose                                             |
| ------------------------- | --------------------------------------------------- |
| `Playlist`                | Ordered list of videos. Can be public or private    |
| `PlaylistItem`            | Video in a playlist with ordering                   |
| `Album`                   | Photo album (manual or smart/filtered)              |
| `AlbumPhoto`              | Photo in an album                                   |
| `WatchHistory`            | User's watch history with progress tracking         |
| `SavedVideo`              | User's saved/bookmarked videos                      |
| `WatchTimeEntry`          | Granular watch time analytics                       |
| `VideoLike`               | Video likes                                         |
| `Comment` / `CommentLike` | Threaded comments with likes                        |
| `Notification`            | User notifications (likes, comments, subscriptions) |


### Live Streaming


| Model        | Purpose                                                                                                                 |
| ------------ | ----------------------------------------------------------------------------------------------------------------------- |
| `StreamKey`  | RTMP stream key per channel                                                                                             |
| `LiveStream` | Active or past live stream. Status: `live → processing → ready`. Stores HLS path, recording path, FK to resulting Video |


### System


| Model            | Purpose                      |
| ---------------- | ---------------------------- |
| `Event`          | Audit log of system events   |
| `ActivityLog`    | User activity log            |
| `MomentCategory` | Categories for video moments |


---

## AI Models Used


| Model                                | Library           | Task                                                              | Queue       | Approx Size                |
| ------------------------------------ | ----------------- | ----------------------------------------------------------------- | ----------- | -------------------------- |
| **YOLOv8n**                          | `ultralytics`     | Object detection in video frames — 80 classes                     | processing  | ~6 MB                      |
| **InsightFace buffalo_l**            | `insightface`     | Face detection + 512-dim face embedding                           | processing  | ~300 MB                    |
| **BLIP**                             | `transformers`    | Scene description / image captioning (default)                    | processing  | ~1.8 GB                    |
| **Florence-2**                       | `transformers`    | Richer scene description (optional alternative to BLIP)           | processing  | ~1.5 GB                    |
| **CLIP ViT-B/32**                    | `transformers`    | 512-dim visual semantic embedding (powers semantic search)        | processing  | ~350 MB                    |
| **faster-whisper**                   | `faster-whisper`  | Speech → text transcription → subtitles                           | captions    | base ~150MB, medium ~1.5GB |
| **pyannote speaker-diarization-3.1** | `pyannote.audio`  | Who spoke when — assigns speaker labels to segments               | captions    | ~500 MB                    |
| **wespeaker-resnet34**               | `wespeaker`       | Speaker voice embedding for cross-video speaker matching          | captions    | ~50 MB                     |
| **PANNs CNN14**                      | `panns_inference` | Non-speech audio event detection (applause, music, laughter etc.) | audio       | ~150 MB                    |
| **NLLB-200-distilled-600M**          | `transformers`    | Subtitle translation — 200 languages                              | translation | ~2.4 GB                    |
| **Ollama llama3.1:8b**               | Ollama REST API   | AI video summary generation — manual trigger only                 | default     | ~5 GB                      |


### Model Cache Locations (on server)


| Model                                                         | Location                            |
| ------------------------------------------------------------- | ----------------------------------- |
| HuggingFace (Whisper, BLIP, CLIP, NLLB, Florence-2, pyannote) | `~/.cache/huggingface/hub/`         |
| InsightFace buffalo_l                                         | `~/.insightface/`                   |
| YOLO weights                                                  | `/var/www/cliplens/yolov8n.pt`      |
| PANNs CNN14                                                   | `~/panns_data/`                     |
| Ollama llama3.1:8b                                            | `/usr/share/ollama/.ollama/models/` |


---

## Celery Workers & Queues

### 4 Workers (each is a separate systemd service)


| Service                               | Queues consumed                     | Concurrency | Purpose                                               |
| ------------------------------------- | ----------------------------------- | ----------- | ----------------------------------------------------- |
| `cliplens-celery.service`             | `processing`, `captions`, `default` | 2           | Main worker — handles most AI tasks                   |
| `cliplens-celery-audio.service`       | `audio`                             | 1           | PANNs audio detection — isolated due to CPU intensity |
| `cliplens-celery-translation.service` | `translation`                       | 1           | NLLB-200 translation — isolated (large model)         |
| `cliplens-celery-live.service`        | `live`                              | 2           | FFmpeg live stream — blocking long-running task       |


### Queue → Task Routing


| Queue         | Tasks                                                                                                                                                                                                             |
| ------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `processing`  | `process_video_task`, `analyze_video_frames_task`, `generate_seek_thumbnails_task`, `upscale_video_task`, `upscale_photo_task`, `analyze_photo_task`, `extract_audio_tracks_task`, `process_livestream_recording` |
| `captions`    | `generate_captions_task`, `run_diarization_task`                                                                                                                                                                  |
| `default`     | `generate_video_summary_task`, `reindex_segments_task`                                                                                                                                                            |
| `translation` | `translate_subtitles_task`                                                                                                                                                                                        |
| `audio`       | `detect_audio_events_task`                                                                                                                                                                                        |
| `live`        | `run_live_ffmpeg`                                                                                                                                                                                                 |


---

## All Celery Tasks


| Task                            | Queue       | Trigger                  | What it does                                                                  |
| ------------------------------- | ----------- | ------------------------ | ----------------------------------------------------------------------------- |
| `process_video_task`            | processing  | Auto on upload           | FFmpeg HLS encode (adaptive bitrate), thumbnail extraction. Main entry point  |
| `generate_seek_thumbnails_task` | processing  | Auto after HLS           | Generates sprite sheet for seek-bar hover preview                             |
| `upscale_video_task`            | processing  | Manual (editor)          | Upscales video to higher resolution                                           |
| `upscale_photo_task`            | processing  | Manual (editor)          | Upscales a photo                                                              |
| `generate_captions_task`        | captions    | Auto after HLS           | faster-whisper transcription → Subtitle + VideoSegment rows                   |
| `generate_video_summary_task`   | default     | Manual (Generate button) | Ollama LLM reads transcript + scene descriptions → saves `video.ai_summary`   |
| `analyze_video_frames_task`     | processing  | Auto after HLS           | Samples frames → YOLO → InsightFace → BLIP/Florence-2 → CLIP                  |
| `analyze_photo_task`            | processing  | Auto on photo upload     | Same as above for photos + duplicate detection                                |
| `run_diarization_task`          | captions    | Manual (editor)          | pyannote diarization + wespeaker embedding → speaker labels on segments       |
| `detect_audio_events_task`      | audio       | Manual (editor)          | PANNs audio tagging + FFmpeg silence detection → AudioEvent rows              |
| `translate_subtitles_task`      | translation | Manual (editor)          | NLLB-200 translates subtitle to target language(s)                            |
| `extract_audio_tracks_task`     | processing  | Manual (editor)          | Extracts separate audio tracks from multi-audio video                         |
| `reindex_segments_task`         | default     | Auto after subtitle edit | Rebuilds PostgreSQL FTS index on VideoSegment                                 |
| `run_live_ffmpeg`               | live        | Auto on OBS connect      | Blocking — runs FFmpeg for entire stream. RTMP → HLS segments + MP4 recording |
| `process_livestream_recording`  | processing  | Auto after stream ends   | Creates Video from recording, queues full AI pipeline                         |


---

## Video Processing Pipeline

```
Upload
  └── process_video_task (processing queue)
        ├── FFmpeg: multi-quality HLS encode
        ├── FFmpeg: thumbnail extraction
        ├── → generate_captions_task (captions queue) [countdown 5s]
        │       └── faster-whisper transcription → Subtitle + VideoSegment rows
        ├── → analyze_video_frames_task (processing queue) [countdown 10s]
        │       ├── FFmpeg: extract frames every N seconds + scene changes
        │       ├── YOLO: object detection → VideoFrame.labels
        │       ├── InsightFace: face detection → DetectedFace rows
        │       ├── BLIP/Florence-2: scene description → VideoFrame.description
        │       └── CLIP: semantic embedding → VideoFrame.clip_embedding
        └── → generate_seek_thumbnails_task (processing queue) [countdown 15s]
                └── FFmpeg: sprite sheet → seek_sprites/<id>.jpg

Manual triggers (editor buttons on watch page):
  → generate_video_summary_task   (Ollama → video.ai_summary)
  → run_diarization_task          (pyannote + wespeaker → speaker labels)
  → detect_audio_events_task      (PANNs → AudioEvent rows)
  → translate_subtitles_task      (NLLB-200 → new Subtitle record)
```

## Live Stream Pipeline

```
OBS → RTMP (port 1935) → mediamtx
        ├── webhook: POST /api/streams/on-publish/
        │     └── Django: creates LiveStream record, queues run_live_ffmpeg
        └── run_live_ffmpeg (live queue — BLOCKS for stream duration)
              ├── Output 1: HLS segments → media/live/<id>/*.ts (viewers watch live)
              └── Output 2: MP4 recording → media/live/<id>/recording.mp4

OBS disconnects
  → mediamtx webhook: POST /api/streams/on-unpublish/ → records ended_at only
  → FFmpeg exits cleanly → run_live_ffmpeg:
        ├── marks LiveStream status: live → processing
        └── → process_livestream_recording (processing queue)
                  ├── Creates Video record pointing to recording.mp4
                  └── → process_video_task (full AI pipeline)
```

---

## Key Features

### Search (9 parallel passes on `?q=` query)

1. Video title + tags — FTS + pg_trgm fuzzy (threshold 0.35)
2. Chapter names — FTS + fuzzy
3. Speech/transcript — FTS only (long text, fuzzy too noisy)
4. YOLO object labels — FTS + fuzzy (threshold 0.50)
5. Scene descriptions — FTS only
6. CLIP semantic — pgvector ANN cosine similarity (threshold 0.24)
7. Face identity names — fuzzy match → DetectedFace join
8. Photos — FTS + fuzzy + CLIP
9. Named places — name/description match

### Roles

- **superadmin** — full access: admin panel, user management, management commands
- **editor** — upload, manage own channels, trigger AI tasks, delete content
- **viewer** — watch only, can like/save/comment

### Live Streaming

- OBS → RTMP → mediamtx → FFmpeg → HLS (live viewers) + MP4 recording
- Stream-copy HLS (no re-encoding = very low CPU)
- MP4 re-encoded to reset RTMP timestamps
- Auto-processed into VOD with full AI pipeline after stream ends

### Face Recognition

- Faces detected in every video frame and photo
- Clustered into `FaceIdentity` records (one per person)
- Cross-video matching via cosine similarity on 512-dim embeddings
- Editor can name/confirm/merge identities
- Speaker↔Face suggestion matching

### Photo DAM

- Upload photos → full AI pipeline (YOLO + InsightFace + BLIP + CLIP)
- Duplicate detection (cosine similarity > 0.97)
- Albums (manual or smart/filtered), archive tab, GPS + place tagging

---

## Ports & Internal Services


| Port   | Service       | Accessible from                    |
| ------ | ------------- | ---------------------------------- |
| 80/443 | Nginx         | Public internet                    |
| 8000   | Gunicorn      | Internal only (Nginx proxies here) |
| 1935   | mediamtx RTMP | Public (OBS connects here)         |
| 9997   | mediamtx API  | 127.0.0.1 only                     |
| 11434  | Ollama        | 127.0.0.1 only                     |
| 6379   | Redis         | 127.0.0.1 only                     |
| 5432   | PostgreSQL    | 127.0.0.1 only                     |


---

## Current Version

**v1.3.5** — on DAMSERVER (65.20.81.122)  
GitHub: `https://github.com/smaskara1812/ClipLens`