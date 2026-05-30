"""
Tenant models — all stored in the 'control' (freestream_control) database.
These are the core of the multi-tenant architecture:
  Plan    — subscription tier with limits
  Tenant  — one per customer org, maps to its own PostgreSQL database
  UsageEvent — metering row logged after each AI task
"""

from django.db import models
from django.contrib.auth.models import User


class Plan(models.Model):
    """
    Subscription tier. Tenants are assigned one plan.
    Zero values mean unlimited (e.g. max_videos=0 → unlimited videos).
    price_usd = 0 → free plan (no Stripe checkout required during onboarding).
    """
    name = models.CharField(max_length=80, unique=True)
    storage_limit_gb = models.PositiveIntegerField(default=100)
    ai_minutes_limit = models.PositiveIntegerField(default=300)
    max_users = models.PositiveIntegerField(default=3)
    max_videos = models.PositiveIntegerField(default=0, help_text="0 = unlimited")
    price_usd = models.DecimalField(max_digits=8, decimal_places=2, default=0,
                                    help_text="Monthly price. 0 = free.")
    # Stripe (created lazily on first paid use)
    stripe_product_id = models.CharField(max_length=120, blank=True, default='')
    stripe_price_id   = models.CharField(max_length=120, blank=True, default='')

    class Meta:
        app_label = 'tenants'
        ordering = ['price_usd', 'name']

    @property
    def is_free(self) -> bool:
        return self.price_usd == 0

    def __str__(self):
        return f"{self.name} (${self.price_usd}/mo)" if self.price_usd else self.name


class Tenant(models.Model):
    """
    One row per customer organisation.
    slug       → subdomain  (orgA.cliplens.local)
    db_name    → PostgreSQL database name  (freestream_orga)
    media_folder → relative path inside MEDIA_ROOT  (tenants/orga/)
    """
    slug = models.SlugField(max_length=60, unique=True)
    name = models.CharField(max_length=200)
    db_name = models.CharField(max_length=100, unique=True)
    media_folder = models.CharField(max_length=200)  # e.g. "tenants/orga/"
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name='tenants',
                             null=True, blank=True,
                             help_text="Chosen by org admin during onboarding")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Contact info (optional)
    admin_email = models.EmailField(blank=True)

    # Stripe — subscription state for the plan itself
    stripe_customer_id        = models.CharField(max_length=120, blank=True, default='')
    stripe_plan_subscription_id = models.CharField(max_length=120, blank=True, default='')
    PLAN_STATUS_NONE       = 'none'           # free plan or not yet subscribed
    PLAN_STATUS_ACTIVE     = 'active'
    PLAN_STATUS_PAST_DUE   = 'past_due'
    PLAN_STATUS_CANCELLED  = 'cancelled'
    PLAN_STATUS_INCOMPLETE = 'incomplete'
    PLAN_STATUS_CHOICES = [
        (PLAN_STATUS_NONE,       'No subscription'),
        (PLAN_STATUS_ACTIVE,     'Active'),
        (PLAN_STATUS_PAST_DUE,   'Past due'),
        (PLAN_STATUS_CANCELLED,  'Cancelled'),
        (PLAN_STATUS_INCOMPLETE, 'Incomplete (awaiting payment)'),
    ]
    plan_status = models.CharField(max_length=20, choices=PLAN_STATUS_CHOICES,
                                   default=PLAN_STATUS_NONE)

    # ── AI feature flags (per-tenant override of global env defaults) ────────
    # All default ON — existing tenants get full features. Admin can disable
    # any individual feature; disabled features skip processing on new uploads
    # but leave existing data intact (can be re-enabled later to resume).
    feature_face_recognition  = models.BooleanField(default=True,
        help_text="Run InsightFace on uploaded media. Biometric — GDPR-sensitive.")
    feature_speech_to_text    = models.BooleanField(default=True,
        help_text="Whisper transcription for video audio tracks.")
    feature_translation       = models.BooleanField(default=True,
        help_text="NLLB-200 translation of subtitles into other languages.")
    feature_diarization       = models.BooleanField(default=True,
        help_text="pyannote speaker diarization. Biometric — GDPR-sensitive.")
    feature_audio_events      = models.BooleanField(default=True,
        help_text="PANNs audio event detection (applause, music, silence).")
    feature_scene_description = models.BooleanField(default=True,
        help_text="BLIP/Florence-2 natural language frame descriptions.")
    feature_object_detection  = models.BooleanField(default=True,
        help_text="YOLO object labels on frames.")
    feature_clip_embeddings   = models.BooleanField(default=True,
        help_text="CLIP semantic search embeddings on frames + photos.")
    feature_video_summary     = models.BooleanField(default=True,
        help_text="Ollama-based AI video summary (no-op if USE_OLLAMA=false).")
    feature_auto_captions     = models.BooleanField(default=True,
        help_text="Auto-trigger caption generation after upload.")

    # ── HLS encoding policy (per-tenant override of global HLS_QUALITIES) ────
    # Stored as comma-separated heights ("1080,720,480"). Empty = inherit global.
    # Renditions ABOVE the source video height are always skipped at encode time
    # — only resolutions <= source actually get produced. Admin can later
    # manually upscale to a larger resolution via the upscale workflow.
    hls_enabled_qualities = models.CharField(
        max_length=120, blank=True, default='',
        help_text="Comma-separated list of HLS heights to encode for new uploads, "
                  "e.g. '1080,720,480'. Empty = use global HLS_QUALITIES setting.")
    hls_multi_quality = models.BooleanField(
        default=True,
        help_text="If False, encode only a single rendition (no adaptive streaming). "
                  "Saves AI minutes and disk space.")

    class Meta:
        app_label = 'tenants'

    def get_enabled_hls_heights(self) -> set:
        """Parse hls_enabled_qualities → set of int heights. Empty → None (inherit)."""
        s = (self.hls_enabled_qualities or '').strip()
        if not s:
            return set()
        out = set()
        for part in s.split(','):
            part = part.strip()
            if part.isdigit():
                out.add(int(part))
        return out

    def __str__(self):
        return f"{self.name} ({self.slug})"


