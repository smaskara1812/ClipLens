# ClipLens Project Report (Structured Format)

## Cover Page

Leave this page as per your existing MUJ cover page template.

## Declaration

Leave this page as per your existing declaration template.

## Acknowledgments

Leave this page as per your existing acknowledgment template.

---

## Abstract

ClipLens is a self-hosted media intelligence platform built on Django, PostgreSQL, and Celery to solve the problem of searching large video and photo archives with AI-generated context. The system ingests videos/photos, processes them asynchronously using FFmpeg, YOLO, InsightFace, BLIP/Florence-2, CLIP, and faster-whisper, and exposes the extracted metadata through multimodal search (keyword, fuzzy, semantic, faces, places, transcript, scene-level context).  

The product also supports enterprise integration scenarios inspired by LMS workflows: embeddable player pages, iframe-safe delivery, postMessage progress/completion events, role-based access control, and channel-based permissions. ClipLens was evolved from an LMS-oriented branch and now acts as a media intelligence core that can plug into learning and enterprise portals.  

EssarStream-style requirements are represented through: secure media delivery, structured content organization, channel/event/category taxonomy, analytics dashboards, admin tooling, and integration readiness for enterprise ecosystems. AI chatbot capability (RAG-style "ask your library" over transcripts + visual metadata) is part of the documented roadmap and architecture direction, with current foundations already present through searchable transcripts, embeddings, and indexed metadata.

---

## Table of Contents

Use Word's automatic TOC generation (References -> Table of Contents) so headings auto-update on open.

---

## Chapter 1 - Introduction

### 1.1 Scope

ClipLens focuses on:

- AI-assisted indexing and retrieval for video/photo libraries.
- Enterprise-ready media operations (upload, organization, discovery, moderation, analytics).
- Integration with external systems through embeds and APIs.
- Privacy-preserving deployment (self-hosted, no mandatory third-party AI API dependency).

Out of scope for this repository:

- Full LMS course lifecycle (course builder, grading, learner assessment workflows).
- Fully deployed production chatbot module (planned as next layer using existing search corpus).

### 1.2 Product Scenarios

1. **Content operations team** uploads corporate media and wants fast retrieval by topic, speaker, object, or scene.
2. **L&D / training team** embeds videos into LMS/intranet pages and tracks progress/completion events.
3. **Management users** consume highlights via trending, analytics, and categorized channel pages.
4. **Asset curators** use maps, places, events, and albums for geographically/contextually organized browsing.
5. **Moderation/admin users** control users, categories, storage, deleted items, and command tooling.

### 1.3 System Overview

High-level flow:

1. Upload video/photo.
2. Store asset metadata in PostgreSQL.
3. Queue background processing in Celery.
4. Run AI analysis (labels, faces, captions, embeddings, transcript).
5. Expose searchable and browsable views through web pages and REST APIs.
6. Enable embed-based external consumption (LMS/intranet style integration).

Core subsystems:

- Django monolith (`videos` app)
- PostgreSQL + `pgvector` + `pg_trgm`
- Redis + Celery workers
- FFmpeg processing pipeline
- Server-rendered UI with vanilla JS

---

## Chapter 2 - Background and Requirements

### 2.1 Functional Requirements (FR1-FR6, including EssarStream alignment)

**FR1 - Media Ingestion and Processing**  
System shall support upload and async processing of videos/photos into AI-searchable assets.

**FR2 - Intelligent Retrieval**  
System shall support multimodal search over titles, transcripts, scene descriptions, labels, faces, and semantic embeddings.

**FR3 - Playback and External Embed Integration (EssarStream/LMS style)**  
System shall provide watch and embed modes, including iframe-safe playback and integration events for external platforms.

**FR4 - Content Management and Organization**  
System shall support channels, categories, playlists, albums, events, moments, and geotagged place-based browsing.

**FR5 - Governance and Administration**  
System shall enforce role-based access (`viewer`, `editor`, `superadmin`) with admin panels for users, categories, commands, storage, and recycle bin/trash operations.

**FR6 - Analytics and Enterprise Readiness (EssarStream parity goals)**  
System shall provide operational and engagement insights, API coverage, and scalable extension points for AI assistant/chatbot workflows.

### 2.2 Non-Functional Requirements (NFRs)

