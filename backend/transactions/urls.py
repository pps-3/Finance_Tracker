from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    TransactionViewSet, 
    BankAccountViewSet, 
    CategoryViewSet, 
    extract_pdf_transactions
)

router = DefaultRouter()
router.register(r'', TransactionViewSet, basename='transaction')
router.register(r'bank-accounts', BankAccountViewSet, basename='bankaccount')
router.register(r'categories', CategoryViewSet, basename='category')

urlpatterns = [
    path('extract-pdf/', extract_pdf_transactions, name='extract-pdf'),
    path('', include(router.urls)),
]