"""
Stripe integration helpers
──────────────────────────
Handles:
  • Lazy creation of Stripe Product + Price for each TopUpProduct
  • Building Checkout Sessions for storage subscriptions and credit packs
  • Resolving session results into StorageAddon / AICreditPack rows

When STRIPE_ENABLED is False (no STRIPE_SECRET_KEY in .env), all functions
no-op so the mock-purchase flow keeps working unchanged.
"""

import logging
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def _stripe():
    """Lazy import + return the configured stripe module, or None when disabled."""
    if not getattr(settings, 'STRIPE_ENABLED', False):
        return None
    import stripe
    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


# ── Product / Price sync ──────────────────────────────────────────────────────

def ensure_stripe_price(product) -> Optional[str]:
    """
    Make sure a Stripe Product + Price exists for this TopUpProduct.
    Stores stripe_product_id / stripe_price_id on the model.
    Returns the Stripe Price ID (or None if Stripe disabled).
    """
    s = _stripe()
    if s is None:
        return None
    if product.stripe_price_id:
        return product.stripe_price_id

    from .models import TopUpProduct

    # Storage = recurring monthly subscription; Credits = one-off payment
    recurring = product.kind == TopUpProduct.KIND_STORAGE

    stripe_product = s.Product.create(
        name=f"ClipLens — {product.name}",
        metadata={
            'topup_product_id': str(product.pk),
            'kind':             product.kind,
            'amount':           str(product.amount),
        },
    )

    price_kwargs = dict(
        product=stripe_product.id,
        unit_amount=int(product.price_usd * 100),   # cents
        currency='usd',
        metadata={'topup_product_id': str(product.pk)},
    )
    if recurring:
        price_kwargs['recurring'] = {'interval': 'month'}

    stripe_price = s.Price.create(**price_kwargs)

    product.stripe_product_id = stripe_product.id
    product.stripe_price_id   = stripe_price.id
    product.save(using='control', update_fields=['stripe_product_id', 'stripe_price_id'])

    logger.info("Stripe: created product=%s price=%s for TopUpProduct %s",
                stripe_product.id, stripe_price.id, product.pk)
    return stripe_price.id


def ensure_stripe_price_for_plan(plan) -> Optional[str]:
    """
    Lazily create a Stripe Product + Price for a Plan (recurring monthly).
    Returns the Stripe Price ID, or None if Stripe disabled or plan is free.
    """
    s = _stripe()
    if s is None or plan.is_free:
        return None
    if plan.stripe_price_id:
        return plan.stripe_price_id

    stripe_product = s.Product.create(
        name=f"ClipLens Plan — {plan.name}",
        metadata={'plan_id': str(plan.pk)},
    )
    stripe_price = s.Price.create(
        product=stripe_product.id,
        unit_amount=int(plan.price_usd * 100),
        currency='usd',
        recurring={'interval': 'month'},
        metadata={'plan_id': str(plan.pk)},
    )
    plan.stripe_product_id = stripe_product.id
    plan.stripe_price_id   = stripe_price.id
    plan.save(using='control', update_fields=['stripe_product_id', 'stripe_price_id'])
    logger.info("Stripe: created plan price=%s for Plan %s", stripe_price.id, plan.pk)
    return stripe_price.id


def create_plan_checkout_session(*, tenant, plan, success_url: str, cancel_url: str,
                                 customer_email: str = '') -> Optional[str]:
    """
    Build a Stripe Checkout Session for subscribing to (or upgrading to) a paid Plan.
    Reuses an existing Stripe Customer if the tenant already has one.
    """
    s = _stripe()
    if s is None or plan.is_free:
        return None
    price_id = ensure_stripe_price_for_plan(plan)
    if not price_id:
        return None

    session_kwargs = dict(
        mode='subscription',
        line_items=[{'price': price_id, 'quantity': 1}],
        success_url=success_url + '?session_id={CHECKOUT_SESSION_ID}',
        cancel_url=cancel_url,
        client_reference_id=f'plan:{tenant.slug}:{plan.pk}',
        metadata={
            'tenant_slug': tenant.slug,
            'tenant_id':   str(tenant.pk),
            'plan_id':     str(plan.pk),
            'kind':        'plan',
        },
        subscription_data={
            'metadata': {
                'tenant_slug': tenant.slug,
                'plan_id':     str(plan.pk),
                'kind':        'plan',
            },
        },
    )
    if tenant.stripe_customer_id:
        session_kwargs['customer'] = tenant.stripe_customer_id
    elif customer_email:
        session_kwargs['customer_email'] = customer_email

    session = s.checkout.Session.create(**session_kwargs)
    logger.info("Stripe: created plan checkout session=%s tenant=%s plan=%s",
                session.id, tenant.slug, plan.pk)
    return session.url


