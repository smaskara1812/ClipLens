# API reference

ClipLens exposes a session-authenticated REST API for the per-tenant app and a few public endpoints. Everything is scoped to whichever subdomain you call.

> **Authentication**: Most endpoints require a logged-in session cookie. Hit `/login/` first. An API key system exists (`APIKey` model) for external integrations but isn't yet bound to all endpoints — coming in a future release.

---

## Public endpoints (no auth)

### `GET /`
Marketing landing page (when called on bare `cliplens.com`). When called on a tenant subdomain it forwards to `/player/`.

### `GET /privacy/` · `GET /terms/`
Static legal pages.

### `POST /contact/`
Submit the contact form. Creates a `LeadRequest` row on the control DB.

```bash
curl -X POST https://cliplens.com/contact/ \
  -d "name=Jane Doe" \
  -d "email=jane@acme.com" \
  -d "company=Acme" \
  -d "message=Interested in a demo" \
  -d "csrfmiddlewaretoken=<token>"
```

### `GET /api/health/`
Health check for uptime monitors. Returns `{"status": "ok"}`.

### `POST /api/stripe/webhook/`
Stripe webhook receiver. Signature-verified via `STRIPE_WEBHOOK_SECRET`.

---

## Onboarding endpoints (no login required)

### `GET /onboard/<token>/`
Onboarding form for an invited org admin. Shows password fields + plan selection.

### `POST /onboard/<token>/`
Claims the invite. Free plan → activate + redirect to login. Paid plan → redirect to Stripe Checkout.

### `GET /onboard/<token>/success/`
Stripe Checkout success URL for paid onboarding.

---

## Tenant app endpoints

All under the tenant subdomain (`<slug>.cliplens.com`). Require session auth.

### Media

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Search + library view (`player_page`) |
| `POST` | `/api/videos/upload/` | Upload a video (multipart) |
| `GET` | `/api/videos/<id>/status/` | Polling endpoint for processing status |
| `DELETE` | `/api/videos/<id>/` | Soft-delete a video |
| `POST` | `/api/photos/upload/` | Upload a photo |
| `GET` | `/api/videos/<id>/stream/` | HLS manifest (public for embed flow) |

### Search

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/?q=<query>` | Multi-pass search across video, transcripts, frames, faces, photos, places |
| `GET` | `/api/search-suggest/?q=<prefix>` | Autocomplete suggestions |

### Subscriptions & billing (org admin only)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin-panel/usage/` | Current month's AI minutes + storage |
| `GET` | `/admin-panel/topup/` | Browse + buy storage addons / credit packs |
| `POST` | `/admin-panel/topup/buy/` | Initiate a top-up (Stripe Checkout or mock) |
| `POST` | `/admin-panel/topup/cancel-addon/<id>/` | Cancel a storage addon (at period end) |
| `POST` | `/admin-panel/topup/expire-pack/<id>/` | Voluntarily expire a credit pack |
| `GET` | `/admin-panel/billing/` | Full billing center |
| `POST` | `/admin-panel/billing/cancel-plan/` | Cancel the current plan subscription |
| `GET` | `/admin-panel/billing/portal/` | Redirect to Stripe Customer Portal |
| `GET` | `/admin-panel/plan/` | Plan comparison + upgrade buttons |
| `POST` | `/admin-panel/plan/change/` | Switch to a different plan |

### Admin pages (superadmin role)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/admin-panel/` | User management |
| `GET` | `/admin-panel/categories/` | Manage video/photo categories |
| `GET` | `/admin-panel/commands/` | Run management commands from the UI |
| `GET` | `/admin-panel/storage/` | Per-video disk usage breakdown |
| `GET` | `/admin-panel/deleted/` | Trash (soft-deleted items) |
| `GET` | `/django-admin/` | Django admin (per-tenant data) |

---

## Control plane endpoints

Under `admin.cliplens.com`. Require `is_platform_owner=True`.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Dashboard: all tenants + usage |
| `GET` | `/tenants/new/` | Provision a new org (invite mode) |
| `GET` | `/tenants/<id>/` | Tenant detail: users, usage, active subs, recent events |
| `POST` | `/tenants/<id>/toggle/` | Activate / deactivate |
| `POST` | `/tenants/<id>/change-plan/` | Force change a tenant's plan (no Stripe) |
| `GET` | `/plans/` | Manage plan definitions |
| `GET` | `/topups/` | Manage top-up products (storage SKUs, credit packs) |
| `GET` | `/leads/` | Contact form inbox |
| `GET` | `/api/usage/<tenant_id>/` | JSON: daily usage for charts |

---

## Webhook payload schemas

### `checkout.session.completed` (we receive from Stripe)

Relevant fields we inspect:

```json
{
  "type": "checkout.session.completed",
  "data": {
    "object": {
      "id": "cs_test_abc...",
      "customer": "cus_abc...",
      "subscription": "sub_abc...",
      "payment_intent": "pi_abc...",
      "metadata": {
        "tenant_slug": "acme",
        "topup_product_id": "1",
        "kind": "storage|credits|plan",
        "plan_id": "2"
      }
    }
  }
}
```

`metadata` is what we set on the Checkout Session ourselves — that's how the webhook knows which tenant + SKU to attribute the purchase to.

---

## Future API additions

Not yet built:
- API key authentication for headless integrations
- Webhook outbound endpoints (push events to a customer's URL when a video finishes processing)
- Bulk upload API
- Per-tenant API rate limiting
