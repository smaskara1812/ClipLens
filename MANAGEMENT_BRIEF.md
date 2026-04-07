# ClipStream — Executive Overview & Demo Guide

> A presentation brief for management. Non-technical overview of capabilities,
> business value, and a suggested walkthrough for a live demonstration.

---

## What Is ClipStream?

ClipStream is an **internal AI-powered media intelligence platform** — a private,
self-hosted system for uploading, organising, searching, and extracting insights
from your organisation's video and photo library.

Think of it as a combination of:
- **YouTube** — for your internal videos (HLS streaming, chapters, subtitles)
- **Google Photos** — for your photo library (AI tagging, face recognition, albums)
- **Enterprise search** — find anything said, shown, or depicted in any asset

All data stays **on your own infrastructure**. No content leaves your servers.

---

## The Problem It Solves

| Before ClipStream | With ClipStream |
|-------------------|-----------------|
| Videos stored in shared drives with no searchability | Every spoken word, face, and object is searchable |
| Finding a clip requires watching entire recordings | Jump directly to the exact moment in any video |
| Photo libraries are manually tagged or not tagged at all | AI automatically labels objects, describes scenes, identifies people |
| No visibility into who appears in which content | Face recognition across all videos and photos with named identities |
| Subtitles require expensive manual transcription | Auto-generated subtitles with a built-in editor |

---

## Key Capabilities

### 1. Intelligent Video Search
Upload a video and within minutes you can search it by:
- **Spoken words** — "Find every moment someone says 'quarterly targets'"
- **Visual objects** — "Show me all frames with a whiteboard"
- **Scene descriptions** — "Meeting room with projector screen"
- **Semantic intent** — "Someone presenting financial data" (AI understands meaning, not just keywords)

### 2. Photo Library with AI Intelligence
Every uploaded photo is automatically:
- Labelled with detected objects (people, vehicles, products, animals, etc.)
- Given a natural-language scene description ("three people having a discussion at a table")
- Matched against known faces from your video library

### 3. Face Recognition Across All Media
- Name a person once in a video → they are automatically identified in all future videos and photos
- Search "Show me all content featuring [name]" across the entire library instantly
- View a timeline of appearances per person

### 4. Channels & Access Control
- Organise content into channels (departments, projects, events)
- Role-based access: Superadmin → Editor → Viewer
- Channel-level editors for decentralised content management
- Public / private / subscribers-only visibility per asset

### 5. Photo Albums
- Create collections manually or let the system create **Smart Albums** automatically
  - "All photos labelled 'product launch'"
  - "All photos featuring the sales team"
- Share albums via a private link

### 6. Live Subtitle Editing
Videos get auto-generated subtitles. Editors can refine them in a built-in web editor
and republish — no external tools required.

---

## Suggested Live Demo Flow (~15 minutes)

### Step 1 — Home Feed (1 min)
Open the home page. Show the video grid. Point out:
- Categories and channel filters
- The search bar

### Step 2 — AI Search Demo (4 min)
Type a keyword into the search bar (use a word that appears in a video title AND
is spoken inside a video). Show:
- **Title matches** — videos whose title/description matches
- **Speech tab** — exact timestamps where the word was spoken; click a result to jump there in the video
- **Scenes tab** — video frames where the word appears as a visual object or scene description
- **CLIP semantic search** — toggle "AI Search" and search for a phrase; results are conceptually related

### Step 3 — Video Player (2 min)
Open a video. Show:
- HLS adaptive streaming
- Chapter markers / subtitle track
- "Related videos" sidebar

### Step 4 — Photo Library (3 min)
Navigate to Photos. Show:
- The date-grouped timeline view
- AI labels on photo cards ("dog", "outdoor", "meeting")
- Search for an object (e.g. "laptop") — photos appear instantly
- Click a photo → scene description, detected objects, and identified people shown

### Step 5 — Face Recognition Demo (2 min)
Navigate to People (or search a person's name). Show:
- All video moments featuring that person
- All photos where that person appears
- "This identity was named once; the system found them everywhere automatically"

### Step 6 — Album Feature (1 min)
Open Albums. Show a Smart Album for a label (e.g. "all photos from project X").
Show the share link feature.

### Step 7 — Channel Management (1 min)
Show the channel page with Videos/Photos tabs.
Point out the analytics dashboard briefly.

### Step 8 — Upload Flow (1 min)
Show the upload page. Point out:
- Drag-and-drop for videos and photos
- Channel/category assignment
- "Processing begins automatically after upload"

---

## Technical Highlights (Simplified)

| Capability | How it works (plain English) |
|-----------|------------------------------|
| Speech search | Every spoken word in a video is transcribed and stored; searched via full-text index |
| Object detection | AI model (YOLO) scans every 5 seconds of video and identifies objects in frame |
| Scene description | AI model (BLIP/Florence-2) writes a sentence describing what it sees in each frame |
| Semantic / CLIP search | Image and text are encoded into the same mathematical space; searching by meaning rather than keywords |
| Face recognition | Each face is converted to a mathematical fingerprint; faces are matched across all content |
| Fast search at scale | Specialised database indexes (pgvector HNSW) enable millisecond nearest-neighbour search across millions of embeddings |

---

## Data Privacy & Security

- **Fully self-hosted** — all AI processing runs on your own servers. No API calls to third-party AI services.
- **On-premise database** — all media, metadata, embeddings, and user data stays within your network.
- **Role-based access control** — granular permissions at the user, channel, and asset level.
- **Private / URL-only / Subscribers-only** visibility controls per asset.

---

## Deployment Overview

| Component | What it does |
|-----------|-------------|
| Django (Python web framework) | Serves the web application and REST API |
| PostgreSQL + pgvector | Stores all metadata and AI embeddings |
| Redis | Background task queue |
| Celery workers | Run AI processing jobs asynchronously after uploads |
| FFmpeg | Converts uploaded videos to HLS streaming format |
| Nginx | Serves static files and HLS video segments |

The system can run on a single server or scale horizontally with separate
web, processing, and database nodes.

---

## Current Status

| Area | Status |
|------|--------|
| Video upload + HLS pipeline | Production-ready |
| Speech transcription + search | Production-ready |
| YOLO object detection | Production-ready |
| Scene description (BLIP/Florence-2) | Production-ready |
| CLIP semantic search | Production-ready |
| Face recognition + identity management | Production-ready |
| Photo library + AI analysis | Production-ready |
| Albums + Smart Albums | In development |
| EXIF timeline / GPS map | Planned |
| Mobile app | Not started |

---

## Competitive Positioning

| Feature | ClipStream | Google Photos | Microsoft Stream | SharePoint |
|---------|-----------|--------------|-----------------|------------|
| Self-hosted / on-premise | ✅ | ❌ | ❌ | Partial |
| Speech search in videos | ✅ | ❌ | ✅ (limited) | ❌ |
| Object/scene search | ✅ | ✅ (cloud only) | ❌ | ❌ |
| Semantic AI search | ✅ | Partial | ❌ | ❌ |
| Face recognition | ✅ | ✅ (cloud only) | ❌ | ❌ |
| Cross-video + cross-photo face search | ✅ | ❌ | ❌ | ❌ |
| Custom channels + RBAC | ✅ | ❌ | ✅ | ✅ |
| No data leaves your network | ✅ | ❌ | ❌ | Partial |

---

*ClipStream is built on open-source foundations and runs entirely on your infrastructure.*
*All AI models are open-weight and run locally — no subscription fees, no API costs.*
