from django.db import models
from django.contrib.auth import get_user_model

User = get_user_model()

class BankAccount(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='bank_accounts')
    account_name = models.CharField(max_length=100)
    account_number = models.CharField(max_length=50)
    bank_name = models.CharField(max_length=100)
    account_type = models.CharField(max_length=20, choices=[
        ('savings', 'Savings'),
        ('checking', 'Checking'),
        ('credit', 'Credit Card')
    ])
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.account_name} - {self.bank_name}"

class Category(models.Model):
    name = models.CharField(max_length=50, unique=True)
    icon = models.CharField(max_length=50, blank=True)
    color = models.CharField(max_length=7, default='#000000')
    description = models.TextField(blank=True)
    
    class Meta:
        verbose_name_plural = 'Categories'
        ordering = ['name']
    
    def __str__(self):
        return self.name

class Transaction(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='transactions')
    bank_account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, null=True, blank=True)
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True)
    
    transaction_type = models.CharField(max_length=10, choices=[
        ('income', 'Income'),
        ('expense', 'Expense')
    ])
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    description = models.TextField()
    merchant = models.CharField(max_length=200, blank=True)
    
    transaction_date = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # AI categorization
    predicted_category = models.ForeignKey(
        Category, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='predicted_transactions'
    )
    prediction_confidence = models.FloatField(null=True, blank=True)
    is_recurring = models.BooleanField(default=False)
    
    # Notes and attachments
    notes = models.TextField(blank=True)
    
    class Meta:
        ordering = ['-transaction_date']
        indexes = [
            models.Index(fields=['-transaction_date']),
            models.Index(fields=['user', '-transaction_date']),
        ]
    
    def __str__(self):
        return f"{self.transaction_type} - ${self.amount} - {self.description[:30]}"
