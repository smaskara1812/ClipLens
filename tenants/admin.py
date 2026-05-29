from django.contrib import admin
from .models import (
    Plan, Tenant, UsageEvent, OnboardingInvite,
    StorageAddon, AICreditPack, TopUpProduct, LeadRequest,
)


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