class OnboardingInvite(models.Model):
    """
    One-time invite link sent to an org admin after provisioning.
    Token is consumed when the admin sets their password + picks a plan.
    """
    tenant         = models.OneToOneField(Tenant, on_delete=models.CASCADE,
                                          related_name='invite')
    token          = models.CharField(max_length=64, unique=True, db_index=True)
    admin_email    = models.EmailField()
    admin_username = models.CharField(max_length=150)
    created_at     = models.DateTimeField(auto_now_add=True)
    expires_at     = models.DateTimeField()
    consumed_at    = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = 'tenants'

    def is_valid(self) -> bool:
        from django.utils import timezone
        return self.consumed_at is None and self.expires_at > timezone.now()

    def __str__(self):
        return f"Invite for {self.tenant.slug} ({'consumed' if self.consumed_at else 'pending'})"


class TopUpProduct(models.Model):
    """
    Sellable top-up SKU managed by the platform owner.
    Two kinds:
      - storage  → recurring monthly subscription, adds GB to plan
      - credits  → one-time purchase, AI minutes that expire in 12 months
    """
    KIND_STORAGE = 'storage'
    KIND_CREDITS = 'credits'
    KIND_CHOICES = [
        (KIND_STORAGE, 'Storage Addon (monthly)'),
        (KIND_CREDITS, 'AI Credit Pack (one-time)'),
    ]

    kind        = models.CharField(max_length=20, choices=KIND_CHOICES)
    name        = models.CharField(max_length=100,
                                   help_text="Public display name, e.g. '500 GB Pack'")
    amount      = models.PositiveIntegerField(
                      help_text="Number of GB (for storage) or minutes (for credits)")
    price_usd   = models.DecimalField(max_digits=8, decimal_places=2,
                                      help_text="Price in USD")
    is_active   = models.BooleanField(default=True,
                                      help_text="Hide from the org top-up page when unchecked")
    sort_order  = models.PositiveIntegerField(default=0,
                                              help_text="Lower numbers appear first")
    # Stripe wiring (filled in later)
    stripe_price_id   = models.CharField(max_length=120, blank=True, default='')
    stripe_product_id = models.CharField(max_length=120, blank=True, default='')

    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenants'
        ordering  = ['kind', 'sort_order', 'amount']

    @property
    def unit_label(self) -> str:
        return 'GB' if self.kind == self.KIND_STORAGE else 'min'

    @property
    def billing_suffix(self) -> str:
        return '/mo' if self.kind == self.KIND_STORAGE else ''

    def __str__(self):
        return f"{self.get_kind_display()} — {self.name} (${self.price_usd}{self.billing_suffix})"


class StorageAddon(models.Model):
    """
    Monthly subscription addon that adds extra storage GB to a tenant's
    plan limit. Stays active until cancelled.
    """
    tenant       = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='storage_addons')
    product      = models.ForeignKey('TopUpProduct', on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name='storage_addons')
    gb_amount    = models.PositiveIntegerField(help_text="Extra GB added per month")
    price_usd    = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True,
                                       help_text="Price snapshot at subscription time")
    started_at   = models.DateTimeField(auto_now_add=True)
    cancelled_at = models.DateTimeField(null=True, blank=True,
                                        help_text="When the user requested cancellation. "
                                                  "Storage is still granted until expires_at.")
    expires_at   = models.DateTimeField(null=True, blank=True,
                                        help_text="When the subscription actually terminates "
                                                  "(end of paid period). Null = renews indefinitely.")
    # Stripe wiring (filled in later)
    stripe_subscription_id = models.CharField(max_length=120, blank=True, default='')

    class Meta:
        app_label = 'tenants'

    @property
    def is_active(self) -> bool:
        """Active = currently granting GB. True until the paid period has fully ended."""
        from django.utils import timezone
        if self.expires_at and self.expires_at <= timezone.now():
            return False
        # Legacy rows from before expires_at existed → fall back to cancelled_at
        if self.cancelled_at and not self.expires_at:
            return False
        return True

    @property
    def is_pending_cancel(self) -> bool:
        """User clicked cancel but the paid period hasn't ended yet."""
        from django.utils import timezone
        return bool(self.cancelled_at) and self.expires_at and self.expires_at > timezone.now()

    def __str__(self):
        return f"{self.tenant.slug} | +{self.gb_amount} GB" + (" (cancelled)" if self.cancelled_at else "")