def cancel_stripe_subscription(subscription_id: str, at_period_end: bool = True) -> Optional[dict]:
    """
    Cancel a Stripe subscription.

    at_period_end=True (default): subscription stays active until the end of
      the current paid period, then auto-cancels. User keeps what they paid for.
    at_period_end=False: immediate cancellation, no refund.

    Returns the subscription dict (with `current_period_end`) on success,
    or None on failure / Stripe disabled.
    """
    s = _stripe()
    if s is None or not subscription_id:
        return None
    try:
        if at_period_end:
            sub = s.Subscription.modify(subscription_id, cancel_at_period_end=True)
            logger.info("Stripe: subscription %s set to cancel at period end (%s)",
                        subscription_id, sub.get('current_period_end'))
        else:
            sub = s.Subscription.delete(subscription_id)
            logger.info("Stripe: subscription %s cancelled immediately", subscription_id)
        return sub.to_dict() if hasattr(sub, 'to_dict') else sub
    except Exception as exc:
        logger.exception("Stripe: failed to cancel subscription %s: %s", subscription_id, exc)
        return None


def create_billing_portal_session(*, tenant, return_url: str) -> Optional[str]:
    """Open a Stripe Customer Portal session for managing payment method, invoices."""
    s = _stripe()
    if s is None or not tenant.stripe_customer_id:
        return None
    try:
        session = s.billing_portal.Session.create(
            customer=tenant.stripe_customer_id,
            return_url=return_url,
        )
        return session.url
    except Exception as exc:
        logger.exception("Stripe: billing portal failed: %s", exc)
        return None


def list_tenant_invoices(tenant, limit: int = 20) -> list:
    """Return recent Stripe invoices for a tenant's Customer."""
    s = _stripe()
    if s is None or not tenant.stripe_customer_id:
        return []
    try:
        resp = s.Invoice.list(customer=tenant.stripe_customer_id, limit=limit)
        result = []
        for inv in resp.auto_paging_iter() if hasattr(resp, 'auto_paging_iter') else resp.data:
            result.append({
                'id':          inv.id,
                'number':      getattr(inv, 'number', '') or '',
                'amount_paid': (inv.amount_paid or 0) / 100,
                'currency':    (inv.currency or 'usd').upper(),
                'status':      getattr(inv, 'status', ''),
                'created':     getattr(inv, 'created', 0),
                'pdf_url':     getattr(inv, 'invoice_pdf', '') or '',
                'hosted_url':  getattr(inv, 'hosted_invoice_url', '') or '',
            })
            if len(result) >= limit:
                break
        return result
    except Exception as exc:
        logger.exception("Stripe: list invoices failed: %s", exc)
        return []


def get_payment_method_summary(tenant) -> Optional[dict]:
    """Return last4 / brand of the default payment method, if any."""
    s = _stripe()
    if s is None or not tenant.stripe_customer_id:
        return None
    try:
        cust = s.Customer.retrieve(tenant.stripe_customer_id,
                                   expand=['invoice_settings.default_payment_method'])
        pm = (cust.invoice_settings or {}).get('default_payment_method') \
             if isinstance(cust.invoice_settings, dict) else getattr(cust.invoice_settings, 'default_payment_method', None)
        if not pm:
            return None
        card = pm.get('card') if isinstance(pm, dict) else getattr(pm, 'card', None)
        if not card:
            return None
        return {
            'brand':     (card.get('brand') if isinstance(card, dict) else card.brand) or '',
            'last4':     (card.get('last4') if isinstance(card, dict) else card.last4) or '',
            'exp_month': (card.get('exp_month') if isinstance(card, dict) else card.exp_month) or '',
            'exp_year':  (card.get('exp_year') if isinstance(card, dict) else card.exp_year) or '',
        }
    except Exception as exc:
        logger.exception("Stripe: payment method fetch failed: %s", exc)
        return None


# ── Checkout session ──────────────────────────────────────────────────────────

def create_checkout_session(
    *,
    tenant,
    product,
    success_url: str,
    cancel_url: str,
    customer_email: str = '',
) -> Optional[str]:
    """
    Build a Stripe Checkout Session for purchasing one TopUpProduct.
    Returns the hosted checkout URL, or None if Stripe is disabled.
    """
    s = _stripe()
    if s is None:
        return None

    price_id = ensure_stripe_price(product)
    if not price_id:
        return None

    from .models import TopUpProduct
    mode = 'subscription' if product.kind == TopUpProduct.KIND_STORAGE else 'payment'

    session = s.checkout.Session.create(
        mode=mode,
        line_items=[{'price': price_id, 'quantity': 1}],
        success_url=success_url + '?session_id={CHECKOUT_SESSION_ID}',
        cancel_url=cancel_url,
        customer_email=customer_email or None,
        client_reference_id=f'{tenant.slug}:{product.pk}',
        metadata={
            'tenant_slug':       tenant.slug,
            'tenant_id':         str(tenant.pk),
            'topup_product_id':  str(product.pk),
            'kind':              product.kind,
            'amount':            str(product.amount),
            'price_usd':         str(product.price_usd),
        },
        subscription_data={
            'metadata': {
                'tenant_slug':      tenant.slug,
                'topup_product_id': str(product.pk),
            }
        } if mode == 'subscription' else {},
    )
    logger.info("Stripe: created Checkout session=%s for tenant=%s product=%s",
                session.id, tenant.slug, product.pk)
    return session.url


