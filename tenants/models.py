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

    # Controls whether the plan appears on public-facing surfaces
    # (onboarding plan picker, org upgrade page, landing page).
    # Existing tenants on a disabled plan keep working — disabling only stops
    # new signups from selecting it.
    is_active = models.BooleanField(default=True, db_index=True)

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

    # ── Custom media storage (optional) ──────────────────────────────────
    # When empty: this tenant's media lives at MEDIA_ROOT/<media_folder>/.
    # When set: an absolute filesystem path on the host (must be mounted
    # and writable by the running user). Allows per-tenant data placement
    # on different volumes / NFS / S3-fuse / etc.
    media_root_absolute = models.CharField(
        max_length=500, blank=True, default='',
        help_text='Optional absolute path for this tenant\'s media. '
                  'Must be already mounted and writable. Leave blank for default.',
    )

    # Lifecycle flags for media relocation
    media_relocating = models.BooleanField(
        default=False,
        help_text='True while a media-relocation Celery task is running. '
                  'Blocks tenant access via maintenance page.',
    )
    media_relocation_started_at = models.DateTimeField(null=True, blank=True)
    media_relocation_cancel_requested = models.BooleanField(default=False)
    media_relocation_task_id = models.CharField(max_length=64, blank=True, default='')

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


class EmailConnection(models.Model):
    """
    A saved SMTP configuration used to send outbound email.

    Scope:
      • scope='platform' → for platform-wide emails (onboarding invites, lead
        notifications, payment receipts). Managed by the platform owner only.
        `tenant` is NULL for these rows.
      • scope='tenant'   → for org-internal emails (quota warnings, internal
        notifications). Managed by the org's superadmin. `tenant` FK is set.

    Multiple connections can be saved per scope, but only ONE can be active
    at a time. The active connection is what the email system picks up.
    """
    SCOPE_PLATFORM = 'platform'
    SCOPE_TENANT   = 'tenant'
    SCOPE_CHOICES = [
        (SCOPE_PLATFORM, 'Platform'),
        (SCOPE_TENANT,   'Tenant'),
    ]

    scope         = models.CharField(max_length=10, choices=SCOPE_CHOICES, db_index=True)
    tenant        = models.ForeignKey(Tenant, on_delete=models.CASCADE,
                                      null=True, blank=True,
                                      related_name='email_connections',
                                      help_text="Required when scope='tenant'.")
    name          = models.CharField(max_length=80,
                                     help_text="Friendly label, e.g. 'SendGrid prod'.")

    host          = models.CharField(max_length=200)
    port          = models.PositiveIntegerField(default=587)
    use_tls       = models.BooleanField(default=True)
    use_ssl       = models.BooleanField(default=False,
                                        help_text="Mutually exclusive with TLS. SMTPS on port 465.")
    username      = models.CharField(max_length=200, blank=True, default='')
    password_enc  = models.BinaryField(blank=True, default=b'',
                                       help_text="Fernet-encrypted SMTP password.")

    from_email    = models.EmailField(help_text="The 'From:' address used for sent emails.")
    from_name     = models.CharField(max_length=120, blank=True, default='',
                                     help_text="Optional display name, e.g. 'ClipLens'.")

    is_active     = models.BooleanField(default=False, db_index=True,
                                        help_text="Only one connection per scope is active at a time.")

    last_tested_at    = models.DateTimeField(null=True, blank=True)
    last_test_result  = models.CharField(max_length=400, blank=True, default='',
                                         help_text="'OK' on success, otherwise the error message.")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by_username = models.CharField(max_length=150, blank=True, default='')

    class Meta:
        app_label = 'tenants'
        ordering  = ['scope', '-is_active', 'name']
        indexes   = [
            models.Index(fields=['scope', 'is_active']),
            models.Index(fields=['tenant', 'is_active']),
        ]

    def __str__(self):
        target = self.tenant.slug if self.tenant else 'platform'
        return f"{target} · {self.name} ({self.host}:{self.port})"

    # ── Password encryption ────────────────────────────────────────────────
    @staticmethod
    def _fernet():
        """Return a Fernet instance using EMAIL_SECRET_KEY or a SECRET_KEY-derived fallback."""
        from cryptography.fernet import Fernet
        from django.conf import settings as _s
        key = (getattr(_s, 'EMAIL_SECRET_KEY', '') or '').encode() if getattr(_s, 'EMAIL_SECRET_KEY', '') else None
        if not key:
            # Dev fallback — deterministic from SECRET_KEY so saved data still decrypts on restart
            import hashlib, base64
            digest = hashlib.sha256(_s.SECRET_KEY.encode()).digest()
            key = base64.urlsafe_b64encode(digest)
        return Fernet(key)

    def set_password(self, raw_password: str) -> None:
        """Encrypt and store a plain-text SMTP password."""
        if not raw_password:
            self.password_enc = b''
            return
        self.password_enc = self._fernet().encrypt(raw_password.encode('utf-8'))

    def get_password(self) -> str:
        """Decrypt and return the SMTP password, or '' if none stored."""
        if not self.password_enc:
            return ''
        try:
            return self._fernet().decrypt(bytes(self.password_enc)).decode('utf-8')
        except Exception:
            return ''


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

