from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.db.models import Avg, Count
from datetime import datetime, timedelta
from collections import defaultdict

from transactions.models import Transaction

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def monthly_forecast(request):
    """Simple forecast based on historical averages"""
    # Get last 6 months average
    start_date = datetime.now().date() - timedelta(days=180)
    
    avg_expense = Transaction.objects.filter(
        user=request.user,
        transaction_type='expense',
        transaction_date__gte=start_date
    ).aggregate(avg=Avg('amount'))['avg'] or 0
    
    # Generate forecast for next 3 months
    forecast = []
    for i in range(1, 4):
        future_date = datetime.now().date() + timedelta(days=30*i)
        forecast.append({
            'ds': future_date.isoformat(),
            'yhat': float(avg_expense),
            'yhat_lower': float(avg_expense * 0.9),
            'yhat_upper': float(avg_expense * 1.1)
        })
    
    return Response(forecast)

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def recurring_bills(request):
    """Detect recurring bills"""
    # Group by merchant and check frequency
    merchants = Transaction.objects.filter(
        user=request.user,
        transaction_type='expense'
    ).values('merchant').annotate(
        count=Count('id'),
        avg_amount=Avg('amount')
    ).filter(count__gte=2)
    
    recurring = []
    for merchant_data in merchants:
        merchant = merchant_data['merchant']
        if not merchant:
            continue
            
        transactions = Transaction.objects.filter(
            user=request.user,
            merchant=merchant
        ).order_by('-transaction_date')[:3]
        
        if len(transactions) >= 2:
            # Calculate days between transactions
            dates = [t.transaction_date for t in transactions]
            if len(dates) >= 2:
                days_between = (dates[0] - dates[1]).days
                
                frequency = 'Monthly' if 25 <= days_between <= 35 else 'Regular'
                
                next_expected = dates[0] + timedelta(days=days_between)
                
                recurring.append({
                    'id': transactions[0].id,
                    'merchant': merchant,
                    'amount': float(merchant_data['avg_amount']),
                    'frequency': frequency,
                    'next_expected_date': next_expected.isoformat(),
                    'confidence': 0.85 if frequency == 'Monthly' else 0.65
                })
    
    return Response(recurring[:10])
