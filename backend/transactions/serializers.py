from rest_framework import serializers
from .models import Transaction, BankAccount, Category

class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = '__all__'

class BankAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = BankAccount
        fields = '__all__'
        read_only_fields = ['user']

class TransactionSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)
    
    class Meta:
        model = Transaction
        fields = [
            'id', 'user', 'description', 'amount', 'transaction_type',
            'category', 'category_name', 'transaction_date', 'merchant',
            'notes', 'bank_account', 'predicted_category', 
            'prediction_confidence', 'created_at', 'updated_at'
        ]
        read_only_fields = ['user', 'predicted_category', 'prediction_confidence', 'created_at', 'updated_at']
    
    def create(self, validated_data):
        transaction = Transaction.objects.create(**validated_data)
        return transaction