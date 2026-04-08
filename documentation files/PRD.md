# Product Requirements Document (PRD)
## ClipLens — Self-Hosted Media Intelligence Platform

**Version:** 1.0
**Author:** Soham Maskara
**Date:** April 2026
**Status:** Active Development

---

## 1. Executive Summary

ClipLens is a self-hosted media intelligence platform that enables individuals and small teams to search across large personal or organizational video and photo libraries using natural language, AI-generated metadata, and multimodal queries. It combines a traditional media server with a full AI analysis pipeline — object detection, face recognition, scene description, speech transcription, and semantic embeddings — to make unstructured media collections fully searchable without relying on any cloud service.

The product is designed for users who want ownership and privacy over their media data while still benefiting from AI-powered organization and discovery tools that are otherwise only available in commercial platforms like Google Photos, Plex, or Vimeo.

---

## 2. Problem Statement

### 2.1 The Core Problem

Personal and professional video libraries grow faster than they can be organized. Most media management tools fall into one of two categories:

1. **Cloud-based AI services** (Google Photos, Vimeo, YouTube) — these provide excellent AI search and discovery, but require uploading all media to a third-party server, surrendering data ownership and privacy.
2. **Self-hosted media servers** (Plex, Jellyfin) — these preserve data ownership, but offer only basic title/tag search with no AI-powered discovery capabilities.

There is no self-hosted solution that provides AI-grade multimodal search (semantic, object, face, speech) while keeping all data and processing local.

### 2.2 Specific Pain Points

- Finding a specific moment in a video requires watching it or remembering file names
- Searching for "people running on a beach" across thousands of videos is impossible without AI
- Face recognition across video and photo libraries requires expensive cloud APIs or complex self-assembly
- Subtitle/transcript search requires pre-generated captions per file
- Duplicate photo management in large libraries is manual and error-prone
- Adaptive streaming for large video files is missing from most self-hosted tools

---

## 3. Target Users

### Primary User: The Power Media Collector
- Stores hundreds to thousands of personal videos and photos locally
- Values privacy and data sovereignty — unwilling to upload to cloud services
- Has a home server or NAS capable of running Python/Docker workloads
- Technically comfortable enough to run a CLI installer and manage a database
- Use case: finding specific moments in family videos, travel footage, research recordings

### Secondary User: The Small Production Team
- A small content studio, research team, or journalism organization
- Manages a shared library of raw footage, B-roll, and reference material
- Needs multiple roles (editors vs viewers) with access control
- Use case: searching a library of hundreds of hours of interview footage by speaker, topic, or visual content

### Out of Scope Users
- Enterprise media asset management (requires complex rights management, CDN, SLA)
- Mobile-first users (no native mobile app planned)
- Non-technical users who cannot run a local server

---

## 4. Core Features & Acceptance Criteria

### 4.1 Video Ingestion & HLS Streaming

**Feature:** Import video files into ClipLens and serve them as adaptive bitrate HLS streams.

**Acceptance Criteria:**
- System accepts video files via CLI ingestion command (`python manage.py ingest`)
- FFmpeg converts uploaded video to HLS with a minimum of 4 quality levels (360p, 480p, 720p, 1080p)
- Adaptive bitrate switching occurs client-side via HLS.js
- Ingestion status is visible in the UI; failures surface a human-readable error message
- Original file is preserved; HLS segments are stored in a configurable media directory
- Channel/playlist organization is supported at ingest time

**Priority:** P0 (Core)

---

### 4.2 AI Analysis Pipeline

**Feature:** Automatically analyze ingested videos and photos to extract structured metadata for search.

**Acceptance Criteria:**
- YOLO object detection runs on sampled video frames and stores labels per frame
- InsightFace face detection runs on sampled frames, crops faces, and stores crops with embeddings
- BLIP or Florence-2 generates a natural language scene description per sampled frame
- OpenAI CLIP generates 512-dim embeddings per frame for semantic search
- Whisper (faster-whisper) transcribes audio to text segments with timestamps
- All pipeline stages run asynchronously via Celery; the UI remains responsive during processing
- Pipeline progress is visible via Flower monitoring dashboard
- Each stage fails gracefully — if YOLO fails, other stages still complete
- Photo analysis pipeline mirrors the video pipeline (YOLO, InsightFace, CLIP, BLIP)