- **NFR1 - Performance:** indexed search responses in near real-time for practical dataset sizes.
- **NFR2 - Scalability:** async queue-based architecture for long-running AI operations.
- **NFR3 - Reliability:** persistent task status and recoverable processing lifecycle (`pending/processing/ready/failed`).
- **NFR4 - Security:** session auth, role-based authorization, controlled admin surfaces, configurable iframe origin policies.
- **NFR5 - Maintainability:** monolithic but modular app with clear model-view-template separation and API grouping.
- **NFR6 - Portability:** self-hosted deployment with standard Linux/Mac server toolchain.

### 2.3 Use Cases (5)

**UC1 - Smart Media Search**  
Actor: Viewer/Editor  
Goal: Find exact video moments/photos using text query.  
Outcome: Results grouped across videos, speech, scenes, people, channels, playlists, photos, and places.

**UC2 - LMS/Portal Embed Playback**  
Actor: L&D platform integrator  
Goal: Embed player in external portal and receive progress/completion events.  
Outcome: Embedded playback with postMessage event communication and integration-ready iframe code.

**UC3 - Face/Speaker Intelligence Curation**  
Actor: Editor  
Goal: Review identities, merge/rename and improve retrieval quality for person-based search.  
Outcome: Better people and audio/person mapping across assets.

**UC4 - Place/Event-based Discovery**  
Actor: Operations/Business user  
Goal: Browse media by named places, map view, events, and albums.  
Outcome: Faster contextual navigation and retrieval.

**UC5 - Admin Governance**  
Actor: Superadmin  
Goal: Manage users/roles/categories/storage/commands/trash.  
Outcome: Controlled platform operations and compliance-friendly administration.

---

## Chapter 3 - Methodology and Design

### 3.1 Design Goals

- Build a privacy-first media AI system deployable on controlled infrastructure.
- Balance implementation speed and production practicality with Django monolith architecture.
- Keep retrieval quality high using layered search (FTS + fuzzy + semantic + entity-based).
- Support enterprise embedding and governance from the first implementation cycle.

### 3.2 Architecture (3-tier + AI + EssarStream-style integration)

**Presentation Tier**  

- Django templates (`watch`, `embed`, dashboards, libraries, admin pages)
- Vanilla JS for interactions, player control, filters, and integration widgets

**Application Tier**  

- Django views and REST endpoints for content, search, moderation, analytics, and administration
- Celery task orchestration for heavy AI/video processing

**Data Tier**  

- PostgreSQL relational data
- `pgvector` for embeddings (ANN search)
- `pg_trgm` + FTS for lexical/fuzzy retrieval
- Filesystem media storage for originals, HLS streams, thumbnails, face crops

**AI Layer**  

- YOLO (object detection)
- BLIP/Florence-2 (scene captioning)
- CLIP (semantic vectors)
- InsightFace (face detection/identity)
- faster-whisper (speech-to-text)

**EssarStream/LMS Integration Layer**  

- Embed URLs and iframe snippet generation
- postMessage progress/completion events
- origin controls and embed compatibility hooks

### 3.3 Database Schema Summary

Primary entities:

- `Video`, `Photo`, `Channel`, `Category`
- `VideoFrame`, `VideoSegment`, `Subtitle`
- `FaceIdentity`, `DetectedFace`, `SpeakerIdentity`
- `Playlist`, `Album`, `Event`, `NamedPlace`
- `UserProfile` and role-linked governance tables
- `ActivityLog` (new tracking layer), upscaling-related metadata fields

Schema strategy:

- Relational core for transactional integrity.
- Vector columns for semantic retrieval.
- Indexed text fields for FTS/fuzzy speed.

### 3.4 SCORM/LMS-Compatible Design Notes

While full SCORM package authoring is outside this repository, ClipLens implements key SCORM/LMS-compatible behavior through:

- embeddable player routes
- progress and completion signaling via front-end messaging
- watch-session state tracking
- secure role/session model
- API-driven metadata and status retrieval for external systems

### 3.5 Security Design

- Role-based UI and API guards (`viewer`, `editor`, `superadmin`)
- Split admin routes by privilege level
- Session-authenticated web access
- Configurable embedding policies and safe-origin controls
- Controlled command execution through admin endpoint surfaces

---