# ── Webhook handler ───────────────────────────────────────────────────────────

def handle_webhook_event(event) -> dict:
    """
    Process a verified Stripe webhook event and update local DB rows.

    `event` may be a stripe.Event (StripeObject) when signature-verified,
    or a plain dict when parsed without verification. Normalise to dict.

    Supported events:
      checkout.session.completed     → create StorageAddon or AICreditPack
      customer.subscription.deleted  → mark StorageAddon cancelled

    Returns a result dict for logging / debugging.
    """
    from .models import Tenant, TopUpProduct, StorageAddon, AICreditPack

    # Normalise: Stripe SDK returns StripeObject (no .get()). .to_dict() in the
    # current SDK already produces a fully-nested plain dict tree, so we use it
    # when available and fall back to a no-op when the event is already a dict.
    if hasattr(event, 'to_dict'):
        event = event.to_dict()

    event_type = event.get('type', '')
    data       = (event.get('data') or {}).get('object') or {}
    logger.info("Stripe webhook received: %s id=%s", event_type, data.get('id'))

    if event_type == 'checkout.session.completed':
        meta        = data.get('metadata') or {}
        tenant_slug = meta.get('tenant_slug')
        kind        = meta.get('kind', '')

        if not tenant_slug:
            return {'ok': False, 'reason': 'missing-tenant'}

        try:
            tenant = Tenant.objects.using('control').get(slug=tenant_slug)
        except Tenant.DoesNotExist:
            return {'ok': False, 'reason': 'tenant-missing'}

        # Persist the Stripe Customer ID so we can reuse it for future checkouts.
        cust_id = data.get('customer') or ''
        if cust_id and tenant.stripe_customer_id != cust_id:
            tenant.stripe_customer_id = cust_id
            tenant.save(using='control', update_fields=['stripe_customer_id'])

        # ── Plan subscription ──────────────────────────────────────────────
        if kind == 'plan':
            from .models import Plan
            plan_id = meta.get('plan_id')
            try:
                plan = Plan.objects.using('control').get(pk=int(plan_id))
            except Plan.DoesNotExist:
                return {'ok': False, 'reason': 'plan-missing'}
            sub_id = data.get('subscription') or ''
            tenant.plan                       = plan
            tenant.stripe_plan_subscription_id = sub_id
            tenant.plan_status                = Tenant.PLAN_STATUS_ACTIVE
            tenant.is_active                  = True
            tenant.save(using='control', update_fields=[
                'plan', 'stripe_plan_subscription_id', 'plan_status', 'is_active',
            ])
            logger.info("Stripe: tenant %s subscribed to plan %s sub=%s",
                        tenant_slug, plan.name, sub_id)
            return {'ok': True, 'kind': 'plan', 'tenant': tenant_slug}

        # ── Top-up purchase ────────────────────────────────────────────────
        product_id  = meta.get('topup_product_id')
        if not product_id:
            return {'ok': False, 'reason': 'missing-product'}

        try:
            product = TopUpProduct.objects.using('control').get(pk=int(product_id))
        except TopUpProduct.DoesNotExist:
            return {'ok': False, 'reason': 'product-missing'}

        if product.kind == TopUpProduct.KIND_STORAGE:
            # Subscription created — record StorageAddon
            sub_id = data.get('subscription') or ''
            # De-duplicate by stripe_subscription_id
            existing = StorageAddon.objects.using('control').filter(
                stripe_subscription_id=sub_id
            ).first() if sub_id else None
            if existing:
                logger.info("Stripe: subscription %s already recorded", sub_id)
                return {'ok': True, 'reason': 'already-recorded'}
            StorageAddon.objects.using('control').create(
                tenant=tenant,
                product=product,
                gb_amount=product.amount,
                price_usd=product.price_usd,
                stripe_subscription_id=sub_id,
            )
            logger.info("Stripe: created StorageAddon for tenant=%s sub=%s", tenant_slug, sub_id)
            return {'ok': True, 'kind': 'storage', 'tenant': tenant_slug}

        else:   # KIND_CREDITS
            pi_id = data.get('payment_intent') or ''
            existing = AICreditPack.objects.using('control').filter(
                stripe_payment_intent_id=pi_id
            ).first() if pi_id else None
            if existing:
                return {'ok': True, 'reason': 'already-recorded'}
            AICreditPack.objects.using('control').create(
                tenant=tenant,
                product=product,
                minutes_purchased=product.amount,
                price_usd=product.price_usd,
                expires_at=timezone.now() + timedelta(days=365),
                stripe_payment_intent_id=pi_id,
            )
            logger.info("Stripe: created AICreditPack for tenant=%s pi=%s", tenant_slug, pi_id)
            return {'ok': True, 'kind': 'credits', 'tenant': tenant_slug}

    elif event_type == 'customer.subscription.deleted':
        sub_id = data.get('id')

        # Storage addon — paid period is OVER, fully expire it now
        addon = StorageAddon.objects.using('control').filter(
            stripe_subscription_id=sub_id
        ).first()
        if addon:
            now = timezone.now()
            if not addon.cancelled_at:
                addon.cancelled_at = now
            addon.expires_at = now   # forces is_active=False on next read
            addon.save(using='control')
            logger.info("Stripe: terminated StorageAddon for sub=%s", sub_id)
            return {'ok': True, 'kind': 'addon-cancellation'}

        # Plan subscription cancellation
        tenant = Tenant.objects.using('control').filter(
            stripe_plan_subscription_id=sub_id
        ).first()
        if tenant:
            tenant.plan_status = Tenant.PLAN_STATUS_CANCELLED
            tenant.save(using='control', update_fields=['plan_status'])
            logger.info("Stripe: cancelled plan subscription for tenant=%s", tenant.slug)
            return {'ok': True, 'kind': 'plan-cancellation'}

    elif event_type == 'invoice.payment_failed':
        # Mark the tenant past-due if it was a plan invoice
        sub_id = data.get('subscription') or ''
        tenant = Tenant.objects.using('control').filter(
            stripe_plan_subscription_id=sub_id
        ).first()
        if tenant:
            tenant.plan_status = Tenant.PLAN_STATUS_PAST_DUE
            tenant.save(using='control', update_fields=['plan_status'])
            return {'ok': True, 'kind': 'plan-past-due'}

    elif event_type == 'customer.subscription.updated':
        sub_id              = data.get('id')
        status              = data.get('status', '')
        cancel_at_period_end = data.get('cancel_at_period_end', False)
        period_end          = data.get('current_period_end')

        # ── StorageAddon: cancel_at_period_end → set expires_at locally ─────
        addon = StorageAddon.objects.using('control').filter(
            stripe_subscription_id=sub_id
        ).first()
        if addon:
            from datetime import datetime, timezone as dt_tz
            changed = []
            if cancel_at_period_end:
                if not addon.cancelled_at:
                    addon.cancelled_at = timezone.now()
                    changed.append('cancelled_at')
                if period_end:
                    new_exp = datetime.fromtimestamp(period_end, tz=dt_tz.utc)
                    if addon.expires_at != new_exp:
                        addon.expires_at = new_exp
                        changed.append('expires_at')
            else:
                # Re-activation — user clicked "Don't cancel" in Stripe portal
                if addon.cancelled_at or addon.expires_at:
                    addon.cancelled_at = None
                    addon.expires_at   = None
                    changed.extend(['cancelled_at', 'expires_at'])
            if changed:
                addon.save(using='control', update_fields=changed)
                return {'ok': True, 'kind': 'addon-update', 'fields': changed}

        # ── Plan subscription: sync plan_status ─────────────────────────────
        tenant = Tenant.objects.using('control').filter(
            stripe_plan_subscription_id=sub_id
        ).first()
        if tenant and status:
            mapping = {
                'active':              Tenant.PLAN_STATUS_ACTIVE,
                'past_due':            Tenant.PLAN_STATUS_PAST_DUE,
                'canceled':            Tenant.PLAN_STATUS_CANCELLED,
                'unpaid':              Tenant.PLAN_STATUS_PAST_DUE,
                'incomplete':          Tenant.PLAN_STATUS_INCOMPLETE,
                'incomplete_expired':  Tenant.PLAN_STATUS_CANCELLED,
                'trialing':            Tenant.PLAN_STATUS_ACTIVE,
            }
            new_status = mapping.get(status, tenant.plan_status)
            if new_status != tenant.plan_status:
                tenant.plan_status = new_status
                tenant.save(using='control', update_fields=['plan_status'])
                return {'ok': True, 'kind': 'plan-status-change', 'status': new_status}

    return {'ok': True, 'reason': 'ignored'}
