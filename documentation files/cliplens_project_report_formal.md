# ClipLens Project Report (Formal Academic Version)

## Cover Page
Use your existing approved MUJ cover page format.

## Declaration
Use your existing approved declaration page.

## Acknowledgments
Use your existing approved acknowledgments page.

---

## Abstract

ClipLens is a self-hosted media intelligence platform developed to address the practical challenge of searching large video and image repositories in organizational environments. Traditional media systems generally support metadata-based browsing and basic keyword matching; however, they do not provide deep semantic discovery across speech, faces, objects, and visual context. ClipLens extends this capability by combining a Django-based application layer, asynchronous AI processing pipelines, and indexed search over relational and vectorized representations.

The system supports end-to-end media ingestion, HLS-based streaming, multimodal analysis, and unified retrieval. During processing, each asset is transformed into searchable representations using object detection, face recognition, scene captioning, speech transcription, and vector embeddings. Search results are consolidated into meaningful user-facing categories such as videos, speech moments, people, scenes, channels, playlists, photos, and places.

From an enterprise perspective, ClipLens is designed to be integration-ready for LMS and intranet ecosystems through embeddable player routes, iframe-safe delivery behavior, and progress/completion signaling patterns. The current implementation also lays the groundwork for a retrieval-augmented AI chatbot by storing structured transcript and visual context that can be cited in future conversational responses. In this way, the project demonstrates a practical bridge between media management, enterprise learning workflows, and applied AI.

---

## Table of Contents

Generate automatically in MS Word after applying heading styles (References -> Table of Contents).

---

## Chapter 1 - Introduction

### 1.1 Project Context and Need

Organizations generate large amounts of media content for training, communication, operations, and documentation. Although storage and playback have become easy, discovery remains difficult. Users often know what they want to find conceptually, but not exact filenames, upload dates, or manually assigned tags. This gap creates operational inefficiency, especially in learning and development workflows where teams frequently need specific moments from long recordings.

ClipLens was conceived to solve this discoverability problem by transforming raw media into machine-interpretable and user-searchable knowledge. Instead of behaving only as a video hosting interface, it operates as a searchable intelligence layer over organizational media.

### 1.2 Scope

The scope of this project includes:
- ingestion and processing of videos and photos
- AI-assisted metadata extraction and indexing
- multimodal search and retrieval interfaces
- role-based governance and admin tooling
- enterprise-style integration readiness (LMS/embed scenarios)

The project does not fully implement LMS academic workflows such as course authoring, grading, or SCORM package lifecycle management. However, it intentionally supports LMS-compatible integration behavior through embedding and event signaling.

### 1.3 Product Scenarios

ClipLens is intended for multi-stakeholder use:
- content teams searching and reusing archival footage
- L&D teams embedding media in training portals
- supervisors monitoring usage trends via analytics surfaces
- editors managing identities, tags, places, and media organization
- superadmins controlling governance and storage operations

### 1.4 System Overview

The architecture follows a practical monolithic pattern: Django serves pages and APIs, PostgreSQL stores structured and indexed data, Redis/Celery executes asynchronous AI workloads, and FFmpeg handles media transformation. The user-facing experience is delivered through server-rendered templates enhanced with JavaScript interactions for filtering, playback, and integration workflows.

---

## Chapter 2 - Background, Requirements, and Use Cases

### 2.1 Functional Requirements

**FR1: Media ingestion and processing**  
The system shall accept video and photo uploads, queue background processing, and update status lifecycle transitions until assets are ready for consumption.

**FR2: AI-assisted metadata generation**  
The system shall generate searchable metadata including speech transcripts, object labels, scene descriptions, identities, and semantic embeddings.

**FR3: Multimodal retrieval**  
The system shall support query resolution across lexical, fuzzy, semantic, and entity-oriented dimensions in a unified search workflow.

**FR4: Enterprise/LMS-compatible delivery**  
The system shall support watch and embed routes with integration-friendly behaviors such as iframe delivery and progress/completion signaling patterns.

