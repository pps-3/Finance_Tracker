from django.contrib import admin

# Try to import prediction models - adjust based on what models exist
try:
    from .models import MonthlyForecast, RecurringBill, SpendingPrediction

    @admin.register(MonthlyForecast)
    class MonthlyForecastAdmin(admin.ModelAdmin):
        list_display = ['user', 'month', 'predicted_income', 'predicted_expenses', 'predicted_savings', 'created_at']
        list_filter = ['month', 'created_at']
        search_fields = ['user__email']
        ordering = ['-month']
        readonly_fields = ['created_at']

    @admin.register(RecurringBill)
    class RecurringBillAdmin(admin.ModelAdmin):
        list_display = ['user', 'name', 'amount', 'frequency', 'next_due_date', 'is_active']
        list_filter = ['frequency', 'is_active']
        search_fields = ['user__email', 'name']
        ordering = ['next_due_date']

    @admin.register(SpendingPrediction)
    class SpendingPredictionAdmin(admin.ModelAdmin):
        list_display = ['user', 'category', 'predicted_amount', 'actual_amount', 'month', 'accuracy']
        list_filter = ['category', 'month']
        search_fields = ['user__email']
        ordering = ['-month']

except ImportError as e:
    print(f"⚠️ Some prediction models not found: {e}")

# Fallback - try to register whatever models exist
try:
    from .models import MonthlyForecast
    if MonthlyForecast not in admin.site._registry:
        admin.site.register(MonthlyForecast)
except:
    pass

try:
    from .models import RecurringBill
    if RecurringBill not in admin.site._registry:
        admin.site.register(RecurringBill)
except:
    pass