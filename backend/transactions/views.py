from rest_framework import viewsets, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters
import re
from datetime import datetime
import os
import json

from .models import Transaction, BankAccount, Category
from .serializers import TransactionSerializer, BankAccountSerializer, CategorySerializer

# Gemini AI Integration
try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:             
    GEMINI_AVAILABLE = False
    print("⚠️ google-generativeai not installed. Install with: pip install google-generativeai")

class TransactionViewSet(viewsets.ModelViewSet):
    serializer_class = TransactionSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['transaction_type', 'category', 'bank_account']
    search_fields = ['description', 'merchant']
    ordering_fields = ['transaction_date', 'amount']
    ordering = ['-transaction_date']
    
    def get_queryset(self):
        return Transaction.objects.filter(user=self.request.user)
    
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

class BankAccountViewSet(viewsets.ModelViewSet):
    serializer_class = BankAccountSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        return BankAccount.objects.filter(user=self.request.user)
    
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

class CategoryViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    permission_classes = [IsAuthenticated]


def detect_category_with_gemini(description, merchant, amount=None):
    """
    AI-powered category detection using Google Gemini (FREE!)
    Falls back to keyword-based if Gemini unavailable
    """
    
    # Check if Gemini is available and configured
    if not GEMINI_AVAILABLE:
        print("⚠️ Gemini not available, using keyword fallback")
        return detect_category_fallback(description, merchant)
    
    api_key = os.getenv('GEMINI_API_KEY')
    if not api_key:
        print("⚠️ GEMINI_API_KEY not set, using keyword fallback")
        return detect_category_fallback(description, merchant)
    
    try:
        # Configure and initialize Gemini
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        # Prepare prompt
        prompt = f"""You are a financial transaction categorizer for Indian users. Analyze this transaction and assign it to ONE category.

Categories:
1. Food & Dining - Restaurants, groceries, cafes, food delivery, snacks, juice, beverages
2. Healthcare - Medical bills, pharmacy, hospital, doctor visits
3. Online Shopping - Amazon, Flipkart, e-commerce purchases, deliveries
4. Education - Books, stationery, tuition, courses, school/college fees
5. Shopping - Clothing, electronics, general shopping, accessories, mobile
6. Transportation - Fuel, taxi, public transport, parking, Uber, Ola
7. Entertainment - Movies, subscriptions (Netflix, Spotify), games, concerts, cinema
8. Bills & Utilities - Electricity, water, internet, mobile recharge
9. Personal Care - Salon, spa, grooming products
10. Personal Transfer - Money sent to friends, family, or personal contacts (names like Amma, Vishal, etc.)
11. Miscellaneous - Anything that doesn't fit above

Transaction:
- Description: {description}
- Merchant: {merchant}
{f'- Amount: ₹{amount}' if amount else ''}

Rules:
- Personal names without business context → Personal Transfer
- "movie" → Entertainment
- "juice" → Food & Dining
- Business names → Appropriate category
- Consider Indian context (PhonePe, UPI, Indian merchants)

Respond ONLY with JSON (no markdown, no explanation):
{{"category": "Category Name", "confidence": 0.95}}"""

        # Call Gemini API
        response = model.generate_content(prompt)
        result_text = response.text.strip()
        
        # Parse JSON response
        if '```json' in result_text:
            result_text = result_text.split('```json')[1].split('```')[0].strip()
        elif '```' in result_text:
            result_text = result_text.split('```')[1].split('```')[0].strip()
        
        result = json.loads(result_text)
        
        category = result.get('category', 'Miscellaneous')
        confidence = float(result.get('confidence', 0.7))
        
        print(f"🤖 Gemini AI: {description[:30]} → {category} ({confidence*100:.0f}%)")
        
        return category, confidence
        
    except Exception as e:
        print(f"⚠️ Gemini AI failed: {str(e)}, using fallback")
        return detect_category_fallback(description, merchant)