**FR5: Governance and role management**  
The system shall enforce access rules across viewer, editor, and superadmin roles for both page-level and operational endpoints.

**FR6: Content organization and business utility (EssarStream-aligned)**  
The system shall provide channels, playlists, albums, events, map/place navigation, analytics, and admin controls to support production deployment scenarios.

### 2.2 Non-Functional Requirements

**Performance:** index-backed retrieval for practical real-time user interactions.  
**Reliability:** recoverable asynchronous processing with persistent task state.  
**Security:** role-constrained surfaces and session-authenticated access model.  
**Scalability:** queue-driven processing suitable for increasing media volumes.  
**Maintainability:** structured monolith with centralized model and routing layers.  
**Deployment portability:** self-hosted operation with standard server components.

### 2.3 Use Cases

1. **Cross-modal search:** user enters a conceptual query and receives timestamped results from speech/scenes/people/photos.  
2. **Embedded learning playback:** L&D portal embeds player and receives completion events.  
3. **Identity curation:** editor merges and renames face/speaker identities to improve retrieval quality.  
4. **Location-driven discovery:** user navigates media through named places and map interfaces.  
5. **Administrative control:** superadmin manages users, categories, storage, command execution, and deleted assets.

---

## Chapter 3 - Methodology and System Design

### 3.1 Design Philosophy

The design approach prioritized operational realism over architectural complexity. Rather than a fragmented microservice model, a Django monolith was selected to reduce orchestration overhead and accelerate iteration. This choice proved suitable because the majority of complexity in the platform arises from AI/media workflows, not from distributed business logic.

### 3.2 Three-Tier + AI + Integration Architecture

**Presentation tier:** template-driven pages for watch, embed, search, analytics, DAM, map, and admin views.  
**Application tier:** Django view/API layer plus Celery task orchestration for heavy processing.  
**Data tier:** PostgreSQL relational storage with full-text, trigram, and vector indexes.

An additional AI processing layer executes model inference and writes machine-generated knowledge back into indexed fields. A separate integration layer enables external systems (including LMS-like platforms) to consume ClipLens content safely and consistently.

### 3.3 Database and Indexing Strategy

The schema combines transactional entities (`Video`, `Photo`, `Channel`, `Playlist`, `Event`, `NamedPlace`) with analysis entities (`VideoFrame`, `VideoSegment`, `FaceIdentity`, `DetectedFace`, `SpeakerIdentity`). This hybrid model supports both product workflows and search depth.

For retrieval performance, three complementary index families are used:
- full-text search indexes for lexical relevance
- trigram indexes for typo-tolerant matching
- vector indexes for semantic similarity retrieval

### 3.4 SCORM/LMS Compatibility Perspective

Although ClipLens is not positioned as a complete SCORM package authoring tool, it includes important LMS-compatible interaction mechanisms:
- stable embed URLs
- iframe integration support
- playback progress and completion communication
- API-accessible metadata/status endpoints

This architecture allows ClipLens to act as a robust media intelligence backend inside broader educational ecosystems.

### 3.5 Security Methodology

Security is implemented through role-aware route design, privilege-separated admin surfaces, and authenticated session flows. Editor-level and superadmin-only operations are explicitly segmented. Integration flows are designed with configurable origin behavior so organizations can tighten embed policies based on deployment security standards.

---

## Chapter 4 - Implementation Details

### 4.1 Development Environment

Implementation was completed using Python 3.11, Django 4.2, PostgreSQL, Redis, Celery, and FFmpeg. The frontend relies on Django templates and JavaScript for interactive controls and responsive media workflows.

### 4.2 Backend Implementation

The backend includes page routes and REST-style endpoints for content ingestion, retrieval, curation, analytics, and administration. The processing pipeline is decoupled through Celery queues to avoid blocking user interactions during heavy AI workloads. Additional operational functionality includes restore/purge flows, media upscaling initiation, activity logging, and identity management APIs.

