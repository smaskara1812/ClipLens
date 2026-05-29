# How AI minutes work — and the hardware-dependence problem

This document explains how the AI-minute meter works, the **real problem** it has on variable hardware, and how to design around it for hosted vs self-hosted deployments.

---

## 1. What the meter actually measures

`tenants/celery_utils.py` connects two Celery signals:

- `task_prerun` → record `start_ts = time.monotonic()`
- `task_postrun` (and `task_failure`) → `elapsed = time.monotonic() - start_ts`
- Write a `UsageEvent` row to the control DB with `value=elapsed_minutes`, `event_type=<mapped>`

So **one AI minute = one wall-clock minute of a Celery worker doing the task**. Not a minute of input video. Not a minute of CPU time. Wall-clock, real-world, "this process was running for 60 seconds".

That's a deliberate choice — it directly reflects compute consumed — but it has consequences.

---

## 2. The hardware-dependence problem

The same task on the same input produces wildly different AI-minute counts depending on the worker's hardware.

### Empirical example (rough)

For a 10-minute 1080p video with the full pipeline (HLS encode + Whisper-small + InsightFace + YOLO + CLIP):

| Hardware | Total wall-clock | AI minutes logged |
|----------|------------------|-------------------|
| Apple M2 Pro (10-core, no GPU) | ~12 min | 12 |
| Linux x86, 8 vCPU, no GPU | ~20 min | 20 |
| Linux + 1× NVIDIA T4 GPU | ~5 min | 5 |
| Linux + 1× NVIDIA A100 GPU | ~2.5 min | 2.5 |

The customer doing the upload sees only the AI-minute number. They have no way to know whether the high count is because (a) the video is complex, (b) our hardware is slow, or (c) they should buy a bigger plan.

### Why this matters

- **Pricing legitimacy** — "you used 25 minutes" feels arbitrary if a competing service would have logged 5
- **Migration pain** — if we ever change hardware, every customer's monthly burn rate changes overnight
- **Self-host confusion** — a customer's own slow server would burn through quotas in a week, but they're providing the compute

---

## 3. How to design around it

There are three reasonable models. Pick one (or implement all three as plan-tier choices).

### Model A — "wall-clock minutes on standard hardware" (what we have now)

Keep wall-clock metering but commit publicly to a **standard hardware tier per plan** and publish typical throughput numbers.

**Pros:**
- Already implemented
- Honest reflection of compute used
- Plays well with Stripe metered billing if we want to bill per-minute later

**Cons:**
- Locks us into a specific hardware floor — upgrading silicon means giving customers more for free
- Self-host case is awkward (limits don't apply)

**Implementation notes:**
- Document the hardware tier per plan publicly (e.g. "Pro ships on n2-standard-4 with one T4 GPU")
- If we change hardware, send a 30-day notice and adjust quotas proportionally
- Add a "your worker's current speed: X min compute per min of input" indicator on the usage page so customers see the rate

### Model B — "normalised AI units" (better for product)

Stop reporting wall-clock minutes to customers. Instead, define an abstract "AI unit" = the amount of compute needed to process 1 minute of standard 1080p video through the full pipeline on a reference machine. Internally we still measure wall-clock but apply a per-worker calibration factor.

```python
# Pseudo-code
ELAPSED_PER_AI_UNIT_SECONDS = 30   # calibrated per worker
ai_units_consumed = wall_clock_seconds / ELAPSED_PER_AI_UNIT_SECONDS
log_ai_minutes(slug, minutes=ai_units_consumed, ...)
```

**Pros:**
- Customer-facing number is stable across hardware changes
- We can upgrade silicon without giving away free quota
- Pricing comparisons across plans become apples-to-apples

**Cons:**
- More plumbing
- Calibration must be maintained per worker (auto-benchmark on startup?)
- Doesn't fully solve self-host (we'd still need a calibration constant per customer's hardware)

### Model C — "compute-neutral pricing" for self-host (recommended for enterprise)

Keep wall-clock metering for hosted SaaS (Model A or B). For self-hosted enterprise licences, **drop the AI minute quota entirely** and charge a flat platform licence fee instead.

- Hosted: monthly subscription + AI minute allowance + top-up credits
- Self-hosted: annual platform licence per user/seat or per organisation, unlimited compute, customer pays for their own hardware

This is what almost every enterprise B2B platform does. Snowflake, Datadog, Segment, etc. all have a "BYOC" / on-prem tier that doesn't bill per query because the customer's hardware bill is already the cost signal.

---

## 4. What we have today (and what to do next)

### Today

- Model A in production for hosted
- Self-host model is undefined — `MULTI_TENANT=true` works on-prem but the AI minute limit still gets enforced (which makes no sense for self-host)
- We do **not** yet publish the standard hardware tier per plan

### Recommended next steps (~half a day of work)

1. **Add a `Plan.deployment_mode` field**: `'hosted'` vs `'self_host'`
2. **In `check_quota` and `log_ai_minutes`**: skip enforcement / logging entirely when the tenant is on a self-host plan
3. **In the Plans admin UI**: split into two tabs — Hosted plans (with AI/storage limits) vs Self-host plans (with feature flags only)
4. **Add a `processing_speed_label` to each hosted plan** (e.g. "GPU-accelerated", "Standard CPU") and surface it on the plans page and FAQ
5. **Add a "your average rate" indicator on the org usage page**: divide actual AI minutes logged this month by minutes of video uploaded — gives customers a concrete sense of efficiency
6. **In `docs/billing.md`**: document the hardware tier promise per hosted plan; revise this doc once you've decided on Model A vs B

### Longer term

- Implement Model B if customer feedback says the variable rate feels arbitrary
- Add an "ai-units-per-second benchmark" command (`./manage.py benchmark_worker`) so self-host customers can sanity-check their throughput

---

## 5. Q&A talking points (when customers ask)

| Question | Answer |
|----------|--------|
| "Why did my 5-minute video use 18 AI minutes?" | "Wall-clock processing time. Whisper alone takes 30-60% of input duration on CPU. Frame analysis runs on every Nth frame and takes 2-5× the input duration. Translation to 8 languages adds another ~10 minutes per language for a 5-min video. We publish typical processing rates on the Plans page — your plan ships on [hardware tier]." |
| "Will my AI minute usage change if you upgrade your servers?" | "Yes. We send 30-day notice before any change and adjust monthly allowances proportionally so your effective cost stays stable." |
| "Can I bring my own GPU?" | "Yes — self-hosted enterprise deployments use your hardware exclusively and don't have AI minute limits. Contact us for pricing." |
| "Why do limits exist for self-hosting?" | (After the fix above) "They don't. Self-hosted licences are flat-fee and unlimited on compute." |
| "How do I make my videos process faster?" | "Upgrade to a GPU plan, or pre-trim long videos to only the bits you care about. The biggest savings come from skipping translation if you don't need multilingual captions." |

---

## 6. Code references

- Metering entry: `tenants/metering.py::log_ai_minutes`
- Signal wiring: `tenants/celery_utils.py::_on_task_postrun`, `_on_task_failure`
- Quota check: `tenants/metering.py::check_quota`, called from `videos/views.py` upload paths
- Event types: `tenants/celery_utils.py::_TASK_EVENT_TYPES`
- User-facing copy: `tenants/templates/tenants/terms.html#ai-minutes`, `landing.html` FAQ
