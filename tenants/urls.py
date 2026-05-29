from django.urls import path
from . import views

app_name = 'tenants'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('tenants/new/', views.create_tenant, name='create_tenant'),
    path('tenants/<int:tenant_id>/', views.tenant_detail, name='tenant_detail'),
    path('tenants/<int:tenant_id>/change-plan/', views.change_plan, name='change_plan'),
    path('tenants/<int:tenant_id>/toggle/', views.toggle_tenant, name='toggle_tenant'),
    path('plans/', views.manage_plans, name='manage_plans'),
    path('topups/', views.manage_topups, name='manage_topups'),
    path('leads/',  views.manage_leads,  name='manage_leads'),
    path('system/health/', views.system_health, name='system_health'),
    path('system/health/check/<str:check_id>/', views.system_health_check, name='system_health_check'),
    path('api/usage/<int:tenant_id>/', views.api_usage, name='api_usage'),
]
