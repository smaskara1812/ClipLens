# ClipStream — Product & Feature Overview

**Audience:** Leadership, stakeholders, and technical decision-makers evaluating the platform.

**One-line pitch:** A **self-hosted video platform** that combines adaptive streaming with **on-device AI**—so teams can upload, search, and share video **without sending content to third-party cloud APIs.**

---

## Strategic value

| Theme | What it means for the business |
|--------|--------------------------------|
| **Data sovereignty** | Video and derived metadata stay on infrastructure you control. |
| **Discoverability** | Multi-modal search (speech, objects, scenes, faces, semantic “visual meaning”) turns archives into something you can actually query. |
| **Familiar UX** | Channel pages, watch experience, playlists, and embeds mirror patterns users already know from consumer video sites. |
| **Integration-ready** | Embeddable player with optional origin allowlists supports LMS and intranet use cases. |

---

## Core video platform

- **Upload & processing** — Upload videos; the system transcodes to **HLS** with multiple quality rungs (from mobile-friendly up through 4K when source allows).
- **Adaptive streaming** — Viewers get bitrate-appropriate playback via standard HLS delivery.
- **Watch experience** — Full watch pages with player controls, progress, and deep links to specific moments.
- **Embeds** — Dedicated **embed URLs** (parallel to “watch” URLs) for iframe use in portals and LMS tools; optional **Content-Security-Policy frame-ancestors** to restrict which sites may embed.
- **Reprocessing** — API support to **re-run analysis** when pipelines or models change.

---

## AI-powered discovery (all local)

Search merges several signals so one query can surface **moments** across the library—with **timestamp chips** that jump playback to the right second.

| Capability | User-facing benefit |
|------------|---------------------|
| **Speech search** | Find what was **said** (automatic transcription with timestamps; supports many languages via Whisper-class models). |
| **Object / label search** | Find frames where **objects** were detected (e.g. vehicles, devices, people as classes). |
| **Scene description search** | Find content described in **natural language** per frame (configurable caption models). |
| **Semantic visual search (CLIP)** | Find scenes by **meaning** even when exact words never appear in captions (e.g. “celebration” vs. a literal caption). |
| **People & faces** | Detect faces, cluster identities within a video, **tag and merge** people, and **match across videos** to reuse names when the same person appears again. |

**Privacy note:** These models run **on your hardware**; there is **no requirement** to call external AI APIs for these features.

---

## Accessibility & language

- **Closed captions** — Auto-generated subtitles (WebVTT) tied to the player, with options to **upload** or **regenerate** subtitles via API.
- **Optional audio tracks** — API surface for **additional audio tracks** and extraction workflows where needed.

---

## Channels, content organization, and engagement

- **Channels** — Creator-style channels with branding (e.g. avatar, banner), **subscriptions**, and **editor roles** for shared management.
- **Categories** — Curated groupings for browse and organization; admin tooling for category management.
- **Playlists** — User and editorial playlists with shareable playlist pages.
- **Social layer** — **Comments** (with likes and pinning), **likes** on videos, **notifications**, and **trending** surfacing.
- **End screens & chapters** — Video **chapter markers** and **end-screen** cards for structured navigation and follow-on content.

---

## Personalization & retention

- **Watch history** and **resume progress** — Pick up where viewers left off.
- **Saved videos** — Bookmarks for later.
- **Analytics** — Dedicated **analytics** area for usage-oriented insight (watch-time oriented data model in the architecture).

---

## Administration & governance

- **User management** — Web **admin panel** paths plus APIs for user-oriented admin tasks (e.g. enable/disable users where implemented).
- **Category administration** — Manage taxonomy from the admin UI.
- **Configurable analysis** — Operators can tune or disable heavy steps (e.g. frame analysis, CLIP, face pipeline, Whisper size) via environment settings for cost/performance tradeoffs.

---

## APIs & extensibility

- **REST APIs** — Broad coverage for videos, channels, playlists, comments, faces, frames, subtitles, notifications, and admin—suitable for **custom frontends**, automation, or integration with internal tools.
- **Health endpoint** — Simple **health check** for monitoring and load balancers.

---

## Technical foundation (for IT / security briefings)

- **Stack:** Django, Django REST Framework, Celery, Redis, MySQL (configurable), FFmpeg, HLS.
- **Deployment model:** Self-hosted; async workers handle encoding, transcription, and analysis queues.
- **Authentication:** Session-based web login (typical for single-domain browser apps); embed behavior documented separately for LMS scenarios.

---

## Suggested talking points for exec meetings

1. **“YouTube-like UX, enterprise-style control”** — Familiar surfaces (watch, channel, playlist, embed) with data staying on your stack.
2. **“Search is the product”** — The differentiator is not just playback but **finding the right clip** via speech, visuals, objects, and people.
3. **“No mandatory cloud AI spend”** — Models are local; cost is mostly **compute and storage you already provision**.
4. **“LMS and portal friendly”** — Embeds plus optional **origin allowlists** align with corporate web security expectations.

---

*For implementation detail (pipelines, models, env vars), see `ARCHITECTURE.md`.*
