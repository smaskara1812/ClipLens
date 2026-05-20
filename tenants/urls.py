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
    path('api/usage/<int:tenant_id>/', views.api_usage, name='api_usage'),
]
