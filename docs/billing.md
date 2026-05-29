# Billing & Stripe integration

ClipLens uses Stripe for plan subscriptions, storage add-ons, and AI credit packs. This document covers the pricing model, the integration architecture, and how to operate it.

---

## 1. The pricing model

### Three things customers buy

| Item | Stripe mode | Cancellation behaviour |
|------|-------------|------------------------|
| **Plan** (Starter / Pro / Enterprise) | `subscription` (monthly) | Cancel at period end. Access until next billing date. |
| **Storage addon** (+50 GB / +100 GB / +500 GB) | `subscription` (monthly) | Cancel at period end. GB stays until next billing date. |
| **AI credit pack** (60 min / 300 min / 900 min) | `payment` (one-time) | Non-refundable. Credits expire 12 months after purchase. |

### Effective quotas per tenant

```python
effective_ai_minutes  = plan.ai_minutes_limit + Σ(unconsumed credit packs, not expired)
effective_storage_gb  = plan.storage_limit_gb + Σ(active storage addons)
```

Both totals are computed in real time on every quota check by `tenants.metering.get_monthly_usage()`.

### What "AI minute" actually means

One AI minute = one minute of wall-clock time spent by a Celery worker running an AI task on the
tenant's media. The meter is **hardware-dependent** — the same task takes 5 wall-clock minutes on
a GPU server and 20 minutes on a CPU-only box.

This has design implications for hosted vs self-hosted deployments — read
**[docs/ai-minutes-and-hardware.md](ai-minutes-and-hardware.md)** for the full discussion of
metering models (wall-clock vs normalised units vs flat-fee self-host) and recommended next steps.

User-facing copy lives in:
- `terms.html` section 6 ("How AI minutes are counted")
- Landing page FAQ
- `org_usage.html` "About AI Minutes" card

### How credit packs get drained

After every Celery task completes, `log_ai_minutes()`:

1. Inserts a `UsageEvent` row with the elapsed minutes
2. Sums this month's total usage
3. If total > `plan.ai_minutes_limit`, the overage is drained from credit packs **FIFO** (oldest pack first)
4. `pack.minutes_consumed` is updated lazily — credits are conceptually drained but the row stays so it can be audited

---

## 2. Initial setup

### Get Stripe test keys

1. Sign up at https://dashboard.stripe.com/register (free)
2. Open https://dashboard.stripe.com/test/apikeys
3. Copy the **Secret key** (`sk_test_...`) and **Publishable key** (`pk_test_...`)

### Install Stripe CLI for webhook tunnelling

```bash
brew install stripe/stripe-cli/stripe       # macOS
# or download from https://stripe.com/docs/stripe-cli

stripe login                                # opens browser, authorises CLI
```

### Add to `.env`

```bash
STRIPE_SECRET_KEY=sk_test_...
STRIPE_PUBLISHABLE_KEY=pk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...   # printed by `stripe listen` on first run
```

### Auto-launch the webhook tunnel

`./start.sh` automatically launches `stripe listen --forward-to localhost:8000/api/stripe/webhook/` when `STRIPE_SECRET_KEY` is set. The signing secret it prints on first run goes into `STRIPE_WEBHOOK_SECRET`.

---

## 3. The 10,000-foot integration

### Where things live

| Component | File | Purpose |
|-----------|------|---------|
| Helpers | `tenants/stripe_utils.py` | Checkout sessions, customer portal, webhook handler |
| Settings | `cliplens/settings.py` | `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_ENABLED` |
| Webhook URL | `cliplens/urls.py` | `/api/stripe/webhook/` (CSRF-exempt, signature-verified) |
| Models with Stripe IDs | `tenants/models.py` | `Plan.stripe_price_id`, `Tenant.stripe_customer_id`, `StorageAddon.stripe_subscription_id`, `AICreditPack.stripe_payment_intent_id` |

### Lazy Stripe Product/Price creation

We never pre-create products in Stripe. Instead, the first time a customer tries to buy a plan or top-up, `ensure_stripe_price()` calls `stripe.Product.create()` + `stripe.Price.create()` and saves the IDs back on our model.

If you edit a price in our admin, the next purchase creates a **new** Stripe Price (since Stripe prices are immutable) — old subscriptions stay on their original price.

### Checkout flow

```
User clicks Subscribe / Buy
    ↓
View calls create_checkout_session() / create_plan_checkout_session()
    ↓
ensure_stripe_price[_for_plan]()  — lazy product/price creation
    ↓
stripe.checkout.Session.create()  — with metadata: tenant_slug, kind, product_id
    ↓
Redirect user to Stripe-hosted checkout page
    ↓
User pays
    ↓
Stripe POSTs `checkout.session.completed` to /api/stripe/webhook/
    ↓
handle_webhook_event() — creates StorageAddon / AICreditPack / activates plan
    ↓
User lands on success URL
```

The success page just shows "thanks!" — the actual data was already recorded by the webhook.

---

## 4. Webhook event handling

`tenants/stripe_utils.py::handle_webhook_event()` processes these events:

| Event | What we do |
|-------|------------|
| `checkout.session.completed` (kind=plan) | Save `stripe_customer_id` + `stripe_plan_subscription_id` on Tenant, set `plan_status=active`, activate tenant |
| `checkout.session.completed` (kind=storage) | Create `StorageAddon` row linked to the subscription |
| `checkout.session.completed` (kind=credits) | Create `AICreditPack` row with 12-month expiry |
| `customer.subscription.updated` | Sync `plan_status` (active/past_due/cancelled). If `cancel_at_period_end=True`, set local `expires_at` on storage addon. |
| `customer.subscription.deleted` | Final termination — `expires_at = now()` on addon, `plan_status=cancelled` on tenant |
| `invoice.payment_failed` | Flip tenant to `past_due` |