**Priority:** P0 (Core)

---

### 4.3 Multimodal Search

**Feature:** Allow users to search the media library using natural language queries that span multiple data modalities simultaneously.

**Acceptance Criteria:**
- A single search query triggers up to 8 parallel search passes:
  1. Video title and tags (FTS + trigram fuzzy, threshold 0.35)
  2. Chapter names (FTS + fuzzy, threshold 0.35)
  3. Speech/transcript segments (FTS only, no fuzzy — text too long)
  4. YOLO object labels (FTS + fuzzy, threshold 0.50)
  5. Scene descriptions (FTS only)
  6. CLIP semantic embeddings (pgvector ANN, cosine similarity)
  7. Face identity names (FTS + fuzzy, threshold 0.35)
  8. Photos (FTS + fuzzy + CLIP across all photo metadata)
- Queries of 3 characters or fewer skip fuzzy matching to prevent noise (e.g., "cow" should not match "course")
- CLIP cosine thresholds: 0.24 for video frames, 0.28 for photos
- Results from all passes are merged and presented in a unified interface with source labels
- Search completes in under 3 seconds for libraries up to 10,000 videos on commodity hardware

**Priority:** P0 (Core)

---

### 4.4 Face Identity Management

**Feature:** Recognize faces across the media library, cluster them into identities, and allow users to label and review them.

**Acceptance Criteria:**
- Every face detected in a video frame or photo is saved as a `DetectedFace` record with a cropped image
- Faces are clustered into `FaceIdentity` records using embedding similarity
- The People page (`/faces/`) lists all identities with representative face crops
- Clicking an identity shows all media where that person appears, grouped by source (5 groups per page)
- Users can merge identities, rename them, and confirm/reject face assignments via the manual review workflow
- Management commands support bulk identity propagation (`propagate_identities`) and auto-confirmation of high-confidence matches (`auto_confirm_similar`)
- Face search integrates with the main search (searching "Alice" returns all media featuring Alice)
- Face count on the People list reflects only faces with saved crop images (not raw detection count)

**Priority:** P1 (High)

---

### 4.5 Photo DAM (Digital Asset Management)

**Feature:** Manage a photo library with AI-powered organization, deduplication, and search.

**Acceptance Criteria:**
- Photos can be uploaded individually or in bulk
- AI pipeline (YOLO, InsightFace, CLIP, BLIP) runs on each photo asynchronously
- Duplicate detection uses CLIP cosine similarity > 0.97 to flag near-identical images
- A dedicated duplicates management page (`/photos/duplicates/`) presents pairs for resolution
- Archive tab allows soft-hiding photos from the main grid without deletion
- Photos share the face identity system with videos (a face found in a photo can be linked to the same identity as in videos)

**Priority:** P1 (High)

---

### 4.6 Subtitle Editor

**Feature:** In-browser editing of video subtitles with support for VTT and SRT formats.

**Acceptance Criteria:**
- Users can upload VTT or SRT subtitle files for any video
- Subtitles can be regenerated from Whisper transcription via a UI button
- The subtitle editor page displays the video alongside an editable cue list
- Users can modify cue text and timestamps inline
- Changes are saved and the updated file is served on the next video load
- HLS.js handles subtitle track loading during video playback in the editor

**Priority:** P1 (High)

---

### 4.7 Role-Based Access Control

**Feature:** Control what different users can do within the platform.

**Acceptance Criteria:**
- Three roles are supported: `superadmin`, `editor`, `viewer`
- Viewers can browse and search; they cannot upload, delete, or modify
- Editors can ingest, edit metadata, and manage subtitles; they cannot access admin panel
- Superadmins have full access including the management commands panel (`/admin-panel/commands/`)
- Role assignment is managed via the `assign_role` management command
- The `@editor_required` decorator is applied to all write-operation views

**Priority:** P1 (High)

---

### 4.8 Albums, Playlists & Channels

**Feature:** Organize media into curated collections.

**Acceptance Criteria:**
- Albums group photos; playlists group videos; channels are top-level organizational units
- Users can create, rename, and delete albums and playlists
- Videos and photos can belong to multiple albums/playlists
- Channel filtering narrows search to a specific channel's content
- The `patch_playlists` management command can bulk-repair playlist associations after ingestion

