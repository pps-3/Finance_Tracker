from django.urls import path
from .views import monthly_forecast, recurring_bills

urlpatterns = [
    path('monthly-forecast/', monthly_forecast),
    path('recurring-bills/', recurring_bills),
]
