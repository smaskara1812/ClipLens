from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender='tenants.AICreditPack')
def bust_usage_warning_cache_on_credit_pack(sender, instance, created, **kwargs):
    if not created:
        return
    try:
        from django.core.cache import cache
        slug = instance.tenant.slug if instance.tenant_id else ''
        if slug:
            cache.delete(f'usage_warning_{slug}')
    except Exception:
        pass


@receiver(post_save, sender='tenants.StorageAddon')
def bust_usage_warning_cache_on_storage_addon(sender, instance, created, **kwargs):
    if not created:
        return
    try:
        from django.core.cache import cache
        slug = instance.tenant.slug if instance.tenant_id else ''
        if slug:
            cache.delete(f'usage_warning_{slug}')
    except Exception:
        pass
