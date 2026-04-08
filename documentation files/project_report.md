# ClipLens: A Self-Hosted Multimodal Media Intelligence Platform
## Technical Project Report

**Author:** Soham Maskara
**Date:** April 2026
**Repository:** ClipLens (formerly FreeStream)
**Stack:** Python 3.11, Django 4.2, Celery, PostgreSQL, Redis, FFmpeg, YOLOv8, InsightFace, BLIP, Florence-2, OpenAI CLIP, faster-whisper, pgvector, pg_trgm

---

## Abstract

ClipLens is a self-hosted media intelligence platform that provides AI-powered multimodal search across large personal and organizational video and photo libraries. The system combines a Django 4.2 web application with an asynchronous AI analysis pipeline — running object detection, face recognition, scene captioning, semantic embedding generation, and speech transcription on every ingested asset — and a PostgreSQL-backed search engine that runs up to eight parallel query passes per request. This report describes the system architecture, each major implementation component, the engineering challenges encountered during development, and the lessons learned in building a production-grade AI-powered application as a first major software project.

---

## 1. Introduction & Motivation

### 1.1 Background

Large personal media libraries are increasingly difficult to search. The dominant tools available today force a choice: either use a cloud-based service with AI-powered search (Google Photos, YouTube, Vimeo) and surrender data ownership, or use a self-hosted media server (Plex, Jellyfin) and accept keyword-only search with no AI capability.

There is no self-hosted, privacy-preserving solution that provides the kind of multimodal search familiar from commercial platforms — searching for "the moment someone mentioned the budget in the Berlin trip videos," or "any photo with a dog at the beach," or "all footage where Alice appears." ClipLens was built to fill this gap.

### 1.2 Goals

The project had four primary goals:

1. **Privacy-preserving AI:** Run all AI inference locally using open-source models — no cloud API calls, no data leaving the machine.
2. **Multimodal search:** Support search across objects, faces, speech, scene descriptions, and semantic meaning in a single unified query interface.
3. **Production-quality streaming:** Serve video with adaptive bitrate HLS rather than progressive download, matching the quality of commercial platforms.
4. **Practical self-hosting:** Operate reliably on a single server with standard hardware, deployable by a technically capable individual without DevOps expertise.

### 1.3 Scope

ClipLens is a monolithic Django application with a single `videos` app. It deliberately avoids microservices, separate frontend frameworks, and cloud dependencies. The design prioritizes operability over scalability — it targets libraries in the range of hundreds to low thousands of videos, not millions.

---

## 2. System Architecture

### 2.1 High-Level Overview

ClipLens consists of five major subsystems:

```
[ Browser ]
    |
    | HTTP
    v
[ Django (views.py) ]  <-->  [ PostgreSQL (pgvector, pg_trgm, FTS) ]
    |                              |
    | Celery task dispatch         | Model storage
    v                              |
[ Redis (broker) ]           [ Media filesystem ]
    |
    v
[ Celery Workers ]
    |-- process_video_task  --> FFmpeg HLS, YOLO, InsightFace, BLIP/Florence-2, CLIP, Whisper
    |-- analyze_photo_task  --> PIL, YOLO, InsightFace, BLIP/Florence-2, CLIP, duplicate detection
    `-- generate_captions_task --> faster-whisper
