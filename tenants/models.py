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
    """
    name = models.CharField(max_length=80, unique=True)
    storage_limit_gb = models.PositiveIntegerField(default=100)
    ai_minutes_limit = models.PositiveIntegerField(default=300)
    max_users = models.PositiveIntegerField(default=3)
    max_videos = models.PositiveIntegerField(default=0, help_text="0 = unlimited")

    class Meta:
        app_label = 'tenants'

    def __str__(self):
        return self.name


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
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name='tenants')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Contact info (optional)
    admin_email = models.EmailField(blank=True)

    class Meta:
        app_label = 'tenants'

    def __str__(self):
        return f"{self.name} ({self.slug})"


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
