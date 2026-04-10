# ClipStream

**A self-hosted media intelligence platform for ingesting, processing, and semantically searching video and image assets.**

---

## What ClipStream Is

ClipStream is a Django-based platform that ingests video files and photos, runs a multi-stage AI pipeline over each asset, and exposes the resulting structured knowledge through a fast, multi-modal search interface.

The core idea: **raw media goes in, and a richly annotated, fully searchable knowledge base comes out** — without relying on any external API, cloud service, or proprietary search vendor. Everything runs on your own hardware.

---

## What ClipStream Is Not

ClipStream is not:

- A learning management system (LMS) — that fork is a separate deployed project
- A video CDN or streaming service — HLS playback exists but is a side effect of transcoding, not the goal
- A cloud storage replacement — it is designed for on-premise or self-hosted deployment

---

## Core Value


| Problem                                                                  | ClipStream's answer                                                                |
| ------------------------------------------------------------------------ | ---------------------------------------------------------------------------------- |
| "I need to find the exact moment someone says X in 500 hours of footage" | Whisper transcription + Postgres FTS on every segment                              |
| "I need every frame where a car or a person appears"                     | YOLOv8 object detection indexed per frame                                          |
| "I need to find all footage that looks like a sunset"                    | CLIP 512-d visual embeddings + pgvector HNSW ANN search                            |
| "I need all scenes featuring John Doe across every asset"                | InsightFace ArcFace embeddings + greedy identity clustering                        |
| "I need to search my photo library the same way I search videos"         | Unified DAM pipeline — photos go through the same YOLO/BLIP/CLIP/InsightFace stack |
| "My search is returning garbage with minor typos"                        | pg_trgm trigram fuzzy matching GIN-indexed on every text field                     |


---

## System Components

```
┌────────────────────────────────────────────────────────────┐
│                        Django 4.2                          │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────────┐  │
│  │  Ingestion   │  │  Processing  │  │     Search      │  │
│  │  (upload +   │  │  (Celery     │  │  (pgvector +    │  │
│  │   metadata)  │  │   workers)   │  │   FTS + trgm)   │  │
│  └──────┬───────┘  └──────┬───────┘  └────────┬────────┘  │
└─────────┼─────────────────┼───────────────────┼───────────┘
          │                 │                   │
          ▼                 ▼                   ▼
    PostgreSQL 16      Redis (broker)      REST API +
    + pgvector         Celery workers      HTML UI
```

### Ingestion layer

- Django REST Framework endpoints accept video files and images
- Files stored in `media/originals/` and `media/photos/originals/`
- Records created in Postgres with `status=pending`
- Celery tasks queued immediately

### Processing layer (Celery)

- **Video**: FFmpeg → HLS → scene-aware frame extraction (baseline interval + hard cuts) → YOLO → BLIP/Florence-2 → CLIP → InsightFace → Whisper
- **Photo**: YOLO → BLIP/Florence-2 → CLIP → InsightFace → Pillow thumbnail
- Workers pick up tasks from `processing` and `captions` queues
- Status transitions: `pending → processing → ready | failed`
- All AI results written back to Postgres

### Storage layer (Postgres + pgvector)

- `VideoFrame`: one row per sampled frame (interval + scene-cut frames), holds YOLO labels, BLIP caption, CLIP `vector(512)`, real timestamp from FFmpeg
- `VideoSegment`: one row per Whisper segment (start/end time + text)
- `DetectedFace`: one row per detected face (bbox, ArcFace embedding JSON, identity FK)
- `Photo`: one row per uploaded image, same AI fields as VideoFrame
- `FaceIdentity`: one row per person identity (running-average embedding JSON, name)
- HNSW indexes on all `clip_embedding` columns for ANN search
- GIN FTS + GIN trgm indexes on all text content

### Search layer

- **FTS** (Postgres `to_tsvector`) — exact lexeme match with stemming, no full-table scan
- **Fuzzy** (pg_trgm) — trigram similarity, catches typos and partial matches
- **Semantic CLIP** — text query → CLIP text encoder → HNSW ANN on `clip_embedding`
- All three are combined per search, results merged and deduped in Python
- Same channel / category / duration / date filters applied to AI sub-searches

---

## AI Pipeline Details

### Object detection (YOLO)