class EmailLog(models.Model):
    """
    Detailed log of every email send attempt — successes, failures, and drops.
    Lives in the control DB so platform owners can see all activity across
    tenants. Tenant admins see only their own tenant's rows via a filtered view.
    """
    STATUS_QUEUED  = 'queued'
    STATUS_SENT    = 'sent'
    STATUS_FAILED  = 'failed'
    STATUS_DROPPED = 'dropped'       # EMAIL_ENABLED=false or no valid recipients
    STATUS_CHOICES = [
        (STATUS_QUEUED,  'Queued'),
        (STATUS_SENT,    'Sent'),
        (STATUS_FAILED,  'Failed'),
        (STATUS_DROPPED, 'Dropped'),
    ]

    SCOPE_PLATFORM = 'platform'
    SCOPE_TENANT   = 'tenant'
    SCOPE_CHOICES  = [
        (SCOPE_PLATFORM, 'Platform'),
        (SCOPE_TENANT,   'Tenant'),
    ]

    BACKEND_SMTP     = 'smtp'
    BACKEND_CONSOLE  = 'console'
    BACKEND_DEFAULT  = 'default'      # Django settings fallback (no managed conn)
    BACKEND_DISABLED = 'disabled'     # EMAIL_ENABLED=false
    BACKEND_CHOICES  = [
        (BACKEND_SMTP,     'SMTP (managed)'),
        (BACKEND_CONSOLE,  'Console (dev mode)'),
        (BACKEND_DEFAULT,  'Django default'),
        (BACKEND_DISABLED, 'Disabled (dropped)'),
    ]

    # Scope + tenancy
    scope          = models.CharField(max_length=10, choices=SCOPE_CHOICES)
    tenant         = models.ForeignKey(
        Tenant, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='email_logs',
    )

    # Connection used (nullable so deleting a connection doesn't lose history)
    connection     = models.ForeignKey(
        EmailConnection, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='logs',
    )
    connection_name_snapshot = models.CharField(
        max_length=120, blank=True,
        help_text="Name of the connection at send time (preserved if connection deleted)",
    )
    backend_type   = models.CharField(max_length=12, choices=BACKEND_CHOICES, default=BACKEND_SMTP)

    # Message
    subject        = models.CharField(max_length=300)
    from_email     = models.CharField(max_length=320, blank=True)
    to_emails      = models.JSONField(default=list, help_text='List of recipient addresses')
    cc_emails      = models.JSONField(default=list, blank=True)
    bcc_emails     = models.JSONField(default=list, blank=True)
    reply_to       = models.CharField(max_length=320, blank=True)
    body_preview   = models.CharField(max_length=500, blank=True, help_text='First 500 chars of plain body')
    has_html       = models.BooleanField(default=False)
    template_key   = models.CharField(max_length=64, blank=True, help_text='Future: template system key')

    # Trigger / actor
    trigger_source = models.CharField(
        max_length=64, blank=True,
        help_text='What code path queued this — e.g. submit_lead, create_tenant, quota_warning, manual_test',
    )
    triggered_by_username = models.CharField(
        max_length=150, blank=True,
        help_text='Username of the human who initiated (blank = system / webhook)',
    )

    # Status + lifecycle
    status         = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_QUEUED, db_index=True)
    error_class    = models.CharField(max_length=120, blank=True)
    error_message  = models.TextField(blank=True)
    retry_count    = models.PositiveSmallIntegerField(default=0)
    duration_ms    = models.PositiveIntegerField(null=True, blank=True,
                       help_text='Time spent in the send call (ms)')
    celery_task_id = models.CharField(max_length=64, blank=True)

    # Timestamps
    queued_at      = models.DateTimeField(auto_now_add=True, db_index=True)
    sent_at        = models.DateTimeField(null=True, blank=True)
    failed_at      = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = 'tenants'
        ordering = ['-queued_at']
        indexes = [
            models.Index(fields=['scope', 'queued_at']),
            models.Index(fields=['tenant', 'queued_at']),
            models.Index(fields=['status', 'queued_at']),
            models.Index(fields=['trigger_source']),
        ]

    def __str__(self):
        return f'[{self.status}] {self.scope}: {self.subject[:60]}'

    @property
    def recipients_display(self):
        return ', '.join(self.to_emails or [])