class AICreditPack(models.Model):
    """
    One-time AI minutes credit purchase. Expires 12 months after purchase.
    Consumed FIFO when monthly usage exceeds plan limit.
    """
    tenant            = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='credit_packs')
    product           = models.ForeignKey('TopUpProduct', on_delete=models.SET_NULL,
                                          null=True, blank=True, related_name='credit_packs')
    minutes_purchased = models.PositiveIntegerField()
    minutes_consumed  = models.FloatField(default=0,
                                          help_text="Drained as overage accrues; updated lazily by metering")
    price_usd         = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True,
                                            help_text="Price snapshot at purchase time")
    purchased_at      = models.DateTimeField(auto_now_add=True)
    expires_at        = models.DateTimeField()
    # Stripe wiring
    stripe_payment_intent_id = models.CharField(max_length=120, blank=True, default='')

    class Meta:
        app_label = 'tenants'
        ordering = ['purchased_at']   # FIFO consumption

    @property
    def minutes_remaining(self) -> float:
        return max(0.0, self.minutes_purchased - self.minutes_consumed)

    @property
    def is_expired(self) -> bool:
        from django.utils import timezone
        return self.expires_at <= timezone.now()

    @property
    def is_active(self) -> bool:
        return not self.is_expired and self.minutes_remaining > 0

    def __str__(self):
        return f"{self.tenant.slug} | {self.minutes_remaining:.1f}/{self.minutes_purchased} min (exp {self.expires_at:%Y-%m-%d})"


class LeadRequest(models.Model):
    """
    Inbound contact-form submission from the public landing page.
    Reviewed by the platform owner in the control plane.
    """
    STATUS_NEW       = 'new'
    STATUS_CONTACTED = 'contacted'
    STATUS_QUALIFIED = 'qualified'
    STATUS_CLOSED    = 'closed'
    STATUS_SPAM      = 'spam'
    STATUS_CHOICES = [
        (STATUS_NEW,       'New'),
        (STATUS_CONTACTED, 'Contacted'),
        (STATUS_QUALIFIED, 'Qualified'),
        (STATUS_CLOSED,    'Closed'),
        (STATUS_SPAM,      'Spam'),
    ]

    name         = models.CharField(max_length=120)
    email        = models.EmailField()
    company      = models.CharField(max_length=160, blank=True)
    phone        = models.CharField(max_length=50,  blank=True)
    interest     = models.CharField(max_length=80,  blank=True,
                                    help_text="e.g. 'Pro plan', 'Enterprise', 'Demo'")
    message      = models.TextField(blank=True)
    referrer     = models.CharField(max_length=300, blank=True, default='')
    user_agent   = models.CharField(max_length=300, blank=True, default='')
    ip_address   = models.GenericIPAddressField(null=True, blank=True)
    status       = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_NEW)
    notes        = models.TextField(blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = 'tenants'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} <{self.email}> — {self.get_status_display()}"


class UsageEvent(models.Model):
    """
    Append-only ledger of AI/storage usage per tenant.
    Aggregated to enforce plan limits.
    """
    TYPE_VIDEO_PROCESSING = 'video_processing'
    TYPE_PHOTO_PROCESSING = 'photo_processing'
    TYPE_TRANSLATION      = 'translation'
    TYPE_STORAGE_DELTA    = 'storage_delta'

    EVENT_TYPES = [
        (TYPE_VIDEO_PROCESSING, 'Video Processing'),
        (TYPE_PHOTO_PROCESSING, 'Photo Processing'),
        (TYPE_TRANSLATION,      'Translation'),
        (TYPE_STORAGE_DELTA,    'Storage Delta'),
    ]

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='usage_events')
    event_type = models.CharField(max_length=30, choices=EVENT_TYPES)
    # minutes for AI tasks, bytes for storage_delta
    value = models.FloatField(default=0)
    timestamp = models.DateTimeField(auto_now_add=True)
    # Optional reference to task that generated this event
    task_id = models.CharField(max_length=100, blank=True)

    class Meta:
        app_label = 'tenants'
        indexes = [
            models.Index(fields=['tenant', 'event_type', 'timestamp']),
        ]

    def __str__(self):
        return f"{self.tenant.slug} | {self.event_type} | {self.value}"