### 4.3 Frontend Implementation

The frontend emphasizes user productivity for media operations:
- dashboard-style media discovery
- advanced watch interfaces
- dedicated embed experiences
- subtitle/transcript editing flows
- map and place-driven navigation
- admin panels with role-aware navigation

Integration-specific UX is also present through an embed wizard that helps external systems consume player URLs and related parameters.

### 4.4 AI and Search Implementation

ClipLens uses a staged AI pipeline:
- object detection for visual labels
- face detection/identity clustering
- scene caption generation
- speech-to-text transcript extraction
- vector embedding generation for semantic retrieval

Search combines these outputs with lexical and fuzzy indexing, producing robust retrieval behavior across both structured and unstructured content.

### 4.5 EssarStream-Aligned Capability Mapping

From an enterprise deployment viewpoint, the implemented modules map well to EssarStream-style expectations:
- centralized media publishing
- searchable content intelligence
- embed-ready consumption model
- governance and role controls
- analytics and administrative observability

---

## Chapter 5 - Results, Difficulties Faced, and Mitigation

### 5.1 Functional Results Achieved

The project successfully achieved end-to-end ingestion, asynchronous analysis, and multimodal retrieval. All major workflow surfaces required for day-to-day operations are present, including content publishing, intelligent search, media organization, identity curation, map/place navigation, and administrative governance.

### 5.2 Difficulties Faced and How They Were Overcome

#### Difficulty 1: Search noise and inconsistent relevance

Early versions produced noisy results due to different text characteristics across fields (short labels vs long transcripts). A single fuzzy threshold was insufficient for all contexts.  
**Mitigation:** field-specific retrieval tuning was adopted, with selective fuzzy application and stricter thresholds where short-token noise occurred. Semantic thresholds were also calibrated differently for frames and photos.

#### Difficulty 2: Processing latency in AI-heavy workflows

Running multiple inference stages over long videos introduced substantial processing time and potential UI waiting issues.  
**Mitigation:** asynchronous queue execution with status polling and clear state transitions was implemented to separate user experience from background compute cost.

#### Difficulty 3: Synchronizing player behavior for external embedding

Delivering a consistent embedded experience while supporting external progress/completion handling required careful front-end event wiring.  
**Mitigation:** dedicated embed route behavior and structured message signaling were implemented, along with configurable integration parameters.

#### Difficulty 4: Data model complexity from multimodal features

Combining video, photos, faces, speakers, events, places, and transcripts in one schema increased relationship complexity.  
**Mitigation:** entity responsibilities were clarified across domain models, and indexing was aligned with actual query patterns to preserve response quality.

#### Difficulty 5: Operational governance in a fast-evolving codebase

As features expanded, privilege boundaries risked becoming inconsistent across UI and APIs.  
**Mitigation:** explicit role gates and admin-surface separation were enforced to maintain least-privilege behavior.

#### Difficulty 6: Balancing product breadth with academic timeline

The project timeline required simultaneous development of core architecture, AI integration, and user-facing polish.  
**Mitigation:** implementation was prioritized by high-impact vertical slices (ingest -> process -> search -> consume), ensuring each stage reached functional completeness before adding optional enhancements.

### 5.3 Business and Academic Impact

ClipLens demonstrates practical value by reducing media retrieval effort and making existing content reusable at scale. Academically, the project validates applied understanding of web architecture, asynchronous systems, AI integration, database indexing, and secure enterprise workflows in a single cohesive implementation.

### 5.4 Individual Contribution

The project reflects end-to-end ownership across architecture, backend APIs, AI pipeline integration, UI implementation, and operational/admin tooling. This includes both engineering execution and retrieval-quality tuning based on empirical behavior.

---

## Chapter 6 - Conclusion and Future Roadmap

### 6.1 Conclusion

ClipLens successfully evolved into a full media intelligence platform capable of serving both operational and integration-oriented use cases. The implemented system goes beyond basic streaming by enabling meaningful discovery across speech, scenes, semantics, identities, and location context.