class SupportTicket(models.Model):
    """
    Support request raised by a tenant org admin. Visible to the platform owner
    who can reply, change status/priority, and resolve.
    """
    CATEGORY_BUG     = 'bug'
    CATEGORY_BILLING = 'billing'
    CATEGORY_FEATURE = 'feature_request'
    CATEGORY_QUESTION = 'question'
    CATEGORY_OTHER   = 'other'
    CATEGORY_CHOICES = [
        (CATEGORY_BUG,      'Bug / something broken'),
        (CATEGORY_BILLING,  'Billing / subscription'),
        (CATEGORY_FEATURE,  'Feature request'),
        (CATEGORY_QUESTION, 'How-to / question'),
        (CATEGORY_OTHER,    'Other'),
    ]

    PRIORITY_LOW    = 'low'
    PRIORITY_NORMAL = 'normal'
    PRIORITY_HIGH   = 'high'
    PRIORITY_URGENT = 'urgent'
    PRIORITY_CHOICES = [
        (PRIORITY_LOW,    'Low'),
        (PRIORITY_NORMAL, 'Normal'),
        (PRIORITY_HIGH,   'High'),
        (PRIORITY_URGENT, 'Urgent'),
    ]

    STATUS_OPEN          = 'open'
    STATUS_IN_PROGRESS   = 'in_progress'
    STATUS_WAITING_USER  = 'waiting_on_user'
    STATUS_RESOLVED      = 'resolved'
    STATUS_CLOSED        = 'closed'
    STATUS_CHOICES = [
        (STATUS_OPEN,         'Open'),
        (STATUS_IN_PROGRESS,  'In Progress'),
        (STATUS_WAITING_USER, 'Waiting on User'),
        (STATUS_RESOLVED,     'Resolved'),
        (STATUS_CLOSED,       'Closed'),
    ]

    tenant       = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='support_tickets')
    subject      = models.CharField(max_length=200)
    body         = models.TextField(help_text='Initial description of the issue')
    category     = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default=CATEGORY_QUESTION)
    priority     = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default=PRIORITY_NORMAL)
    status       = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN, db_index=True)

    created_by_username = models.CharField(max_length=150, blank=True)
    created_by_email    = models.CharField(max_length=320, blank=True)

    created_at  = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at  = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = 'tenants'
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['tenant', 'status', '-updated_at']),
            models.Index(fields=['status', 'priority', '-updated_at']),
        ]

    def __str__(self):
        return f'#{self.pk} [{self.status}] {self.subject[:50]}'

    @property
    def is_open(self):
        return self.status not in (self.STATUS_RESOLVED, self.STATUS_CLOSED)


class SupportMessage(models.Model):
    """One reply on a SupportTicket thread (from tenant or platform)."""
    AUTHOR_TENANT   = 'tenant'
    AUTHOR_PLATFORM = 'platform'
    AUTHOR_CHOICES  = [
        (AUTHOR_TENANT,   'Tenant'),
        (AUTHOR_PLATFORM, 'Platform'),
    ]

    ticket          = models.ForeignKey(SupportTicket, on_delete=models.CASCADE, related_name='messages')
    body            = models.TextField()
    author_username = models.CharField(max_length=150, blank=True)
    author_role     = models.CharField(max_length=10, choices=AUTHOR_CHOICES)
    is_internal     = models.BooleanField(default=False,
                       help_text='Internal note — only visible to platform staff')
    created_at      = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = 'tenants'
        ordering = ['created_at']

    def __str__(self):
        return f'msg {self.pk} on ticket {self.ticket_id} by {self.author_role}'


class TeamMemberInvite(models.Model):
    """
    Invite for a non-admin team member (editor / viewer) to join an existing
    tenant. Created during onboarding ("Invite your team" step) or later from
    the user management page. Consumed when the invitee clicks the link and
    sets their password — at that point the User + UserProfile rows are
    created inside the tenant DB.
    """
    ROLE_SUPERADMIN = 'superadmin'
    ROLE_EDITOR     = 'editor'
    ROLE_VIEWER     = 'viewer'
    ROLE_CHOICES = [
        (ROLE_SUPERADMIN, 'Superadmin'),
        (ROLE_EDITOR,     'Editor'),
        (ROLE_VIEWER,     'Viewer'),
    ]

    tenant     = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='team_invites')
    token      = models.CharField(max_length=64, unique=True)
    username   = models.CharField(max_length=150)
    email      = models.CharField(max_length=320, blank=True)
    role       = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_EDITOR)
    invited_by_username = models.CharField(max_length=150, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)
    expires_at  = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = 'tenants'
        indexes = [
            models.Index(fields=['tenant', 'consumed_at']),
            models.Index(fields=['token']),
        ]
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'username'],
                                    condition=models.Q(consumed_at__isnull=True),
                                    name='unique_pending_invite_per_tenant_username'),
        ]

    def __str__(self):
        return f'invite {self.username}@{self.tenant.slug} [{self.role}]'

    def is_valid(self) -> bool:
        from django.utils import timezone
        return (self.consumed_at is None) and (self.expires_at > timezone.now())