## Chapter 4 - Implementation

### 4.1 Development Environment

- Python 3.11
- Django 4.2
- PostgreSQL with `pgvector` and `pg_trgm`
- Redis + Celery workers (`processing`, `captions`, `default`)
- FFmpeg for transcoding/extraction
- JS/CSS template-based frontend

### 4.2 Backend Implementation

Implemented backend areas include:

- media CRUD and ingestion APIs
- background task orchestration for AI pipelines
- analytics/storage/admin services
- speaker/face/place/event/album APIs
- restore/purge/archive lifecycle APIs
- activity logging and upscaling endpoints

### 4.3 Frontend Implementation (including seek-lock/embed/SCORM-style integration)

- Advanced watch page with timeline and metadata panels
- Integration wizard for LMS embed code generation
- iframe embed page optimized for external hosting contexts
- subtitle/transcript interfaces
- map-driven geospatial exploration
- admin dashboards and reusable navigation components

Note: "seek-lock" behavior is implemented as controlled playback state handling in integration-aware player workflows (watch/embed and progress sync logic).

### 4.4 AI Chatbot Layer (Current + Planned)

Current implementation foundations:

- searchable transcripts (`VideoSegment`)
- scene/frame descriptors (`VideoFrame`)
- embeddings (`clip_embedding`) and ANN retrieval
- speaker/identity links for better context grounding

Planned chatbot (RAG) implementation:

- query interpretation over indexed transcript + scene + place metadata
- citation-style responses with timestamp/video references
- enterprise ask-mode for "ask your library" workflows

### 4.5 EssarStream Section Mapping

ClipLens already provides EssarStream-aligned capabilities:

- enterprise media publishing
- searchable training/content repository
- role and governance controls
- embed-friendly consumption
- analytics and admin observability

---

## Chapter 5 - Results and Discussion

### 5.1 Functional Results

- End-to-end media ingestion and AI enrichment operational.
- Unified multimodal search across multiple content signals operational.
- Video/photo DAM and map/place/event workflows operational.
- Face and speaker identity tooling operational.
- Admin governance and storage tooling operational.

### 5.2 Feature Demonstration Highlights

- Search can return speech moments, people matches, scene matches, and visual-semantic results in one query context.
- Embed flow supports portal/LMS integration with generated iframe code.
- Location intelligence enables place detail pages and map marker APIs.
- New activity log and upscaling components expand operational maturity.

### 5.3 Business Impact

- Reduces content retrieval time for teams managing large media archives.
- Improves usability of existing media without manual tagging effort.
- Enables enterprise portal/LMS integration without duplicating storage pipelines.
- Provides governance and role controls suitable for organizational use.

### 5.4 Individual Contribution

Suggested framing for your submission (edit as needed):

- Designed and implemented core Django architecture and API layer.
- Built AI processing pipeline integration across multiple open-source models.
- Implemented multimodal search indexing and retrieval logic.
- Delivered production-style UI surfaces (watch, embed, analytics, admin, map, DAM).
- Added governance, performance-oriented indexing, and deployment-ready workflows.

---

## Chapter 6 - Conclusion

### 6.1 Key Achievements

- Delivered a complete AI-powered media intelligence platform, not just a video player.
- Unified video/photo search, retrieval, and governance in one deployable system.
- Established strong integration path for LMS/enterprise platforms.

### 6.2 Lessons Learned

- Retrieval quality requires tuning multiple signals, not relying on one search method.
- Async pipelines are mandatory for practical AI media workloads.
- Good product outcomes require equal focus on UX, ops, and model integration.

### 6.3 Future Roadmap

- Production-grade RAG chatbot ("ask your library")
- expanded analytics and recommendation workflows
- deeper LMS/SCORM interoperability enhancements
- pagination and advanced ranking improvements for very large datasets
- distributed processing and storage optimization for larger deployments

---

## Annexure A - Full Tech Stack Table