### 6.2 Key Learnings

The most important lesson is that successful AI products require system-level thinking: model inference alone is not enough without indexing strategy, workflow design, governance, and UX clarity. Another major takeaway is that asynchronous architecture is essential when AI processing latency is non-trivial.

### 6.3 Future Work

Planned future improvements include:
- production RAG chatbot ("ask your library")
- expanded analytics and recommendation intelligence
- deeper SCORM/LMS interoperability
- advanced pagination/ranking for very large libraries
- distributed processing and storage optimization

---

## Annexure A - Full Tech Stack

| Layer | Technology | Purpose |
|---|---|---|
| Backend | Django 4.2, Python 3.11 | Core web application and APIs |
| Database | PostgreSQL | Transactional and indexed storage |
| Vector Engine | pgvector | Semantic similarity retrieval |
| Fuzzy Search | pg_trgm | Typo-tolerant text matching |
| Full-text | PostgreSQL FTS | Indexed lexical retrieval |
| Queue | Celery + Redis | Asynchronous processing |
| Media Engine | FFmpeg | HLS and extraction pipeline |
| AI Models | YOLO, InsightFace, BLIP/Florence-2, CLIP, faster-whisper | Multimodal analysis |
| Frontend | Django templates + JS | User interfaces and interactions |
| Imaging | Pillow | Photo preprocessing and thumbnails |

---

## Annexure B - Screenshot Placeholders (B1-B16)

Use the following labels in your Word report and replace images manually.

**B1 - Home Dashboard (All Videos + Filters)**  
![B1](/Users/sohammaskara/.cursor/projects/Users-sohammaskara-Desktop-Freestream/assets/Screenshot_2026-04-20_at_2.31.19_PM-8aa740b3-4c06-437c-b079-3c9ebfbc1892.png)

**B2 - Watch Page (Player + Metadata + Actions)**  
![B2](/Users/sohammaskara/.cursor/projects/Users-sohammaskara-Desktop-Freestream/assets/Screenshot_2026-04-20_at_2.31.19_PM-8aa740b3-4c06-437c-b079-3c9ebfbc1892.png)

**B3 - LMS Integration Wizard**  
![B3](/Users/sohammaskara/.cursor/projects/Users-sohammaskara-Desktop-Freestream/assets/Screenshot_2026-04-20_at_2.31.19_PM-8aa740b3-4c06-437c-b079-3c9ebfbc1892.png)

**B4 - Embed Player View**  
![B4](/Users/sohammaskara/.cursor/projects/Users-sohammaskara-Desktop-Freestream/assets/Screenshot_2026-04-20_at_2.31.19_PM-8aa740b3-4c06-437c-b079-3c9ebfbc1892.png)

**B5 - Analytics Dashboard**  
![B5](/Users/sohammaskara/.cursor/projects/Users-sohammaskara-Desktop-Freestream/assets/Screenshot_2026-04-20_at_2.31.19_PM-8aa740b3-4c06-437c-b079-3c9ebfbc1892.png)

**B6 - Photo Library (DAM)**  
![B6](/Users/sohammaskara/.cursor/projects/Users-sohammaskara-Desktop-Freestream/assets/Screenshot_2026-04-20_at_2.31.19_PM-8aa740b3-4c06-437c-b079-3c9ebfbc1892.png)

**B7 - Photo Detail (AI Metadata)**  
![B7](/Users/sohammaskara/.cursor/projects/Users-sohammaskara-Desktop-Freestream/assets/Screenshot_2026-04-20_at_2.31.19_PM-8aa740b3-4c06-437c-b079-3c9ebfbc1892.png)

**B8 - Faces/People Management**  
![B8](/Users/sohammaskara/.cursor/projects/Users-sohammaskara-Desktop-Freestream/assets/Screenshot_2026-04-20_at_2.31.19_PM-8aa740b3-4c06-437c-b079-3c9ebfbc1892.png)