**Priority:** P2 (Medium)

---

## 5. Non-Functional Requirements

### 5.1 Performance

| Metric | Target |
|--------|--------|
| Search response time (query → results rendered) | < 3 seconds for libraries up to 10,000 videos |
| Video player first-frame time (HLS) | < 2 seconds on local network |
| AI pipeline throughput | >= 1 video processed per minute on a machine with a mid-range GPU |
| Photo upload + analysis | < 60 seconds per photo on reference hardware |
| Database query time per search pass | < 500ms (enforced by hard caps per pass) |

### 5.2 Security

- All write operations require authentication and appropriate role (`@editor_required` or superadmin)
- Session management via Django's built-in auth framework
- Static files served via WhiteNoise (no direct filesystem exposure)
- Media files are served from a configurable directory; path traversal mitigated by Django's file serving
- No external API calls for AI inference — all models run locally
- Secret key and database credentials managed via environment variables

### 5.3 Scalability

- Database uses HNSW vector index (pgvector) for sub-linear ANN search scaling
- GIN indexes on pg_trgm columns ensure fuzzy search does not degrade with library size
- Celery task queue allows horizontal scaling of workers if needed
- Hard result caps per search pass prevent unbounded memory usage (speech: 500, YOLO/scene: 400, CLIP: 250, chapters: 200)
- Django's ORM N+1 queries mitigated with `select_related` and `prefetch_related` throughout

### 5.4 Reliability

- Celery tasks use `django-db` result backend for persistence across worker restarts
- Each AI pipeline stage is isolated — a failure in one stage (e.g., CLIP) does not block other stages
- Database migrations are tracked via Django's migration system; rollback is supported
- Flower dashboard provides real-time visibility into task queue health

### 5.5 Maintainability

- Single Django app (`videos`) keeps code co-located and avoids unnecessary abstraction
- All views in a single `views.py` — searchable but requires discipline to maintain
- Templates use server-side rendering with vanilla JS (no separate frontend build step)
- Management commands encapsulate all bulk/admin operations for reproducibility

---

## 6. Technical Constraints

- **Python 3.11** — required by faster-whisper and some InsightFace dependencies
- **PostgreSQL** — required for pgvector and pg_trgm; no SQLite support
- **Redis** — required as Celery broker; cannot be swapped without code changes
- **FFmpeg** — must be installed system-wide for HLS transcoding
- **GPU optional but strongly recommended** — YOLO, InsightFace, BLIP/Florence-2, and CLIP are CPU-runnable but impractically slow on large libraries without a CUDA-capable GPU
- **Local filesystem** — media files stored on local disk; no S3/object storage support in current version
- **Single-server deployment** — no distributed deployment design; all components (Django, Celery, PostgreSQL, Redis) expected on one machine or LAN

---

## 7. Out of Scope (v1.0)

The following are explicitly not in scope for the current version:

| Feature | Rationale |
|---------|-----------|
| Mobile native app | UI is responsive but not optimized for mobile; no native apps planned |
| Cloud storage backends (S3, GCS) | Adds complexity; target user stores media locally |
| Public sharing / CDN delivery | Target use case is private/LAN deployment |
| Multi-server distributed Celery | Single-server target; horizontal scaling is future work |
| Real-time collaborative editing | No WebSocket infrastructure planned |
| Commercial licensing / rights management | Out of scope for personal/small team use case |
| Transcoding to formats other than HLS | FFmpeg supports it but not exposed in UI |
| Plugin/extension system | Monolith design intentional; extensibility via management commands |
| Automated testing suite | Not yet implemented; manual QA only in v1.0 |
| EXIF metadata extraction for photos | Listed as pending future work |
| Map view for geotagged media | Listed as pending future work |

---

## 8. Assumptions & Dependencies

- The operator has PostgreSQL 14+ with the `pgvector` and `pg_trgm` extensions available
- Redis is available on the same host or LAN
- FFmpeg is installed and accessible in the system PATH
- Python AI model weights are downloaded on first run and cached locally (HuggingFace, YOLO, InsightFace caches)
- The platform is deployed on a trusted LAN or with appropriate network-level access control (no built-in TLS/reverse-proxy configuration provided)