| Layer                | Technology                       | Purpose                             |
| -------------------- | -------------------------------- | ----------------------------------- |
| Web Framework        | Django 4.2                       | Core app, pages, APIs               |
| API                  | Django REST-style views          | Programmatic integrations           |
| Language             | Python 3.11                      | Backend and task logic              |
| Database             | PostgreSQL                       | Relational data store               |
| Vector Search        | pgvector                         | Semantic ANN search                 |
| Fuzzy Search         | pg_trgm                          | Typo-tolerant retrieval             |
| Full Text Search     | PostgreSQL FTS                   | Tokenized indexed keyword search    |
| Queue Broker/Cache   | Redis                            | Celery broker/caching               |
| Task Processing      | Celery                           | Async AI/media jobs                 |
| Video Pipeline       | FFmpeg                           | Transcoding, frame/audio extraction |
| Object Detection     | YOLOv8                           | Per-frame/per-photo labels          |
| Face Recognition     | InsightFace                      | Face embeddings/identity            |
| Scene Captioning     | BLIP / Florence-2                | Visual text descriptions            |
| Semantic Embeddings  | OpenAI CLIP                      | Text-image shared embedding space   |
| Speech-to-Text       | faster-whisper                   | Transcript generation               |
| Image Processing     | Pillow                           | Thumbnails/photo preprocessing      |
| Frontend             | Django Templates + JS            | Web UI                              |
| Deployment Utilities | WhiteNoise / standard WSGI stack | Static delivery/runtime             |


---

## Annexure B - Screenshot Placeholders (B1-B16)

Use these labels in your report. Replace image later with your final screenshots.

**B1 - Home Dashboard (All Videos + Filters)**
B1 - Home Dashboard

**B2 - Watch Page (Player + Metadata + Actions)**
B2 - Watch Page

**B3 - LMS Integration Wizard (Embed Code Generator)**
B3 - LMS Integration Wizard

**B4 - Embed Player View (Iframe Mode)**
B4 - Embed Player View

**B5 - Analytics Dashboard**
B5 - Analytics Dashboard

**B6 - Photo Library (DAM)**
B6 - Photo Library

**B7 - Photo Detail (AI Metadata Panel)**
B7 - Photo Detail

**B8 - Faces/People Management**
B8 - Faces Management

**B9 - Speakers Management**
B9 - Speakers Management

**B10 - Named Places Management**
B10 - Named Places

**B11 - Media Map (Photo + Video Markers)**
B11 - Media Map

**B12 - Event Detail / Event Upload**
B12 - Event Views

**B13 - Album Detail / Shared Album**
B13 - Album Views

**B14 - Admin Panel (Users/Categories/Commands/Storage/Trash)**
B14 - Admin Panel

**B15 - Subtitle/Transcript Editor**
B15 - Subtitle Transcript Editor

**B16 - API Testing Snapshot (Postman/Swagger/cURL Output)**
B16 - API Testing Snapshot

---

## Annexure C - API Endpoint Reference Table

> Base namespace reflects the `videos/urls.py` routing map.


