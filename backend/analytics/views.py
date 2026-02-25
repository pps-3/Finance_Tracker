from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.db.models import Sum, Count, Avg, Q
from django.db.models.functions import TruncMonth
from datetime import datetime, timedelta
from decimal import Decimal

from transactions.models import Transaction

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def spending_by_category(request):
    """Get spending grouped by category"""
    days = int(request.GET.get('days', 30))
    start_date = datetime.now().date() - timedelta(days=days)
    
    data = Transaction.objects.filter(
        user=request.user,
        transaction_type='expense',
        transaction_date__gte=start_date
    ).values('category__name').annotate(
        total=Sum('amount'),
        count=Count('id')
    ).order_by('-total')
    
    return Response(list(data))

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def monthly_trends(request):
    """Get monthly income and expense trends"""
    months = int(request.GET.get('months', 6))
    start_date = datetime.now().date() - timedelta(days=months*30)
    
    # Income by month
    income = Transaction.objects.filter(
        user=request.user,
        transaction_type='income',
        transaction_date__gte=start_date
    ).annotate(
        month=TruncMonth('transaction_date')
    ).values('month').annotate(
        total=Sum('amount')
    ).order_by('month')
    
    # Expenses by month
    expenses = Transaction.objects.filter(
        user=request.user,
        transaction_type='expense',
        transaction_date__gte=start_date
    ).annotate(
        month=TruncMonth('transaction_date')
    ).values('month').annotate(
        total=Sum('amount')
    ).order_by('month')
    
    return Response({
        'income': list(income),
        'expenses': list(expenses)
    })

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def financial_health(request):
    """Calculate financial health score"""
    # Last 30 days
    start_date = datetime.now().date() - timedelta(days=30)
    
    income = Transaction.objects.filter(
        user=request.user,
        transaction_type='income',
        transaction_date__gte=start_date
    ).aggregate(total=Sum('amount'))['total'] or Decimal(0)
    
    expenses = Transaction.objects.filter(
        user=request.user,
        transaction_type='expense',
        transaction_date__gte=start_date
    ).aggregate(total=Sum('amount'))['total'] or Decimal(0)
    
    savings = income - expenses
    savings_rate = (savings / income * 100) if income > 0 else 0
    
    # Calculate health score (0-100)
    score = 0
    if savings_rate >= 20:
        score += 40
    elif savings_rate >= 10:
        score += 20
    elif savings_rate >= 0:
        score += 10
    
    if income > expenses:
        score += 30
    
    if expenses < income * Decimal(0.8):
        score += 30
    
    return Response({
        'score': min(100, max(0, score)),
        'income': float(income),
        'expenses': float(expenses),
        'savings': float(savings),
        'savings_rate': float(savings_rate)
    })

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def anomalies(request):
    """Detect unusual spending patterns"""
    anomalies_list = []
    
    # Get average spending by category
    categories = Transaction.objects.filter(
        user=request.user,
        transaction_type='expense'
    ).values('category__name').annotate(
        avg_amount=Avg('amount'),
        max_amount=Sum('amount')
    )
    
    # Find transactions that are significantly higher than average
    for cat in categories:
        if cat['category__name']:
            recent = Transaction.objects.filter(
                user=request.user,
                category__name=cat['category__name'],
                transaction_type='expense'
            ).order_by('-transaction_date')[:5]
            
            for trans in recent:
                if trans.amount > cat['avg_amount'] * 2:
                    anomalies_list.append({
                        'amount': float(trans.amount),
                        'category': cat['category__name'],
                        'threshold': float(cat['avg_amount']),
                        'deviation': float(trans.amount / cat['avg_amount'])
                    })
    
    return Response(anomalies_list[:5])
