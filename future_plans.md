# Roadmap

## ✅ Recently shipped (this build)

- Multi-tenant architecture (per-org DB, media folder, subdomain)
- Invite-based onboarding with 7-day tokens
- Plans (free + paid via Stripe subscription)
- Storage addons (recurring monthly via Stripe)
- AI credit packs (one-time, 12-month FIFO expiry)
- Per-tenant usage metering with auto credit-pack draining
- Cancel-at-period-end for all subscriptions
- Stripe Customer Portal integration
- Billing center + invoice history + payment method UI
- Self-service plan upgrade page
- Public marketing landing page (with logo, founder bio, FAQ)
- Privacy Policy + Terms of Service
- Leads inbox for contact form submissions
- Top-up product CRUD for platform owner

---

## 🔜 Up next (high priority)

### Production deployment
- Wildcard SSL via Let's Encrypt DNS-01
- systemd services for all 5 Celery workers
- nginx with HSTS, HTTP/2, security headers
- DNS records (`*.cliplens.com`)
- Pre-warm AI model caches
- See [docs/deployment.md](docs/deployment.md)

### Email notifications
- Onboarding invite email (currently link must be shared manually)
- Payment receipt + failed-payment emails
- Usage warning emails at 80% / 95% (currently only banner)
- Weekly digest for platform owner (new tenants, churned tenants, MRR change)

### White-label per-tenant branding
- Tenant model fields: `brand_name`, `brand_logo`, `brand_color`, `brand_favicon`
- Context processor injects into every template
- Replace ClipLens logo with tenant logo on subdomain pages
- Custom CSS variables for accent colour

### Coupon system (full design in chat history)
- `CouponCode` model: percent/fixed, scope (plan/addon/credits), per-tenant limit, expiry
- Stripe Coupon API integration (sync local rows → Stripe coupons)
- "Have a code?" field on Checkout pages
- Redemption history dashboard for platform owner

---

## 🧭 Mid-term

### Hard quota enforcement
Currently soft — a task that starts at 99% can overshoot. Hard enforcement requires checking quota mid-task and revoking. Worth doing for production fairness.

### Per-tenant API rate limiting
Use `django-ratelimit` keyed by tenant slug. Different limits per plan tier.

### Audit log UI inside org admin
`ActivityLog` rows already exist; just need a viewer at `/admin-panel/activity/`.

### Migrate-all-tenants management command
Loops through every active tenant and runs `migrate --database=freestream_<slug>`. Replaces the manual shell snippet.

### Backup management command
`backup_tenant <slug>` — exports the tenant DB + media tarball to off-server storage.

---

## 🌌 Aspirational

### "Ask your library" — RAG over your media
Natural language questions over the transcript + scene description + face metadata. E.g. "show every clip where John explains the budget" → timestamped answers with frame + transcript citations.

### Auto-highlight reels
One-click "make a 60-second highlight from these search results" with editable cut points. Could output EDL/XML for Premiere/Resolve.

### Decision / action-item extraction from meetings
Parse speech segments for decisions, TODOs, names, dates → render a "Meeting outcomes" panel per video.

### Cross-video storylines
Auto-suggest threads (e.g. "Project Alpha", "Trip to Japan") via recurring faces + places + phrases.

### Interactive timeline heatmap search
Global timeline where searches paint heatmaps across the entire library.

### Auto-privacy modes
Detect faces of minors, screens with sensitive text, documents → auto-blur in previews with click-through.

### Smart collections (saved searches)
Search rules that auto-update as new media arrives, with shareable links.

### Near-duplicate video moment detection
Detect repeated intros, reused B-roll, re-uploads — for de-dup or canonical-source marking.

### Semantic bookmarks + personal notes
Bookmark a timestamp with a note; notes become searchable later.

### Batch curation workflows
"Review 300 hits fast" mode — bulk tag/archive/rename/confirm-faces from any search result.

### "This day last year" notifications
Memory-style resurfacing of older content for personal libraries.

### Export to external NLEs
Generate EDL/XML/FCPXML for Premiere, Resolve, Final Cut.