| Module            | Method         | Endpoint                                                                 | Purpose                          |
| ----------------- | -------------- | ------------------------------------------------------------------------ | -------------------------------- |
| Health            | GET            | `/api/health/`                                                           | Service liveness check           |
| Search            | GET            | `/api/search/suggest/`                                                   | Autocomplete suggestions         |
| Search            | GET            | `/api/search/scoped/`                                                    | Scoped/global search results     |
| Channels          | GET/POST       | `/api/channels/`                                                         | List/create channels             |
| Channels          | GET/PUT/DELETE | `/api/channels/<uuid:channel_id>/`                                       | Channel by UUID                  |
| Channels          | GET/POST       | `/api/channels/<uuid:channel_id>/editors/`                               | Manage channel editors           |
| Channels          | DELETE         | `/api/channels/<uuid:channel_id>/editors/<int:user_id>/`                 | Remove editor                    |
| Channels          | POST           | `/api/channels/<slug:slug>/subscribe/`                                   | Subscribe toggle                 |
| Channels          | GET/POST       | `/api/channels/<slug:slug>/links/`                                       | Channel links                    |
| Channels          | GET            | `/api/channels/<slug:slug>/`                                             | Channel by slug                  |
| Channel Links     | GET/PUT/DELETE | `/api/channel-links/<int:link_id>/`                                      | Link detail                      |
| Named Places      | GET/POST       | `/api/named-places/`                                                     | List/create places               |
| Named Places      | GET/PUT/DELETE | `/api/named-places/<int:place_id>/`                                      | Place detail                     |
| Categories        | GET/POST       | `/api/categories/`                                                       | List/create categories           |
| Categories        | GET/PUT/DELETE | `/api/categories/<int:category_id>/`                                     | Category detail                  |
| Moment Categories | GET/POST       | `/api/moment-categories/`                                                | List/create moment categories    |
| Moment Categories | GET/PUT/DELETE | `/api/moment-categories/<int:cat_id>/`                                   | Detail                           |
| Videos            | GET            | `/api/videos/`                                                           | Video listing                    |
| Videos            | POST           | `/api/videos/upload/`                                                    | Upload video                     |
| Videos            | GET/PUT/DELETE | `/api/videos/<uuid:video_id>/`                                           | Video detail/update/delete       |
| Videos            | POST           | `/api/videos/<uuid:video_id>/restore/`                                   | Restore soft-deleted video       |
| Videos            | DELETE         | `/api/videos/<uuid:video_id>/purge/`                                     | Permanent delete                 |
| Videos            | POST           | `/api/videos/<uuid:video_id>/view/`                                      | Record view                      |
| Videos            | GET            | `/api/videos/<uuid:video_id>/status/`                                    | Processing status                |
| Videos            | POST           | `/api/videos/<uuid:video_id>/reprocess/`                                 | Re-run processing                |
| Upscaling         | GET            | `/api/videos/<uuid:video_id>/upscale/presets/`                           | Preset options                   |
| Upscaling         | POST           | `/api/videos/<uuid:video_id>/upscale/`                                   | Start upscaling                  |
| Streaming         | GET            | `/api/videos/<uuid:video_id>/stream/`                                    | HLS playlist/stream              |
| Download          | GET            | `/api/videos/<uuid:video_id>/download/`                                  | Download source                  |
| Videos            | POST           | `/api/videos/<uuid:video_id>/thumbnail/`                                 | Update thumbnail                 |
| Engagement        | POST           | `/api/videos/<uuid:video_id>/like/`                                      | Toggle like                      |
| Engagement        | POST           | `/api/videos/<uuid:video_id>/save/`                                      | Toggle save                      |
| History           | DELETE         | `/api/videos/<uuid:video_id>/history/`                                   | Remove history row               |
| Playback          | POST           | `/api/videos/<uuid:video_id>/progress/`                                  | Update watch progress            |
| Comments          | GET/POST       | `/api/videos/<uuid:video_id>/comments/`                                  | List/create comments             |
| Chapters          | GET/POST       | `/api/videos/<uuid:video_id>/chapters/`                                  | List/create chapters             |
| Moments           | GET/POST       | `/api/videos/<uuid:video_id>/moments/`                                   | List/create moments              |
| End Screens       | GET/POST       | `/api/videos/<uuid:video_id>/end-screens/`                               | List/create end screens          |
| End Screens       | GET/PUT/DELETE | `/api/videos/<uuid:video_id>/end-screens/<int:end_screen_id>/`           | End screen detail                |
| Subtitles         | GET            | `/api/videos/<uuid:video_id>/subtitles/`                                 | List subtitles                   |
| Subtitles         | POST           | `/api/videos/<uuid:video_id>/subtitles/upload/`                          | Upload subtitle file             |
| Subtitles         | POST           | `/api/videos/<uuid:video_id>/subtitles/regenerate/`                      | Regenerate subtitle              |
| Subtitles         | DELETE         | `/api/videos/<uuid:video_id>/subtitles/<int:subtitle_id>/`               | Delete subtitle                  |
| Subtitles         | GET/PUT        | `/api/videos/<uuid:video_id>/subtitles/<int:subtitle_id>/cues/`          | Cue-level operations             |
| Audio Tracks      | GET            | `/api/videos/<uuid:video_id>/audio-tracks/`                              | List audio tracks                |
| Audio Tracks      | POST           | `/api/videos/<uuid:video_id>/audio-tracks/extract/`                      | Extract additional track         |
| Frames            | GET            | `/api/videos/<uuid:video_id>/frames/`                                    | Frame listing                    |
| Frames            | POST           | `/api/videos/<uuid:video_id>/frames/analyze/`                            | Trigger frame analysis           |
| Diarization       | POST           | `/api/videos/<uuid:video_id>/diarize/`                                   | Run speaker diarization          |
| Faces             | GET            | `/api/videos/<uuid:video_id>/faces/`                                     | Video face list                  |
| Faces             | POST           | `/api/videos/<uuid:video_id>/faces/<int:identity_id>/remove/`            | Remove identity from video       |
| Faces             | GET            | `/api/faces/list/`                                                       | Identity list                    |
| Faces             | POST           | `/api/faces/<int:identity_id>/tag/`                                      | Tag identity                     |
| Faces             | POST           | `/api/faces/<int:identity_id>/merge/`                                    | Merge identities                 |
| Faces             | POST           | `/api/faces/<int:identity_id>/rename/`                                   | Rename identity                  |
| Faces             | GET/POST       | `/api/faces/<int:identity_id>/nicknames/`                                | Nickname operations              |
| Faces             | DELETE         | `/api/faces/<int:identity_id>/nicknames/<int:nickname_id>/`              | Delete nickname                  |
| Faces             | DELETE         | `/api/faces/<int:identity_id>/delete/`                                   | Delete identity                  |
| Face Crops        | POST           | `/api/faces/crops/<int:face_id>/status/`                                 | Set face review status           |
| Face Crops        | POST           | `/api/faces/crops/<int:face_id>/set-thumbnail/`                          | Set identity thumbnail           |
| Faces             | POST           | `/api/faces/cleanup-orphans/`                                            | Maintenance cleanup              |
| Faces/Audio       | GET            | `/api/faces/<int:identity_id>/audio/`                                    | Audio-linked face insights       |
| Segments          | POST           | `/api/segments/<int:segment_id>/set-speaker/`                            | Link segment to speaker          |
| Speakers          | GET            | `/api/videos/<uuid:video_id>/speakers/`                                  | Video speaker list               |
| Speakers          | GET            | `/api/speakers/list/`                                                    | Global speaker list              |
| Speakers          | POST           | `/api/speakers/<int:speaker_id>/rename/`                                 | Rename speaker                   |
| Speakers          | POST           | `/api/speakers/<int:speaker_id>/set-role/`                               | Set role                         |
| Speakers          | POST           | `/api/speakers/<int:speaker_id>/link-face/`                              | Link face identity               |
| Speakers          | POST           | `/api/speakers/<int:speaker_id>/suggestions/<int:suggestion_id>/accept/` | Accept suggestion                |
| Speakers          | POST           | `/api/speakers/<int:speaker_id>/suggestions/<int:suggestion_id>/reject/` | Reject suggestion                |
| Speakers          | POST           | `/api/speakers/<int:speaker_id>/merge/`                                  | Merge speaker identities         |
| Speakers          | DELETE         | `/api/speakers/<int:speaker_id>/delete/`                                 | Delete speaker                   |
| Comments          | DELETE         | `/api/comments/<int:comment_id>/`                                        | Delete comment                   |
| Comments          | POST           | `/api/comments/<int:comment_id>/like/`                                   | Toggle comment like              |
| Comments          | POST           | `/api/comments/<int:comment_id>/pin/`                                    | Pin/unpin comment                |
| Chapters          | GET/PUT/DELETE | `/api/chapters/<int:chapter_id>/`                                        | Chapter detail                   |
| Moments           | GET/PUT/DELETE | `/api/moments/<int:moment_id>/`                                          | Moment detail                    |
| Playlists         | GET/POST       | `/api/playlists/`                                                        | List/create playlists            |
| Playlists         | GET/PUT/DELETE | `/api/playlists/<uuid:playlist_id>/`                                     | Playlist detail                  |
| Playlists         | POST/DELETE    | `/api/playlists/<uuid:playlist_id>/videos/<uuid:video_id>/`              | Add/remove video                 |
| Notifications     | GET            | `/api/notifications/`                                                    | List notifications               |
| Notifications     | POST           | `/api/notifications/read/`                                               | Mark notifications read          |
| Admin             | GET/POST       | `/api/admin/users/`                                                      | User management                  |
| Admin             | POST           | `/api/admin/users/<int:user_id>/`                                        | Toggle/manage specific user      |
| Map               | GET            | `/api/media/map-markers/`                                                | Combined photo/video map markers |
| Photos            | GET            | `/api/photos/`                                                           | Photo listing                    |
| Photos            | GET            | `/api/photos/map-markers/`                                               | Photo map markers                |
| Photos            | POST           | `/api/photos/upload/`                                                    | Upload photo                     |
| Photos            | POST/PATCH     | `/api/photos/bulk/`                                                      | Bulk photo operations            |
| Photos            | GET/PUT/DELETE | `/api/photos/<uuid:photo_id>/`                                           | Photo detail/update/delete       |
| Photos            | POST           | `/api/photos/<uuid:photo_id>/restore/`                                   | Restore photo                    |
| Photos            | DELETE         | `/api/photos/<uuid:photo_id>/purge/`                                     | Purge photo                      |
| Photos            | GET            | `/api/photos/<uuid:photo_id>/status/`                                    | Photo processing status          |
| Photos            | POST           | `/api/photos/<uuid:photo_id>/archive/`                                   | Archive toggle                   |
| Photos Upscaling  | GET            | `/api/photos/<uuid:photo_id>/upscale/presets/`                           | Presets                          |
| Photos Upscaling  | POST           | `/api/photos/<uuid:photo_id>/upscale/`                                   | Start upscaling                  |
| Photo Faces       | GET            | `/api/photos/<uuid:photo_id>/faces/`                                     | Photo face list                  |
| Photo Faces       | POST           | `/api/photos/<uuid:photo_id>/faces/<int:identity_id>/remove/`            | Remove face identity             |
| Albums            | GET            | `/api/albums/`                                                           | Album list                       |
| Albums            | GET/PUT/DELETE | `/api/albums/<uuid:album_id>/`                                           | Album detail                     |
| Albums            | GET/POST       | `/api/albums/<uuid:album_id>/photos/`                                    | Album photo operations           |
| Events            | GET/POST       | `/api/events/`                                                           | List/create events               |
| Events            | GET/PUT/DELETE | `/api/events/<int:event_id>/`                                            | Event detail                     |
| Events            | GET            | `/api/events/<int:event_id>/search/`                                     | Event scoped search              |
| Admin Commands    | POST           | `/api/admin/commands/run/`                                               | Execute admin commands           |


