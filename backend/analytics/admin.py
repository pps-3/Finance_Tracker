from django.contrib import admin

# Analytics usually doesn't have its own models
# It uses Transaction models for calculations
# But if there are any cached analytics models, register them here

try:
    from .models import AnalyticsCache

    @admin.register(AnalyticsCache)
    class AnalyticsCacheAdmin(admin.ModelAdmin):
        list_display = ['user', 'cache_type', 'created_at', 'expires_at']
        list_filter = ['cache_type', 'created_at']
        search_fields = ['user__email']
        ordering = ['-created_at']
        readonly_fields = ['created_at']

except ImportError:
    pass  # Analytics module has no models to register - that's normal!

try:
    from .models import SpendingReport

    @admin.register(SpendingReport)
    class SpendingReportAdmin(admin.ModelAdmin):
        list_display = ['user', 'report_type', 'start_date', 'end_date', 'total_spending', 'created_at']
        list_filter = ['report_type', 'created_at']
        search_fields = ['user__email']
        ordering = ['-created_at']
        readonly_fields = ['created_at']

except ImportError:
    pass