def detect_category_fallback(description, merchant):
    """
    Fallback keyword-based categorization
    Used when Gemini API fails or is unavailable
    """
    text = (description + ' ' + merchant).lower()
    original_text = description
    
    # Enhanced keywords with scoring
    category_keywords = {
        'Entertainment': {
            'keywords': ['movie', 'cinema', 'netflix', 'prime', 'spotify', 'hotstar', 
                        'zee5', 'saavn', 'film', 'theater', 'theatre', 'show'],
            'score': 3
        },
        'Food & Dining': {
            'keywords': ['food', 'juice', 'restaurant', 'cafe', 'coffee', 'tiffin',
                        'meal', 'dinner', 'lunch', 'breakfast', 'snack', 'drink',
                        'cool drink', 'sweet', 'biryani', 'pizza'],
            'score': 3
        },
        'Healthcare': {
            'keywords': ['medical', 'medicals', 'hospital', 'clinic', 'pharmacy',
                        'medicine', 'doctor', 'health', 'apollo'],
            'score': 3
        },
        'Online Shopping': {
            'keywords': ['amazon', 'flipkart', 'ekart', 'meesho', 'myntra', 'ajio',
                        'delivery', 'courier', 'online', 'ecommerce'],
            'score': 3
        },
        'Education': {
            'keywords': ['book', 'books', 'stationery', 'stationary', 'xerox',
                        'library', 'school', 'college', 'anits', 'university',
                        'tuition', 'education', 'cse'],
            'score': 3
        },
        'Shopping': {
            'keywords': ['shop', 'store', 'mall', 'mart', 'accessories', 'mobile',
                        'electronics', 'electrical', 'fancy', 'general'],
            'score': 2
        },
        'Transportation': {
            'keywords': ['uber', 'ola', 'rapido', 'taxi', 'cab', 'fuel', 'petrol',
                        'auto', 'bus', 'train'],
            'score': 2
        },
        'Bills & Utilities': {
            'keywords': ['electricity', 'water bill', 'internet', 'broadband', 
                        'recharge', 'bill', 'utility'],
            'score': 3
        }
    }
    
    best_match = None
    highest_score = 0
    
    for category, data in category_keywords.items():
        score = 0
        for keyword in data['keywords']:
            if keyword in text:
                score += data['score']
        
        if score > highest_score:
            highest_score = score
            best_match = category
    
    if best_match and highest_score >= 2:
        return best_match, 0.85
    
    # Personal transfer detection
    words = original_text.split()
    if len(words) <= 3:
        business_indicators = ['shop', 'store', 'mart', 'pvt', 'ltd', 'services',
                              'center', 'centre', 'hospital', 'clinic', 'pharmacy']
        has_business = any(biz in text for biz in business_indicators)
        
        if not has_business:
            # Check for personal name patterns
            personal_patterns = ['amma', 'akka', 'anna', '******', '@', 
                               'kumar', 'anil', 'chitti', 'yerra', 'vamsi',
                               'kalyan', 'preethi', 'vishal']
            has_personal = any(pattern in text for pattern in personal_patterns)
            
            if has_personal or len(words) <= 2:
                return 'Personal Transfer', 0.75
    
    return 'Miscellaneous', 0.40


