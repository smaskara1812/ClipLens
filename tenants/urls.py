from django.urls import path
from . import views

app_name = 'tenants'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('tenants/new/', views.create_tenant, name='create_tenant'),
    path('tenants/<int:tenant_id>/', views.tenant_detail, name='tenant_detail'),
    path('tenants/<int:tenant_id>/change-plan/', views.change_plan, name='change_plan'),
    path('tenants/<int:tenant_id>/toggle/', views.toggle_tenant, name='toggle_tenant'),
    # ── Per-tenant media relocation (Phase 7) ─────────────────────────────────
    path('tenants/<int:tenant_id>/media/relocate/queue/',
         views.tenant_media_relocate_queue, name='tenant_media_relocate_queue'),
    path('tenants/<int:tenant_id>/media/relocate/<int:relocation_id>/status/',
         views.tenant_media_relocate_status, name='tenant_media_relocate_status'),
    path('tenants/<int:tenant_id>/media/relocate/<int:relocation_id>/cancel/',
         views.tenant_media_relocate_cancel, name='tenant_media_relocate_cancel'),
    path('tenants/<int:tenant_id>/media/relocate/<int:relocation_id>/force-cancel/',
         views.tenant_media_relocate_force_cancel, name='tenant_media_relocate_force_cancel'),
    path('tenants/<int:tenant_id>/media/relocate/<int:relocation_id>/purge/',
         views.tenant_media_relocate_purge, name='tenant_media_relocate_purge'),
    path('plans/', views.manage_plans, name='manage_plans'),
    path('topups/', views.manage_topups, name='manage_topups'),
    path('leads/',  views.manage_leads,  name='manage_leads'),
    path('email/',     views.manage_platform_email, name='platform_email'),
    path('email/log/', views.platform_email_log,    name='platform_email_log'),
    path('support/',                       views.platform_support_list,   name='platform_support_list'),
    path('support/<int:ticket_id>/',       views.platform_support_detail, name='platform_support_detail'),
    path('system/health/', views.system_health, name='system_health'),
    path('tasks/failed/',                    views.failed_tasks_page,        name='failed_tasks'),
    path('tasks/failed/<int:task_pk>/resolve/', views.failed_task_resolve,   name='failed_task_resolve'),
    path('tasks/failed/resolve-all/',        views.failed_tasks_resolve_all, name='failed_tasks_resolve_all'),
    path('system/health/check/<str:check_id>/', views.system_health_check, name='system_health_check'),
    path('system/databases/', views.system_databases, name='system_databases'),
    path('system/databases/<str:alias>/reveal/', views.system_database_reveal, name='system_database_reveal'),
    path('api/usage/<int:tenant_id>/', views.api_usage, name='api_usage'),
]