```

### 2.2 Django Application

The application is a single Django app (`videos`) with no separate frontend framework. All UI is server-rendered via Django templates with vanilla JavaScript. This choice eliminates a frontend build pipeline and keeps the development cycle simple — template changes are reflected immediately without a compile step.

Key files:

| File | Role |
|------|------|
| `videos/models.py` | All database models |
| `videos/views.py` | All views and API endpoints (~3,800 lines) |
| `videos/tasks.py` | All Celery tasks |
| `videos/urls.py` | URL routing |
| `videos/templates/videos/` | All HTML templates |
| `cliplens/settings.py` | Django configuration |

### 2.3 Database Design

PostgreSQL was chosen specifically for two extensions that are central to the search architecture:

- **pgvector:** Stores 512-dimensional CLIP and BLIP embeddings as `vector` columns. HNSW indexes support approximate nearest-neighbor search at sub-linear cost.
- **pg_trgm:** Enables trigram-based fuzzy string matching with GIN indexes. This powers the "fuzzy search" passes that catch misspellings and partial matches.
- **Django FTS:** Full-text search uses Django's `SearchVector`/`SearchQuery` with the `'english'` stemming configuration for exact semantic matching.

Core models:

| Model | Description |
|-------|-------------|
| `Video` | A video asset with title, tags, HLS path, channel FK |
| `VideoFrame` | A sampled frame with YOLO labels, scene description, CLIP embedding |
| `VideoSegment` | A Whisper transcript segment with text, start/end timestamps |
| `VideoChapter` | A named chapter within a video |
| `Photo` | A photo asset with labels, face names, CLIP embedding |
| `FaceIdentity` | A named person identity (one per person) |
| `DetectedFace` | A face crop with embedding, linked to Video+Frame or Photo |
| `Album` | A named collection of photos |
| `Subtitle` | A VTT/SRT subtitle file linked to a video |
| `Channel` | A top-level organizational unit |

### 2.4 Task Queue

Three Celery queues handle different priority classes:

- `processing` — main video and photo analysis pipeline (long-running, GPU-intensive)
- `captions` — Whisper transcription (long-running, CPU/GPU)
- `default` — short utility tasks

The `django-db` result backend stores task results in PostgreSQL, enabling persistence across worker restarts. Flower provides a web dashboard at `localhost:5555` for monitoring queue depth, worker health, and task history.

---

## 3. Implementation Details

### 3.1 Video Ingestion & HLS Pipeline

Video ingestion is initiated via the `ingest` management command, which accepts a channel slug and file path. The ingestion process:

1. Creates a `Video` record in the database with status `pending`
2. Dispatches `process_video_task` to the `processing` Celery queue
3. Returns immediately; the CLI user sees the task ID

Inside `process_video_task`:

1. **FFmpeg HLS transcoding:** The source video is transcoded into an HLS playlist with 8 quality levels (240p through 1080p, or source resolution if lower). FFmpeg generates `.m3u8` playlist files and `.ts` segment files in the media directory. The master playlist references all quality variants, and HLS.js on the client performs adaptive bitrate selection.

2. **Frame sampling:** FFmpeg extracts frames at a configurable interval (default: 1 frame per 5 seconds) for AI analysis. Frames are written to temporary storage and processed in sequence.

3. **YOLO object detection:** Each sampled frame is run through YOLOv8 (the `yolov8n.pt` nano model for speed). Detected object class names above confidence threshold are stored as a comma-separated string in `VideoFrame.labels`.

4. **InsightFace face detection:** Each frame is processed by InsightFace to detect faces, compute 512-dimensional ArcFace embeddings, and save face crop images to disk. Each crop becomes a `DetectedFace` record linked to the frame.

5. **BLIP/Florence-2 scene description:** A captioning model generates a natural language description of each frame's overall scene content. This description is stored in `VideoFrame.description`.

6. **CLIP embedding:** OpenAI CLIP generates a 512-dimensional embedding for each frame's image. This embedding is stored in `VideoFrame.clip_embedding` as a pgvector `vector` column.

7. **Whisper transcription:** The `generate_captions_task` (dispatched to the `captions` queue) runs faster-whisper on the audio track, producing time-stamped text segments stored as `VideoSegment` records.

The photo pipeline (`analyze_photo_task`) is structurally identical but operates on a single image rather than a sequence of frames, and adds a duplicate detection step using cosine similarity between CLIP embeddings.

### 3.2 Multimodal Search Engine

The search engine is implemented entirely within the `player_page` view function in `views.py`. When a `?q=` parameter is present, it executes up to 8 search passes in parallel using Python's `concurrent.futures.ThreadPoolExecutor` or sequential Django ORM queries (depending on whether the pass can be parallelized safely with the ORM connection pool).

**Pass 1 — Video title/tags:**
```
SearchVector('title', 'tags') + trigram similarity on title (threshold 0.35)
```
Results are `Video` objects.

**Pass 2 — Chapter names:**
```
SearchVector('title') on VideoChapter + trigram similarity (threshold 0.35)
```
Results link back to parent `Video`.

**Pass 3 — Speech segments:**
```
SearchVector('text') on VideoSegment
```
No fuzzy — long transcript text produces too many false positives with trigram matching.

**Pass 4 — YOLO object labels:**
```
SearchVector('labels') on VideoFrame + trigram similarity (threshold 0.50)
```
Higher threshold than default because YOLO labels are short structured strings (e.g., "person", "car") where a 0.35 threshold caused excessive false matches.

**Pass 5 — Scene descriptions:**
```
SearchVector('description') on VideoFrame
```
No fuzzy — same reasoning as speech segments.

**Pass 6 — CLIP semantic search:**
```
CLIP.encode(query) → pgvector cosine ANN on VideoFrame.clip_embedding
Threshold: 0.24 for video frames, 0.28 for photos
```

**Pass 7 — Face identity:**
```
FaceIdentity.name trigram match → DetectedFace join → Video/Photo
```

**Pass 8 — Photos:**
All of the above (minus HLS-specific passes) applied to the `Photo` model simultaneously.

Hard result caps prevent unbounded memory consumption: speech 500 segments, YOLO/scene 400 frames, CLIP 250 frames, chapters 200.

### 3.3 Face Identity Management

The face recognition workflow is one of the most complex subsystems:

1. **Detection:** InsightFace detects faces in every video frame and photo. Each detection produces a crop image and a 512-dim ArcFace embedding stored in `DetectedFace`.

2. **Identity clustering:** New faces are compared against existing `FaceIdentity` embeddings. If cosine similarity exceeds the auto-confirm threshold, the face is automatically assigned to the matching identity. Below the threshold, the face is flagged for manual review.

3. **Manual review:** The People page shows unconfirmed faces. An editor can confirm, reject, or reassign each face to an identity. Multiple faces can be merged into a single identity.

4. **Bulk operations:** Management commands handle large-scale operations:
   - `propagate_identities` — re-runs identity assignment across all unassigned faces
   - `auto_confirm_similar` — bulk-confirms faces above a similarity threshold
   - `rename_identities` — batch renames from a CSV input

5. **Search integration:** `FaceIdentity.name` is indexed for both FTS and trigram search. A query for "Alice" returns all videos and photos where a confirmed Alice face appears.

### 3.4 Subtitle Editor

The subtitle editor provides in-browser VTT/SRT editing backed by the video player:

- **Video playback:** HLS.js loads the video's HLS playlist and attaches the VTT subtitle track as a side-loaded text track. This was necessary because native `<video>` tag subtitle support does not integrate well with HLS.js's adaptive playback.
- **Cue editing:** Each subtitle cue is rendered as an editable row. The user can modify text and timestamps directly. Changes are submitted via a form POST and written back to the VTT file on disk.
- **Regeneration:** A button triggers `generate_captions_task` via Celery. The task runs faster-whisper, writes the new VTT, and updates the `Subtitle` record. The editor page polls for task completion.

### 3.5 Role-Based Access Control

Three roles are enforced via the `@editor_required` decorator applied to all write-operation views:

| Role | Permissions |
|------|-------------|
| `viewer` | Browse, search, watch |
| `editor` | All viewer permissions + upload, edit metadata, manage subtitles |
| `superadmin` | All editor permissions + management command panel, role assignment |

Roles are stored in a `UserProfile` model linked one-to-one with Django's built-in `User` model. Role assignment is done via the `assign_role` management command.

---

## 4. Challenges & Solutions

### 4.1 YOLO Label Fuzzy Search: False Positive Noise

**Problem:** Initial implementation applied the same trigram fuzzy threshold (0.35) to all text fields. YOLO labels are very short structured strings (e.g., "person", "car", "laptop"). At threshold 0.35, short query terms produced incorrect matches — for example, querying "cow" would match "crow" or "cot" in the label space.

**What was tried:** Lowering the threshold further made the problem worse. Disabling fuzzy for labels entirely meant misspellings never matched anything.

**Solution:** Per-call threshold override. The YOLO search pass uses threshold 0.50 rather than the default 0.35. This is high enough to require substantial string overlap, eliminating short-string noise while still catching genuine misspellings of longer labels (e.g., "elefant" still matches "elephant").

**Lesson:** There is no single fuzzy threshold that works well across all field types. Each field type needs an independently tuned threshold based on the characteristics of the data it stores.

---

### 4.2 Short Query Fuzzy Noise (3-Character Guard)

**Problem:** Queries of 3 characters or fewer produced severe fuzzy noise. The query "cow" would trigram-match "course," "crow," "cob," and many other unrelated strings across the library.

**What was tried:** Raising thresholds helped but did not eliminate the problem for very short queries, because trigrams for 3-character strings have almost no discriminating power.

**Solution:** A length guard: if the query term is 3 characters or fewer, all fuzzy matching passes are skipped entirely. Only FTS and exact matching run. For short queries, FTS stemming provides sufficient recall without the noise of trigram matching.

**Lesson:** Fuzzy matching algorithms have fundamental limitations on short input strings. It is better to disable a feature gracefully than to tune it into marginal territory.

---

### 4.3 CLIP Semantic False Positives

**Problem:** CLIP semantic search, while powerful, produced surprising false positives. Querying "cow" in a library that contained no cows returned office footage from a Bali trip — presumably because CLIP's embedding space conflated "Bali" with certain visual patterns that overlapped with the "cow" query embedding.

**What was tried:** Adjusting the cosine similarity threshold uniformly. A single global threshold either cut too many true positives or admitted too many false positives.

**Solution:** Separate thresholds for video frames (0.24) and photos (0.28), tuned independently by observing false positive rates against a known test set. Photos tend to be higher quality and more compositionally distinct, supporting a higher threshold. Video frames are sampled from potentially noisy or blurry segments, requiring a lower threshold to maintain recall.

**Lesson:** Semantic embedding search quality depends heavily on the distribution of the data. Thresholds must be tuned per data type, not set globally.

---

### 4.4 OCR Substring Matching

**Problem:** Initial implementation applied FTS to OCR text extracted from video frames. FTS uses stemming and tokenization designed for natural prose, which performed poorly on OCR output — a mix of proper nouns, partial words, numbers, and typographic noise. Queries for brand names or short codes produced no results despite the text being present.

**What was tried:** FTS with different stemming configurations (`'simple'` vs `'english'`). Neither handled OCR-style text well.

**Solution:** Removed FTS from OCR text entirely. Replaced with pure `icontains` (case-insensitive substring matching) via Django ORM. Substring matching on OCR text is more appropriate because users typically search for the exact string they saw on screen, not a stemmed variant.

**Lesson:** FTS is designed for natural language prose. For structured or noisy text (OCR, labels, codes), simpler string matching often outperforms FTS.

---

### 4.5 People Page Count Discrepancy

**Problem:** The People list page showed a `total` face count for each identity that was significantly higher than the number of faces visible on the identity detail page. This confused users who expected the counts to match.

**Investigation:** The `total` annotation counted all `DetectedFace` rows associated with an identity. The detail page only showed faces where `crop_path` was set (i.e., where the face crop image had actually been saved to disk). Some early pipeline runs had saved detection records without successfully writing the crop file, producing "phantom" face records.

**Solution:** Updated the list page annotation to count only `DetectedFace` rows where `crop_path` is non-empty, matching the detail page's filter. The higher total count (including cropless detections) is deliberately retained in a separate internal field for diagnostic purposes.

**Lesson:** User-facing counts must exactly match user-visible content. A count that includes data the user cannot see reads as a bug even if technically correct.

---

### 4.6 N+1 Query Problems

**Problem:** Several pages, particularly the video list and people detail page, were extremely slow on large libraries. Django Debug Toolbar revealed dozens to hundreds of individual SQL queries per page load — a classic N+1 pattern where each item in a list triggered an additional database round-trip.

**Examples found:**
- Video list: each `Video` object triggered a separate query to fetch its `Channel`
- People detail: each `DetectedFace` triggered separate queries for its linked `Video` and `VideoFrame`
- Search results: each result object triggered queries for related tags and face identities

**Solution:** Systematic audit of all list and detail views using Django Debug Toolbar's SQL panel. Added `select_related()` for all forward FK relationships and `prefetch_related()` for all reverse FK and M2M relationships traversed in templates. Query counts on the video list page dropped from O(N) to O(1) for relationship data.

**Lesson:** Django's ORM lazy-loads related objects by default. Any view that iterates over a queryset and accesses related objects must use `select_related`/`prefetch_related`. This is non-obvious and must be audited explicitly — the ORM does not warn about N+1 patterns.

---

### 4.7 Celery Result Persistence

**Problem:** During development, Celery was configured with the default in-memory result backend. When the worker was restarted, all task results and status information were lost. This made it impossible to check whether a video had finished processing after a worker restart, leading to UI states that showed "processing" indefinitely for completed videos.

**Solution:** Switched the Celery result backend to `django-db`, which stores task results in a PostgreSQL table (`django_celery_results_taskresult`). Results now persist across worker restarts and can be queried by the Django application to update video processing status.

**Lesson:** In-memory Celery backends are only appropriate for fire-and-forget tasks where result persistence is not needed. Any application that checks task status from the web layer requires a persistent backend.

---

### 4.8 HLS.js and Subtitle Editor Integration

**Problem:** The subtitle editor needed to display the video alongside the cue editing interface. Initial implementation used a plain `<video>` element. However, HLS streams (`.m3u8` playlists) are not natively supported by Chrome and Firefox — they require HLS.js for playback. Additionally, attaching VTT subtitle tracks to an HLS.js-managed video element requires specific configuration that differs from the native `<track>` element approach.

**What failed:** Loading the `.m3u8` source directly in a `<video>` element produced no video on Chrome. Adding a `<track>` element with the VTT file caused subtitle rendering issues because HLS.js intercepts the video element's source management.

**Solution:** Used HLS.js's API to attach the stream and manually added the VTT track as a side-loaded text track after HLS.js initialized the video element. This required careful sequencing — the track can only be added after the `MANIFEST_PARSED` event fires.

**Lesson:** HLS.js introduces a non-standard video element lifecycle. Any code that modifies the video element (adding tracks, seeking, quality selection) must be written against HLS.js's event system, not the native HTMLVideoElement API.

---

### 4.9 Project Rename (freestream → cliplens)

**Problem:** The project was originally named FreeStream. Mid-development, the decision was made to rename it to ClipLens. This required more than a simple find-and-replace: Django uses the module name in settings (`DJANGO_SETTINGS_MODULE`), Celery configuration, WSGI/ASGI application references, and database migration history.

**Solution:** Systematic rename at the module level:
1. Renamed the `freestream/` settings directory to `cliplens/`
2. Updated `DJANGO_SETTINGS_MODULE` in all startup scripts and the `.env` file
3. Updated `manage.py`, `wsgi.py`, and `asgi.py` references
4. Updated `celery.py` application name and all `@shared_task` imports
5. Updated `start.sh` and `start_flower.sh`
6. Updated `requirements.txt` and any references in templates

No database migration was required because the rename was at the Python module level, not the Django app label level (the `videos` app label was unchanged).

**Lesson:** Project renames in Django are feasible but require a systematic checklist. The riskiest part is the `DJANGO_SETTINGS_MODULE` environment variable — if it remains pointing at the old module name, the application silently fails to find settings rather than raising a clear error.

---

## 5. Results & Current State

### 5.1 Feature Completion

| Feature | Status |
|---------|--------|
| HLS video streaming (8 quality levels) | Complete |
| YOLO object detection | Complete |
| InsightFace face recognition | Complete |
| BLIP/Florence-2 scene description | Complete |
| CLIP semantic embedding + ANN search | Complete |
| Whisper speech transcription | Complete |
| 8-pass multimodal search engine | Complete |
| Photo DAM with duplicate detection | Complete |
| Face identity management + manual review | Complete |
| In-browser subtitle editor | Complete |
| Role-based access control (3 roles) | Complete |
| Album / playlist / channel management | Complete |
| Celery async pipeline + Flower monitoring | Complete |
| 8 management commands | Complete |

### 5.2 Known Limitations

- **No search pagination for AI results:** All results within the per-pass hard caps are rendered at once. This is acceptable for current library sizes but will not scale to very large collections.
- **No automated test suite:** All testing has been manual. A testing suite is the most significant technical debt item.
- **Single-server deployment only:** No distributed Celery, no load balancing, no horizontal scaling.
- **GPU optional but practically required:** On CPU-only hardware, the AI pipeline processes approximately one frame per second for CLIP/BLIP models, making ingestion of a 1-hour video impractically slow.

### 5.3 Performance Observations

On reference hardware (Apple M-series CPU, no discrete GPU):
- Search response time: 1–2 seconds for a library of ~500 videos
- HLS first-frame time: < 1 second on local network
- Photo analysis (all AI passes): ~20–40 seconds per photo
- Video ingestion (30-min video, all AI passes): ~45–90 minutes on CPU; estimated 5–15 minutes with a mid-range NVIDIA GPU

---

## 6. Future Work

### 6.1 Near-Term

- **EXIF metadata extraction:** Extract GPS coordinates, camera model, capture date from photo EXIF headers and expose them in the UI and search
- **Map view:** Display geotagged photos and videos on an interactive map using extracted coordinates
- **Automated test suite:** Add Django test cases for the search engine passes, at minimum, to prevent regression as the codebase grows

### 6.2 Medium-Term

- **Search result pagination:** Implement cursor-based pagination for AI search results to support larger libraries without the current hard caps
- **Moondream model swap:** Evaluate replacing BLIP/Florence-2 with Moondream (a smaller, faster vision-language model) for scene description to improve pipeline throughput on CPU hardware
- **Memories feature:** Automatically surface "on this day" content or algorithmically curated highlight reels from the library

### 6.3 Long-Term

- **Load-balancing Celery workers:** Allow the AI pipeline to run on a separate machine from the web server
- **Object storage support:** Add an optional S3-compatible backend for media storage to support larger libraries
- **Mobile-responsive UI improvements:** Improve the interface for tablet and mobile browsing
- **Automated duplicate video detection:** Extend the cosine similarity deduplication from photos to video content using per-video CLIP embedding aggregates

---

## 7. Conclusion

ClipLens demonstrates that AI-powered multimodal media search is achievable on self-hosted hardware using entirely open-source components, without cloud API dependencies. The project required integrating a diverse set of systems — a Django web application, an asynchronous Celery pipeline, five distinct AI models, a vector database, a fuzzy text search system, and a full-text search engine — into a coherent product.

The most significant engineering lessons from this project were not about any individual technology, but about system integration and search quality tuning. The search engine required multiple iterations of threshold tuning, feature gating by data type, and failure mode analysis before it produced results that felt correct to a user. The AI pipeline required careful error isolation so that a failure in one model did not cascade into a failed ingestion. And the overall architecture required repeated debugging of Django ORM behavior, Celery result persistence, and browser video API constraints that are not documented in any single place.

As a first major software project, ClipLens provided hands-on experience with the full stack of a production-grade web application — from database index design to CSS layout — while also engaging with the specific challenges of deploying AI models as part of a user-facing product. The codebase is intentionally simple in its architecture (a Django monolith with a single app) because simplicity was the right tradeoff for a first project: it kept the system debuggable, the deployment straightforward, and the iteration cycle fast.
