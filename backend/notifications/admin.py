from django.contrib import admin

try:
    from .models import Notification

    @admin.register(Notification)
    class NotificationAdmin(admin.ModelAdmin):
        list_display = ['user', 'title', 'notification_type', 'is_read', 'created_at']
        list_filter = ['notification_type', 'is_read', 'created_at']
        search_fields = ['user__email', 'title', 'message']
        ordering = ['-created_at']
        readonly_fields = ['created_at']
        
        actions = ['mark_as_read', 'mark_as_unread']
        
        def mark_as_read(self, request, queryset):
            queryset.update(is_read=True)
            self.message_user(request, f"{queryset.count()} notifications marked as read")
        mark_as_read.short_description = "Mark selected as read"
        
        def mark_as_unread(self, request, queryset):
            queryset.update(is_read=False)
            self.message_user(request, f"{queryset.count()} notifications marked as unread")
        mark_as_unread.short_description = "Mark selected as unread"

except ImportError as e:
    print(f"⚠️ Notification model not found: {e}")

try:
    from .models import NotificationPreference

    @admin.register(NotificationPreference)
    class NotificationPreferenceAdmin(admin.ModelAdmin):
        list_display = ['user', 'email_notifications', 'push_notifications', 'spending_alerts']
        search_fields = ['user__email']

except ImportError:
    pass