def detect_category(description, merchant):
    """
    Main category detection function
    Uses Gemini AI if available, otherwise falls back to keywords
    """
    return detect_category_with_gemini(description, merchant)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def extract_pdf_transactions(request):
    """
    Extract transactions from PhonePe PDF statement with AI category detection
    """
    print("📄 PhonePe PDF Extraction Started")
    
    if 'file' not in request.FILES:
        print("❌ No file uploaded")
        return Response({'error': 'No file uploaded'}, status=400)
    
    uploaded_file = request.FILES['file']
    print(f"📎 File received: {uploaded_file.name}")
    
    if not uploaded_file.name.endswith('.pdf'):
        print("❌ Not a PDF file")
        return Response({'error': 'File must be a PDF'}, status=400)
    
    try:
        # Extract text from PDF
        try:
            import pdfplumber
            print("✅ Using pdfplumber")
            with pdfplumber.open(uploaded_file) as pdf:
                text = ''
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text += page_text + '\n'
            print(f"📝 Extracted {len(text)} characters")
        except ImportError:
            print("⚠️ pdfplumber not available, trying PyPDF2")
            import PyPDF2
            pdf_reader = PyPDF2.PdfReader(uploaded_file)
            text = ''
            for page in pdf_reader.pages:
                text += page.extract_text() + '\n'
            print(f"📝 Extracted {len(text)} characters with PyPDF2")
        
        if not text.strip():
            print("❌ No text extracted from PDF")
            return Response({'error': 'Could not extract text from PDF'}, status=400)
        
        # Parse PhonePe transactions
        transactions = []
        
        lines = text.split('\n')
        print(f"📋 Processing {len(lines)} lines")
        
        current_date = None
        current_description = None
        current_type = None
        current_amount = None
        
        extracted_count = 0
        
        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            
            # Look for date pattern
            date_match = re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),\s+(\d{4})', line)
            
            # Look for transaction type
            type_match = re.search(r'\b(DEBIT|CREDIT)\b', line)
            
            # Look for amount with Rupee symbol
            amount_match = re.search(r'₹([\d,]+)', line)
            
            # Look for description patterns
            paid_to = re.search(r'Paid to (.+)', line)
            received_from = re.search(r'Received from (.+)', line)
            payment_to = re.search(r'Payment to (.+)', line)
            
            # Collect transaction details
            if date_match:
                current_date = date_match.group(0)
            
            if type_match:
                current_type = type_match.group(1)
            
            if amount_match:
                current_amount = amount_match.group(1)
            
            if paid_to or received_from or payment_to:
                if paid_to:
                    current_description = paid_to.group(1).strip()
                elif received_from:
                    current_description = received_from.group(1).strip()
                elif payment_to:
                    current_description = payment_to.group(1).strip()
                
                # Clean up description
                # current_description = re.sub(r'Transaction ID.*', '', current_description).strip()
                # current_description = re.sub(r'UTR No\..*', '', current_description).strip()
                # current_description = re.sub(r'Paid by.*', '', current_description).strip()
                # current_description = re.sub(r'Credited to.*', '', current_description).strip()

                    # Remove Transaction ID, UTR, Paid by, Credited to
                current_description = re.sub(r'(Transaction ID|UTR No\.|Paid by|Credited to).*', '', current_description).strip()
                    # ✅ NEW: Remove "DEBIT ₹80" or "CREDIT ₹500" from description
                current_description = re.sub(r'\s*(DEBIT|CREDIT)\s*₹[\d,]+', '', current_description).strip()
                    # ✅ NEW: Remove any remaining ₹ amounts
                current_description = re.sub(r'₹[\d,]+', '', current_description).strip()
                    # ✅ NEW: Remove trailing DEBIT/CREDIT words
                current_description = re.sub(r'\s*(DEBIT|CREDIT)\s*$', '', current_description).strip()
            
            # When we have all components, create transaction
            if current_date and current_type and current_amount and current_description:
                try:
                    # Parse amount
                    amount = float(current_amount.replace(',', ''))
                    
                    # Skip very small amounts
                    if amount < 1:
                        current_date = None
                        current_description = None
                        current_type = None
                        current_amount = None
                        continue
                    
                    # Determine transaction type
                    transaction_type = 'expense' if current_type == 'DEBIT' else 'income'
                    
                    # Parse date
                    try:
                        transaction_date = datetime.strptime(current_date, '%b %d, %Y').date()
                    except:
                        transaction_date = datetime.now().date()
                    
                    # Extract merchant name
                    merchant = current_description.split()[0] if current_description else ''
                    merchant = merchant[:50]
                    
                    # 🤖 GEMINI AI CATEGORY DETECTION (with fallback)
                    detected_category_name, confidence = detect_category_with_gemini(
                        current_description, 
                        merchant,
                        amount
                    )
                    
                    # Get or create category
                    category, created = Category.objects.get_or_create(
                        name=detected_category_name,
                        defaults={'icon': '📁', 'color': '#3b82f6'}
                    )
                    
                    # Create transaction with category
                    transaction = Transaction.objects.create(
                        user=request.user,
                        description=current_description[:200],
                        amount=amount,
                        transaction_type=transaction_type,
                        transaction_date=transaction_date,
                        merchant=merchant,
                        category=category,
                        predicted_category=category,
                        prediction_confidence=confidence
                    )
                    
                    transactions.append({
                        'id': transaction.id,
                        'description': transaction.description,
                        'amount': str(transaction.amount),
                        'transaction_type': transaction.transaction_type,
                        'transaction_date': str(transaction.transaction_date),
                        'merchant': transaction.merchant,
                        'category': detected_category_name,
                        'confidence': round(confidence * 100, 1)
                    })
                    
                    extracted_count += 1
                    print(f"✅ Transaction {extracted_count}: {current_description[:30]} - ₹{amount} ({transaction_type}) → {detected_category_name} ({confidence*100:.0f}%)")
                    
                    # Reset for next transaction
                    current_date = None
                    current_description = None
                    current_type = None
                    current_amount = None
                    
                except Exception as e:
                    print(f"⚠️ Error creating transaction: {str(e)}")
                    current_date = None
                    current_description = None
                    current_type = None
                    current_amount = None
                    continue
        
        print(f"🎉 Successfully extracted {len(transactions)} transactions with AI categories")
        
        if len(transactions) == 0:
            return Response({
                'success': False,
                'transactions': [],
                'count': 0,
                'error': 'No valid transactions found in PDF'
            }, status=200)
        
        return Response({
            'success': True,
            'transactions': transactions,
            'count': len(transactions),
            'message': f'Successfully extracted {len(transactions)} transactions with AI categorization'
        }, status=status.HTTP_201_CREATED)
        
    except Exception as e:
        print(f"❌ PDF extraction failed: {str(e)}")
        import traceback
        traceback.print_exc()
        return Response({
            'error': f'Failed to process PDF: {str(e)}',
            'success': False
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)