Idempotency: each event checks for existing rows by `stripe_subscription_id` / `stripe_payment_intent_id` before creating, so Stripe's automatic retries don't duplicate.

### Verifying webhook signatures

When `STRIPE_WEBHOOK_SECRET` is set, `stripe_webhook()` calls `stripe.Webhook.construct_event()` which verifies the `Stripe-Signature` header. If unset (dev mode), events are parsed unverified — **don't ship this to production**.

### StripeObject ≠ dict gotcha

Stripe SDK 15.x returns `StripeObject` instances that look dict-like but don't have `.get()`. The webhook handler calls `event.to_dict()` to normalise to a plain dict at the entry — every key access in handler code uses `.get()` afterwards.

---

## 5. Cancellation lifecycle

### Storage addon cancellation (user clicks "Cancel")

1. View calls `cancel_stripe_subscription(sub_id, at_period_end=True)`
2. Stripe sets `subscription.cancel_at_period_end = True`, returns `current_period_end` (Unix timestamp)
3. View sets local `addon.cancelled_at = now()` and `addon.expires_at = <period_end>`
4. **GB stays in user's effective limit** until `expires_at`
5. UI shows "Cancelled — active until Jun 28, 2026"
6. At period end, Stripe fires `customer.subscription.deleted` → webhook sets `expires_at = now()` → `get_storage_addon_gb()` stops counting it

### Plan cancellation

Same pattern but via `Tenant.stripe_plan_subscription_id`. The customer keeps access until period end; `plan_status` flips to `cancelled` immediately so the UI shows the right state.

### Re-activation

If a user opens the Stripe Customer Portal and clicks "Don't cancel", Stripe fires `customer.subscription.updated` with `cancel_at_period_end=False`. Our webhook clears `cancelled_at` and `expires_at`, addon goes back to fully active.

---

## 6. Customer Portal

Stripe's hosted portal lets users:
- Update payment method
- Download invoices
- Cancel / re-activate subscriptions
- See billing history

`tenants/stripe_utils.py::create_billing_portal_session()` mints a temporary URL. The **"Manage in Stripe"** button on the org billing page links to it.

Configure portal features in Stripe dashboard: https://dashboard.stripe.com/test/settings/billing/portal

---

## 7. Top-up products (managed by platform owner)

Platform owner visits `admin.cliplens.com/topups/` to:
- Create new SKUs (e.g. "+1 TB Storage at $39/mo")
- Set/change price, GB amount, minutes
- Activate/deactivate (controls org-side visibility)
- Delete (existing subscriptions are not affected — `FK on_delete=SET_NULL`)

**Editing an SKU's price never changes existing customer subscriptions** — Stripe Prices are immutable, and our `StorageAddon` and `AICreditPack` rows snapshot the price at purchase time.

---

## 8. Plans (also managed by platform owner)

`admin.cliplens.com/plans/`:
- `price_usd = 0` → free plan (no Stripe checkout during onboarding)
- `price_usd > 0` → paid plan, lazy Stripe Price creation on first subscription

Editing a plan's price affects only **new** subscriptions. Existing tenants stay on their original Stripe Price until they cancel and re-subscribe.

---

## 9. Production checklist

- [ ] Switch to live Stripe keys (`sk_live_...`, `pk_live_...`)
- [ ] Configure webhook endpoint in Stripe dashboard: `https://cliplens.com/api/stripe/webhook/`
- [ ] Copy the live webhook signing secret to `STRIPE_WEBHOOK_SECRET`
- [ ] Configure Customer Portal in Stripe dashboard (allowed update fields, cancellation reason)
- [ ] Set up tax handling (Stripe Tax) if applicable
- [ ] Test failed-payment flow: use card `4000 0000 0000 0341` (fails immediately)
- [ ] Set up Stripe Radar rules for fraud
- [ ] Enable Stripe Receipts (in dashboard settings — emails customers automatically)

---

## 10. Common Stripe test cards

| Card | Behaviour |
|------|-----------|
| `4242 4242 4242 4242` | Always succeeds |
| `4000 0000 0000 0002` | Always declined |
| `4000 0000 0000 9995` | Insufficient funds |
| `4000 0027 6000 3184` | Requires 3D Secure authentication |
| `4000 0000 0000 0341` | Card attached succeeds, first charge fails (great for testing failed renewals) |

Use any future expiry date and any 3-digit CVC.

---

## 11. Troubleshooting

| Symptom | Cause |
|---------|-------|
| Webhook returns 500 with `'str' object has no attribute 'get'` | StripeObject not normalised. Make sure `event.to_dict()` is called at the top of `handle_webhook_event` |
| `checkout.session.completed` fires but no addon/pack appears | Check `metadata.tenant_slug` matches an existing tenant slug |
| User stuck on `plan_status=incomplete` | Webhook tunnel not running. Start `stripe listen` and retry: `stripe events resend <evt_id>` |
| Stripe retries fire repeatedly | Earlier failures retry for 3 days. Restart `stripe listen` to drop the backlog. |
| Customer Portal "Add payment method" button missing | Configure allowed updates in Stripe dashboard → Settings → Customer Portal |
| Subscription cancelled but GB still shown | `expires_at` controls visibility, not `cancelled_at`. Wait for the next webhook tick. |
