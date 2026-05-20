from django.contrib import admin
from .models import Plan, Tenant, UsageEvent


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
