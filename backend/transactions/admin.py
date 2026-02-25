from django.contrib import admin
from .models import Transaction, BankAccount, Category

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'icon', 'color']
    search_fields = ['name']

@admin.register(BankAccount)
class BankAccountAdmin(admin.ModelAdmin):
    list_display = ['user', 'account_name', 'account_type', 'balance', 'is_active']
    list_filter = ['account_type', 'is_active']
    search_fields = ['user__email', 'account_name']

@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ['user', 'description', 'amount', 'transaction_type', 'category', 'transaction_date']
    list_filter = ['transaction_type', 'category', 'transaction_date']
    search_fields = ['description', 'merchant', 'user__email']
    ordering = ['-transaction_date']
    readonly_fields = ['created_at', 'updated_at']