- Model: `yolov8n.pt` (nano, ~6 MB, runs on CPU in reasonable time)
- Per-frame: confidence ≥ 0.40, IoU ≥ 0.45
- Output: comma-separated class labels stored in `VideoFrame.labels` / `Photo.labels`
- Indexed by Postgres GIN FTS and trigram for instant keyword search

### Scene captioning (BLIP / Florence-2)

- Default: `Salesforce/blip-image-captioning-base`
- Optional: `microsoft/Florence-2-base` (swap via `SCENE_CAPTION_MODEL='florence2'`)
- Per-frame: produces a natural-language description of the scene
- Output stored in `VideoFrame.description` / `Photo.scene_description`
- Indexed by Postgres GIN FTS and trigram

### Visual embeddings (CLIP)

- Model: `openai/clip-vit-base-patch32` (ViT vision encoder, 512-d output)
- Per-frame: image → CLIP vision encoder → L2-normalise → 512-d float vector
- Stored as `vector(512)` in Postgres (pgvector extension)
- HNSW index with cosine distance ops enables ANN search in O(log n)
- Query: text → CLIP text encoder → L2-normalise → same embedding space → HNSW `<=>`
- Same model handles both video frames and photos

### Face recognition (InsightFace)

- Model: `buffalo_l` (ArcFace backbone)
- Per-frame: detect all faces, compute 512-d ArcFace embedding per face
- Pose filtering: confidence ≥ 0.50
- **Within-video clustering**: greedy cosine similarity (threshold 0.35) groups faces into identities
- **Cross-video matching**: clusters compared to all existing `FaceIdentity` records (named: 0.45; auto: 0.50)
- New unmatched clusters become `Person N` auto-identities
- Running-average embedding updated on each new match (stored as JSON in `FaceIdentity.ref_embedding`)
- Face crops saved to `media/face_crops/` for identity management UI

### Speech transcription (faster-whisper)

- Model: configurable (default `medium`)
- Output: WebVTT subtitle file + `VideoSegment` rows (start/end time, text)
- Indexed by Postgres GIN FTS + trigram for full-text in-video search

---

## Technology Stack


| Component         | Choice                | Why                                                    |
| ----------------- | --------------------- | ------------------------------------------------------ |
| Web framework     | Django 4.2            | Mature ORM, admin, auth                                |
| API               | Django REST Framework | Serializers, parsers, decorators                       |
| Task queue        | Celery + Redis        | Async, multi-queue, retry logic                        |
| Database          | PostgreSQL 16         | JSON, FTS, pgvector, pg_trgm                           |
| Vector search     | pgvector (HNSW)       | ANN search inside Postgres, no separate vector DB      |
| Full-text search  | Postgres FTS (GIN)    | Stemming, stop words, index-backed                     |
| Fuzzy search      | pg_trgm (GIN)         | Typo tolerance, partial match                          |
| Object detection  | YOLOv8 (ultralytics)  | Fast, accurate, CPU-friendly nano model                |
| Scene captioning  | BLIP / Florence-2     | Open-source, self-hosted, no API cost                  |
| Visual embeddings | CLIP ViT-B/32         | Joint vision–language space for semantic search        |
| Face recognition  | InsightFace buffalo_l | ArcFace, high accuracy, runs on CPU                    |
| Transcription     | faster-whisper        | CTranslate2-optimised Whisper, 4× faster               |
| Video transcoding | FFmpeg                | HLS segmenting, frame extraction                       |
| Image processing  | Pillow                | Thumbnail generation                                   |
| Caching           | Redis                 | Django cache backend, Celery broker                    |
| Auth              | Django auth           | Session-based + role system (superadmin/editor/viewer) |


---

## Deployment

- Single server: Django (gunicorn/uvicorn) + Postgres + Redis + Celery workers
- Static files: `collectstatic` → nginx
- Media files: `MEDIA_ROOT` served by nginx
- Workers: `celery -A ClipStream worker -Q processing,captions,default`
- All AI models downloaded from HuggingFace Hub on first run, cached locally

---

## Project History

ClipStream began as an LMS (Learning Management System) with embedded video delivery. As the ML processing capabilities matured, the two goals diverged:

- **LMS branch** — focused on courses, students, progress tracking — deployed separately
- **ClipStream (this repo)** — focused on media intelligence: ingestion, processing, search, DAM

This repository represents the post-fork state. ClipStream is now purpose-built for teams and individuals who need to **understand and search their media at a semantic level**, not just store and play it.