---

## Annexure D - Glossary


| Term         | Meaning in ClipLens Context                                                                                |
| ------------ | ---------------------------------------------------------------------------------------------------------- |
| LMS          | Learning Management System; external platform that can embed ClipLens media                                |
| SCORM        | E-learning interoperability model; ClipLens supports integration-like behavior via embed + progress events |
| JWT          | Token-based auth model (not primary in current web flow; session auth is primary)                          |
| RAG          | Retrieval-Augmented Generation; planned AI chatbot mode over indexed media corpus                          |
| EssarStream  | Enterprise media/training workflow alignment target (integration + governance + discoverability)           |
| seek-lock    | Playback control constraint to manage seeking behavior in integration-aware player flows                   |
| iframe embed | Browser mechanism to render ClipLens player inside another site                                            |
| HLS          | HTTP Live Streaming used for adaptive bitrate playback                                                     |
| Celery       | Distributed task queue for async processing                                                                |
| pgvector     | PostgreSQL extension for vector similarity search                                                          |
| pg_trgm      | PostgreSQL extension for trigram fuzzy matching                                                            |
| FTS          | Full Text Search in PostgreSQL                                                                             |
| YOLO         | Real-time object detection model used for frame/photo labels                                               |
| CLIP         | Vision-language model creating shared text-image embeddings                                                |
| InsightFace  | Face detection/recognition toolkit used for identity workflows                                             |
| Whisper      | Speech-to-text model used for transcript generation                                                        |
| NamedPlace   | Geolocation entity to group media by physical locations                                                    |
| DAM          | Digital Asset Management module for photo/media organization                                               |
| ANN          | Approximate nearest neighbor search for fast vector retrieval                                              |
| RBAC         | Role-Based Access Control for permissions/governance                                                       |


---

## Notes for Final Submission

1. Keep your own cover/declaration/acknowledgment pages unchanged.
2. Paste this content from Abstract onward into your Word template.
3. Insert automatic TOC after heading styles are applied.
4. Replace B1-B16 placeholder images with real screenshots before final print/PDF.
5. If supervisor asks for stricter SCORM claims, keep wording as "SCORM-compatible integration behavior" (not full package authoring).

