from django.contrib import admin
from .models import (
    Plan, Tenant, UsageEvent, OnboardingInvite,
    StorageAddon, AICreditPack, TopUpProduct, LeadRequest,
    EmailConnection, EmailLog,
    SupportTicket, SupportMessage,
    TeamMemberInvite,
)


@admin.register(TeamMemberInvite)
class TeamMemberInviteAdmin(admin.ModelAdmin):
    list_display  = ['username', 'tenant', 'email', 'role', 'invited_by_username',
                     'created_at', 'expires_at', 'consumed_at']
    list_filter   = ['role', 'consumed_at']
    search_fields = ['username', 'email', 'tenant__slug']
    readonly_fields = ['token', 'created_at']


class SupportMessageInline(admin.TabularInline):
    model = SupportMessage
    extra = 0
    readonly_fields = ['created_at', 'author_role']


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display  = ['id', 'tenant', 'subject', 'status', 'priority', 'category',
                     'created_by_username', 'created_at', 'updated_at']
    list_filter   = ['status', 'priority', 'category']
    search_fields = ['subject', 'body', 'tenant__slug', 'created_by_username', 'created_by_email']
    readonly_fields = ['created_at', 'updated_at']
    inlines = [SupportMessageInline]


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    list_display  = ['queued_at', 'status', 'scope', 'tenant', 'subject',
                     'trigger_source', 'triggered_by_username']
    list_filter   = ['status', 'scope', 'backend_type', 'trigger_source']
    search_fields = ['subject', 'from_email', 'error_message', 'triggered_by_username']
    readonly_fields = [f.name for f in EmailLog._meta.fields]
    date_hierarchy = 'queued_at'


@admin.register(EmailConnection)
class EmailConnectionAdmin(admin.ModelAdmin):
    list_display  = ['name', 'scope', 'tenant', 'host', 'port',
                     'from_email', 'is_active', 'last_tested_at']
    list_filter   = ['scope', 'is_active', 'use_tls']
    search_fields = ['name', 'host', 'from_email', 'tenant__slug']
    readonly_fields = ['password_enc', 'last_tested_at', 'last_test_result',
                       'created_at', 'updated_at']


@admin.register(LeadRequest)
class LeadRequestAdmin(admin.ModelAdmin):
    list_display  = ['name', 'email', 'company', 'interest', 'status', 'created_at']
    list_filter   = ['status', 'created_at']
    list_editable = ['status']
    search_fields = ['name', 'email', 'company', 'message']
    readonly_fields = ['referrer', 'user_agent', 'ip_address', 'created_at', 'updated_at']


@admin.register(TopUpProduct)
class TopUpProductAdmin(admin.ModelAdmin):
    list_display  = ['kind', 'name', 'amount', 'price_usd', 'is_active', 'sort_order']
    list_filter   = ['kind', 'is_active']
    list_editable = ['is_active', 'sort_order']
    search_fields = ['name']


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ['name', 'storage_limit_gb', 'ai_minutes_limit', 'max_users', 'max_videos']


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'db_name', 'plan', 'is_active', 'created_at']
    list_filter = ['plan', 'is_active']
    search_fields = ['name', 'slug']


@admin.register(UsageEvent)
class UsageEventAdmin(admin.ModelAdmin):
    list_display = ['tenant', 'event_type', 'value', 'timestamp']
    list_filter = ['tenant', 'event_type']
    date_hierarchy = 'timestamp'


@admin.register(OnboardingInvite)
class OnboardingInviteAdmin(admin.ModelAdmin):
    list_display  = ['tenant', 'admin_email', 'admin_username', 'created_at', 'expires_at', 'consumed_at']
    list_filter   = ['consumed_at']
    search_fields = ['tenant__slug', 'admin_email']
    readonly_fields = ['token', 'created_at']


@admin.register(StorageAddon)
class StorageAddonAdmin(admin.ModelAdmin):
    list_display  = ['tenant', 'gb_amount', 'started_at', 'cancelled_at', 'stripe_subscription_id']
    list_filter   = ['cancelled_at']
    search_fields = ['tenant__slug']


@admin.register(AICreditPack)
class AICreditPackAdmin(admin.ModelAdmin):
    list_display  = ['tenant', 'minutes_purchased', 'minutes_consumed',
                     'purchased_at', 'expires_at', 'stripe_payment_intent_id']
    list_filter   = ['purchased_at', 'expires_at']
    search_fields = ['tenant__slug']
    readonly_fields = ['purchased_at']