**B9 - Speakers Management**  
![B9](/Users/sohammaskara/.cursor/projects/Users-sohammaskara-Desktop-Freestream/assets/Screenshot_2026-04-20_at_2.31.19_PM-8aa740b3-4c06-437c-b079-3c9ebfbc1892.png)

**B10 - Named Places Management**  
![B10](/Users/sohammaskara/.cursor/projects/Users-sohammaskara-Desktop-Freestream/assets/Screenshot_2026-04-20_at_2.31.19_PM-8aa740b3-4c06-437c-b079-3c9ebfbc1892.png)

**B11 - Media Map**  
![B11](/Users/sohammaskara/.cursor/projects/Users-sohammaskara-Desktop-Freestream/assets/Screenshot_2026-04-20_at_2.31.19_PM-8aa740b3-4c06-437c-b079-3c9ebfbc1892.png)

**B12 - Event Views**  
![B12](/Users/sohammaskara/.cursor/projects/Users-sohammaskara-Desktop-Freestream/assets/Screenshot_2026-04-20_at_2.31.19_PM-8aa740b3-4c06-437c-b079-3c9ebfbc1892.png)

**B13 - Album Views**  
![B13](/Users/sohammaskara/.cursor/projects/Users-sohammaskara-Desktop-Freestream/assets/Screenshot_2026-04-20_at_2.31.19_PM-8aa740b3-4c06-437c-b079-3c9ebfbc1892.png)

**B14 - Admin Panel**  
![B14](/Users/sohammaskara/.cursor/projects/Users-sohammaskara-Desktop-Freestream/assets/Screenshot_2026-04-20_at_2.31.19_PM-8aa740b3-4c06-437c-b079-3c9ebfbc1892.png)

**B15 - Subtitle/Transcript Editor**  
![B15](/Users/sohammaskara/.cursor/projects/Users-sohammaskara-Desktop-Freestream/assets/Screenshot_2026-04-20_at_2.31.19_PM-8aa740b3-4c06-437c-b079-3c9ebfbc1892.png)

**B16 - API Test/Response Snapshot**  
![B16](/Users/sohammaskara/.cursor/projects/Users-sohammaskara-Desktop-Freestream/assets/Screenshot_2026-04-20_at_2.31.19_PM-8aa740b3-4c06-437c-b079-3c9ebfbc1892.png)

---

## Annexure C - API Endpoint Reference

A complete endpoint reference is available in the project report companion file:  
`documentation files/cliplens_project_report_structured.md` (Annexure C).

You can copy that full table directly into this formal version to keep this chapter concise and avoid duplication errors.

---

## Annexure D - Glossary

| Term | Description |
|---|---|
| LMS | Learning Management System for course/training delivery |
| SCORM | E-learning content interoperability model |
| JWT | Token-based authentication mechanism |
| RAG | Retrieval-Augmented Generation for citation-aware chatbot responses |
| EssarStream | Enterprise-aligned media and learning integration context |
| seek-lock | Controlled seeking behavior in guided/integration player flows |
| iframe | Embedded browser frame for portal/LMS content rendering |
| HLS | HTTP Live Streaming protocol for adaptive playback |
| Celery | Asynchronous distributed task queue |
| pgvector | PostgreSQL extension for vector storage and similarity search |
| pg_trgm | PostgreSQL trigram extension for fuzzy matching |
| FTS | Full-text indexed lexical search |
| YOLO | Real-time object detection model family |
| CLIP | Vision-language embedding model |
| InsightFace | Face analysis and recognition toolkit |
| Whisper | Speech-to-text model family |
| DAM | Digital Asset Management |
| RBAC | Role-Based Access Control |

---

## Final Formatting Notes

1. Keep cover/declaration/acknowledgment pages as your existing approved content.
2. Apply heading styles before generating the TOC.
3. Replace all B1-B16 placeholders with final screenshots.
4. If required by your supervisor, append the full Annexure C table from the structured version into this formal report.