class MediaRelocation(models.Model):
    """
    Audit log of every per-tenant media relocation attempt.
    One row per attempted move, tracking start/end timestamps + outcome.
    """
    STATUS_QUEUED      = 'queued'
    STATUS_RUNNING     = 'running'
    STATUS_VERIFYING   = 'verifying'
    STATUS_SUCCEEDED   = 'succeeded'
    STATUS_FAILED      = 'failed'
    STATUS_CANCELLED   = 'cancelled'
    STATUS_CHOICES = [
        (STATUS_QUEUED,    'Queued'),
        (STATUS_RUNNING,   'Running'),
        (STATUS_VERIFYING, 'Verifying'),
        (STATUS_SUCCEEDED, 'Succeeded'),
        (STATUS_FAILED,    'Failed'),
        (STATUS_CANCELLED, 'Cancelled'),
    ]

    tenant         = models.ForeignKey(Tenant, on_delete=models.CASCADE,
                                       related_name='media_relocations')
    source_path    = models.CharField(max_length=600)
    target_path    = models.CharField(max_length=600)
    status         = models.CharField(max_length=20, choices=STATUS_CHOICES,
                                      default=STATUS_QUEUED, db_index=True)

    # Counters updated as the task progresses (UI polls these)
    total_bytes        = models.BigIntegerField(default=0)
    bytes_copied       = models.BigIntegerField(default=0)
    total_files        = models.PositiveIntegerField(default=0)
    files_copied       = models.PositiveIntegerField(default=0)

    # When the soft-deleted "<old>.delete_after_<ts>" path will be auto-purged
    old_path_soft_deleted = models.CharField(max_length=600, blank=True, default='',
                              help_text='Where the old data is parked until grace period expires')
    grace_period_until    = models.DateTimeField(null=True, blank=True)
    purged_at             = models.DateTimeField(null=True, blank=True)

    initiated_by_username = models.CharField(max_length=150, blank=True)
    celery_task_id        = models.CharField(max_length=64, blank=True, default='')
    error_message         = models.TextField(blank=True)

    queued_at     = models.DateTimeField(auto_now_add=True)
    started_at    = models.DateTimeField(null=True, blank=True)
    finished_at   = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = 'tenants'
        ordering = ['-queued_at']
        indexes = [
            models.Index(fields=['tenant', '-queued_at']),
            models.Index(fields=['status', '-queued_at']),
        ]

    def __str__(self):
        return f'#{self.pk} {self.tenant.slug}: {self.source_path} → {self.target_path} [{self.status}]'

    @property
    def progress_pct(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return min(100.0, round(self.bytes_copied * 100 / self.total_bytes, 1))


class FailedTask(models.Model):
    """
    Platform-wide log of every Celery task failure, across all tenants.
    Written by the task_failure signal in celery_utils.py — visible only
    to the platform owner at /tasks/failed/ on the admin subdomain.
    """
    task_name     = models.CharField(max_length=200, db_index=True)
    task_id       = models.CharField(max_length=64, blank=True, default='')
    tenant_slug   = models.CharField(max_length=100, blank=True, default='', db_index=True,
                                     help_text='Empty for non-tenant (platform) tasks')
    queue         = models.CharField(max_length=50, blank=True, default='')
    args_snippet  = models.TextField(blank=True, default='',
                                     help_text='Truncated repr of task args for debugging')
    error_message = models.TextField(blank=True, default='')
    traceback     = models.TextField(blank=True, default='',
                                     help_text='Truncated to last 8000 chars')
    failed_at     = models.DateTimeField(auto_now_add=True, db_index=True)

    # Platform-owner triage
    resolved              = models.BooleanField(default=False, db_index=True)
    resolved_at           = models.DateTimeField(null=True, blank=True)
    resolved_by_username  = models.CharField(max_length=150, blank=True, default='')

    class Meta:
        app_label = 'tenants'
        ordering = ['-failed_at']
        indexes = [
            models.Index(fields=['resolved', '-failed_at']),
            models.Index(fields=['tenant_slug', '-failed_at']),
        ]

    def __str__(self):
        return f'{self.task_name} [{self.tenant_slug or "platform"}] @ {self.failed_at:%Y-%m-%d %H:%M}'
