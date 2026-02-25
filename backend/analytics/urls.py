from django.urls import path
from .views import spending_by_category, monthly_trends, financial_health, anomalies

urlpatterns = [
    path('spending-by-category/', spending_by_category),
    path('monthly-trends/', monthly_trends),
    path('financial-health/', financial_health),
    path('anomalies/', anomalies),
]
