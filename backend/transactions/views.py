# backend/transactions/views.py

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

# ── Groq AI ─────────────────────────────────────────────────────────────
try:
    from groq import Groq
    GROQ_AVAILABLE = True
except ImportError:
    GROQ_AVAILABLE = False
    print("⚠️ groq not installed. Run: pip install groq")


# ── ViewSets (unchanged) ──────────────────────────────────────────────────

class TransactionViewSet(viewsets.ModelViewSet):
    serializer_class = TransactionSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['transaction_type', 'category', 'bank_account'] #in admin.py see    
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


# ═══════════════════════════════════════════════════════════════════════════
# STEP 1 — Extract raw text from PDF
# ═══════════════════════════════════════════════════════════════════════════

def extract_text_from_pdf(uploaded_file):
    """Extract all text from a PDF file using pdfplumber (preferred) or PyPDF2."""
    try:
        import pdfplumber
        uploaded_file.seek(0)
        with pdfplumber.open(uploaded_file) as pdf:
            pages_text = []
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    pages_text.append(t)
        text = '\n'.join(pages_text)
        print(f"📝 pdfplumber extracted {len(text)} chars")
        return text
    except ImportError:
        pass

    try:
        import PyPDF2
        uploaded_file.seek(0)
        reader = PyPDF2.PdfReader(uploaded_file)
        text = '\n'.join(page.extract_text() or '' for page in reader.pages)
        print(f"📝 PyPDF2 extracted {len(text)} chars")
        return text
    except Exception as e:
        raise RuntimeError(f"Could not extract PDF text: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# STEP 2 — Let Groq parse the ENTIRE statement
# This is the key insight: instead of writing bank-specific parsers,
# we just give Groq the raw statement text and ask it to extract
# all transactions as structured JSON.
# ═══════════════════════════════════════════════════════════════════════════

GROQ_PARSE_PROMPT = """You are a financial data extraction expert for Indian bank statements.

I will give you raw text extracted from a bank statement PDF. Your job is to:
1. Find EVERY transaction in the text
2. Determine if each is income (money coming IN) or expense (money going OUT)
3. Extract a clean merchant/person name
4. Assign a spending category

TRANSACTION TYPE RULES — very important:
- A DEPOSIT / CREDIT / money received = "income"
- A WITHDRAWAL / DEBIT / money paid = "expense"
- For UCO/SBI style: if Withdrawals column has value → expense, Deposits column → income
- For PhonePe: DEBIT = expense, CREDIT = income
- For HDFC/ICICI: look for Dr/Cr suffix or column position

CATEGORIES (pick exactly one):
- Food & Dining       → Swiggy, Zomato, restaurants, cafes, food delivery
- Transportation      → Metro, DMRC, Uber, Ola, Rapido, fuel, airlines, bus
- Bills & Utilities   → Electricity, internet, mobile recharge, BSNL, APEPDCL, water
- Online Shopping     → Amazon, Flipkart, BigBasket, Meesho, Myntra, Ajio
- Healthcare          → hospitals, pharmacies, MosaicWellness, Apollo, medical
- Entertainment       → Netflix, Spotify, Hotstar, movies, games
- Education           → books, tuition, school fees, courses
- Personal Care       → salon, spa, grooming
- Personal Transfer   → money sent to/received from individual people (person names)
- Shopping            → general retail, clothing, electronics
- Miscellaneous       → anything else

MERCHANT NAME RULES:
- UPI strings like "MPAYUPITRTR654765337668DHARMASURYAPICICXXX1" → extract "DHARMASURYA"
- Remove bank codes: SBIN, YESB, HDFC, ICIC, PUNB, UCBA, INDB, KKBK, UTIB, BARB etc.
- Person names should be kept as-is (e.g. "Hemant Kashyap", "Bailapudi Venk")
- Service names should be human readable (e.g. "Swiggy", "Delhi Metro", "BigBasket")
- "SAATVIKSOUTH" → person name "Saatvik" → Personal Transfer
- "HEMANTKASHYAP" → person name "Hemant Kashyap" → Personal Transfer
- "ONE97COMMUNIC" → "Paytm" → if expense/debit → category = "Personal Transfer"
- "PHONEPEYESB" or "PHONEPE" → "PhonePe" → if expense/debit → category = "Personal Transfer"
- "BBNOWHDFCXXX" → "BigBasket"
- "DMRCNSP" → "Delhi Metro"
- "APEPDCL" → "Electricity (APEPDCL)"
- "MOSAICWELLNESS" or "MOSAIC" → "MosaicWellness"
- "ETERNALLIMITED" or "INDIGO" → "Indigo Airlines"
- "ZOMATOLIMITED" → "Zomato"

DATE FORMAT: Always output as YYYY-MM-DD

OUTPUT: Return ONLY a valid JSON array. No markdown, no explanation.
Each item must have these exact keys:
{
  "date": "YYYY-MM-DD",
  "merchant": "Clean readable name",
  "amount": 123.45,
  "type": "income" or "expense",
  "category": "Category Name"
}

If you cannot determine something with confidence, make your best guess.
Skip non-transaction rows (headers, opening balance, closing balance lines).

BANK STATEMENT TEXT:
"""


def parse_with_groq(text):
    """
    Send full bank statement text to Groq LLM
    and get back structured transactions JSON.
    """

    if not GROQ_AVAILABLE:
        print("⚠️ Groq not available, using fallback parser")
        return None

    api_key = os.getenv('GROQ_API_KEY')
    if not api_key:
        print("⚠️ GROQ_API_KEY not set, using fallback parser")
        return None

    try:
        client = Groq(api_key=api_key)  

        MAX_CHARS = 12000       # Groq can handle large inputs, but we chunk just in case of very long statements. We also want to avoid hitting token limits in the response. This is a safeguard, not a strict requirement.     
        chunks = [text[i:i+MAX_CHARS] for i in range(0, len(text), MAX_CHARS)]

        all_transactions = []

        for chunk_idx, chunk in enumerate(chunks):
            print(f"🤖 Groq parsing chunk {chunk_idx+1}/{len(chunks)}")

            prompt = GROQ_PARSE_PROMPT + chunk

            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {
                        "role": "system",
                        "content": "You are a financial data extraction expert. Return ONLY a valid JSON array. No markdown. No explanation."
                    },
                    {"role": "user", "content": prompt}
                ],
                temperature=0,
                max_tokens=8000
                # ❌ REMOVE response_format completely
            )
            # // Note: We do NOT use Groq's built-in response_format here because it can be too restrictive and may fail if the output is large or slightly malformed. Instead, we ask the model to return raw JSON and we handle parsing ourselves with robust error handling.
            result_text = response.choices[0].message.content.strip()

            # 🔥 Remove markdown wrapping if present
            if "```" in result_text:
                parts = result_text.split("```")
                if len(parts) >= 2:
                    result_text = parts[1]
                result_text = result_text.replace("json", "").strip()

            start = result_text.find('[')      # find first [ to allow for any leading text
            end = result_text.rfind(']')    # find last ] to allow for nested objects

            if start != -1 and end != -1:
                result_text = result_text[start:end+1]

            try:
                parsed = json.loads(result_text)
            except Exception as e:
                print(f"⚠️ JSON decode failed: {e}")
                print("Raw Groq output preview:", result_text[:500])
                continue

            # 🔥 Accept both list and wrapped object
            if isinstance(parsed, list): #// Groq might return a list directly, or an object with a "transactions" key containing the list. We handle both cases.
                chunk_transactions = parsed

            elif isinstance(parsed, dict):
                if "transactions" in parsed:
                    chunk_transactions = parsed["transactions"]  # // Some versions of the prompt might ask for {"transactions": [ ... ]} format. We check for that and extract the list.
                else:
                    chunk_transactions = [parsed]
            else:
                print("⚠️ Unexpected structure from Groq")
                continue

            print(f"  ✅ Chunk {chunk_idx+1}: {len(chunk_transactions)} transactions")
            all_transactions.extend(chunk_transactions)

        print(f"🤖 Groq total: {len(all_transactions)} transactions extracted")
        return all_transactions

    except Exception as e:
        print(f"⚠️ Groq parsing failed: {e}")
        return None

# ═══════════════════════════════════════════════════════════════════════════
# STEP 3 — Fallback: rule-based parsers when Gemini is unavailable
# ═══════════════════════════════════════════════════════════════════════════

def detect_bank_format(text):
    """Detect bank format from statement headers."""
    t = text.lower()
    if 'uco bank' in t or 'ucba' in t:           return 'uco_bank'
    if 'state bank of india' in t:               return 'sbi'
    if 'hdfc bank' in t[:500]:                   return 'hdfc'
    if 'icici bank' in t[:500]:                  return 'icici'
    if 'axis bank' in t[:500]:                   return 'axis'
    if 'kotak' in t[:500]:                       return 'kotak'
    if 'paid to' in t and 'received from' in t:  return 'phonepe'
    return 'unknown'


def _clean_upi_merchant(raw):
    """Extract readable merchant name from raw UPI/bank description string."""
    if not raw:
        return 'Unknown'

    r = raw.upper()

    # Known services — check before generic extraction
    services = {
        'SWIGGY': 'Swiggy', 'ZOMATO': 'Zomato',
        'BSNL': 'BSNL', 'APEPDCL': 'Electricity (APEPDCL)',
        'DMRCNSP': 'Delhi Metro', 'DMRC': 'Delhi Metro',
        'NOIDAMETRO': 'Noida Metro',
        'MOSAICWELLNESS': 'MosaicWellness', 'MOSAIC': 'MosaicWellness',
        'BBNOW': 'BigBasket', 'BIGBASKET': 'BigBasket',
        'ONE97': 'Paytm', 'PAYTM': 'Paytm',
        'PHONEPE': 'PhonePe',
        'AMAZON': 'Amazon', 'FLIPKART': 'Flipkart',
        'NETFLIX': 'Netflix', 'SPOTIFY': 'Spotify', 'HOTSTAR': 'Hotstar',
        'UBER': 'Uber', 'OLA': 'Ola', 'RAPIDO': 'Rapido',
        'ETERNALLIMITED': 'Indigo Airlines', 'INDIGO': 'Indigo Airlines',
        'ZOMATOLIMITED': 'Zomato',
        'UPIRRC': 'Refund',
    }
    for key, name in services.items():
        if key in r:
            return name

    # Generic UPI extraction: MPAYUPITRTR<digits><NAME><BANKCODE>
    m = re.search(r'MPAYUPITRTR\d+([A-Za-z]+)', raw)
    if m:
        name = m.group(1)
        name = re.sub(
            r'(PICIC|SBIN|YESBXXX|YESB|PUNB|HDFC|ICIC|INDB|KKBK|PPIW|UTIB|AIRP|BARB|NSPB|UCBA|CBAX)',
            '', name, flags=re.IGNORECASE
        ).strip()
        if name:
            return name.title()

    return raw[:50].strip()


def _fallback_category(merchant, description):
    """Keyword-based category fallback."""
    text = (merchant + ' ' + description).lower()
    mapping = {
        'Food & Dining':     ['swiggy', 'zomato', 'restaurant', 'food', 'cafe', 'juice'],
        'Transportation':    ['metro', 'dmrc', 'uber', 'ola', 'rapido', 'fuel', 'petrol', 'airline', 'indigo', 'eternal'],
        'Bills & Utilities': ['bsnl', 'electricity', 'apepdcl', 'recharge', 'internet', 'broadband', 'bill'],
        'Online Shopping':   ['amazon', 'flipkart', 'bigbasket', 'bbnow', 'meesho', 'myntra', 'ajio'],
        'Healthcare':        ['hospital', 'pharmacy', 'medical', 'mosaic', 'wellness', 'apollo', 'doctor'],
        'Entertainment':     ['netflix', 'spotify', 'hotstar', 'movie', 'cinema', 'game'],
        'Education':         ['book', 'tuition', 'school', 'college', 'course'],
        'Personal Care':     ['salon', 'spa', 'grooming'],
        'Shopping':          ['shop', 'store', 'mart', 'mall'],
    }
    for category, keywords in mapping.items():
        if any(k in text for k in keywords):
            return category

    # Short description with no business keywords → likely a person
    if len(merchant.split()) <= 3 and not any(
        x in text for x in ['pvt', 'ltd', 'services', 'store', 'shop', 'mart']
    ):
        return 'Personal Transfer'

    return 'Miscellaneous'


def parse_uco_bank_fallback(text):
    """
    Balance-diff based fallback for UCO Bank.
    If balance increased → income, decreased → expense.
    """
    transactions = []
    lines = text.split('\n')
    prev_balance = None

    for i, line in enumerate(lines):
        line = line.strip()
        m = re.match(
            r'^(\d{2}-[A-Z][a-z]{2}-\d{4})\s+([\d,]+(?:\.\d{1,2})?)\s+([\d,]+(?:\.\d{1,2})?)$',
            line
        )
        if not m:
            continue

        date_str   = m.group(1)
        amount     = float(m.group(2).replace(',', ''))
        balance    = float(m.group(3).replace(',', ''))
        raw_desc   = lines[i-1].strip() if i > 0 else ''
        merchant   = _clean_upi_merchant(raw_desc)

        if prev_balance is not None:
            tx_type = 'income' if balance > prev_balance else 'expense'
        else:
            tx_type = 'income' if amount < balance else 'expense'

        prev_balance = balance

        if amount >= 1:
            transactions.append({
                'date': datetime.strptime(date_str, '%d-%b-%Y').strftime('%Y-%m-%d'),
                'merchant': merchant,
                'amount': amount,
                'type': tx_type,
                'category': _fallback_category(merchant, raw_desc)
            })

    print(f"📊 UCO fallback: {len(transactions)} transactions")
    return transactions


def parse_phonepe_fallback(text):
    """Rule-based fallback for PhonePe statements."""
    transactions = []
    lines = text.split('\n')
    cur_date = cur_desc = cur_type = cur_amount = None

    for line in lines:
        line = line.strip()
        if not line:
            continue

        dm = re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),\s+(\d{4})', line)
        tm = re.search(r'\b(DEBIT|CREDIT)\b', line)
        am = re.search(r'₹([\d,]+)', line)
        pm = re.search(r'(?:Paid to|Received from|Payment to)\s+(.+)', line)

        if dm: cur_date   = dm.group(0)
        if tm: cur_type   = tm.group(1)
        if am: cur_amount = am.group(1)
        if pm:
            cur_desc = re.sub(r'(Transaction ID|UTR No\.|Paid by|Credited to).*', '', pm.group(1)).strip()
            cur_desc = re.sub(r'\s*(DEBIT|CREDIT)\s*₹[\d,]+', '', cur_desc).strip()

        if all([cur_date, cur_type, cur_amount, cur_desc]):
            try:
                amount = float(cur_amount.replace(',', ''))
                if amount >= 1:
                    tx_type = 'expense' if cur_type == 'DEBIT' else 'income'
                    merchant = cur_desc.split()[0] if cur_desc else 'Unknown'
                    transactions.append({
                        'date': datetime.strptime(cur_date, '%b %d, %Y').strftime('%Y-%m-%d'),
                        'merchant': cur_desc,
                        'amount': amount,
                        'type': tx_type,
                        'category': _fallback_category(merchant, cur_desc)
                    })
            except:
                pass
            cur_date = cur_desc = cur_type = cur_amount = None

    print(f"📊 PhonePe fallback: {len(transactions)} transactions")
    return transactions


def parse_fallback(text, bank_format):
    """Route to the right fallback parser based on detected bank."""
    if bank_format == 'uco_bank':
        return parse_uco_bank_fallback(text)
    elif bank_format == 'phonepe':
        return parse_phonepe_fallback(text)
    else:
        # Try UCO format first, then PhonePe
        result = parse_uco_bank_fallback(text)
        if result:
            return result
        return parse_phonepe_fallback(text)


# ═══════════════════════════════════════════════════════════════════════════
# STEP 4 — Validate and normalize Gemini output
# ═══════════════════════════════════════════════════════════════════════════

VALID_CATEGORIES = {
    'Food & Dining', 'Transportation', 'Bills & Utilities', 'Online Shopping',
    'Healthcare', 'Entertainment', 'Education', 'Personal Care',
    'Personal Transfer', 'Shopping', 'Miscellaneous'
}

def normalize_transaction(raw):
    """
    Validate and clean a single transaction dict from Groq.
    Returns None if the transaction is invalid/incomplete.
    """
    try:
        # Date — accept YYYY-MM-DD or fallback
        date_str = str(raw.get('date', '')).strip()
        try:
            transaction_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except:
            # Try other common formats
            for fmt in ('%d-%m-%Y', '%d/%m/%Y', '%d-%b-%Y', '%b %d, %Y'):
                try:
                    transaction_date = datetime.strptime(date_str, fmt).date()
                    break
                except:
                    continue
            else:
                from django.utils import timezone
                transaction_date = timezone.now().date()

        # Amount
        amount = float(str(raw.get('amount', 0)).replace(',', ''))  # Remove commas from amounts like "1,234.56"
        if amount < 1:
            return None  # Skip dust amounts

        # Type
        tx_type = str(raw.get('type', 'expense')).lower().strip()   # 
        if tx_type not in ('income', 'expense'):
            tx_type = 'expense'

        # Merchant
        merchant = str(raw.get('merchant', 'Unknown')).strip()[:100]
        if not merchant or merchant.lower() in ('none', 'null', ''):
            merchant = 'Unknown'

        # Category
        category = str(raw.get('category', 'Miscellaneous')).strip()
        if category not in VALID_CATEGORIES:
            category = _fallback_category(merchant, merchant)

        return {
            'date': transaction_date,
            'merchant': merchant,
            'amount': amount,
            'type': tx_type,
            'category': category,
        }

    except Exception as e:
        print(f"  ⚠️ Skipping invalid transaction {raw}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════
# STEP 5 — Main upload endpoint
# ═══════════════════════════════════════════════════════════════════════════

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def extract_pdf_transactions(request):
    """
    Universal PDF parser — works for any Indian bank statement.

    Flow:
    1. Extract text from PDF
    2. Try Groq AI (sends full text, gets back structured JSON)
    3. If Groq fails → use rule-based fallback for known formats
    4. Save transactions to DB
    """
    print("\n" + "="*60)
    print("📄 Universal PDF Parser Started")
    print("="*60)

    if 'file' not in request.FILES:
        return Response({'error': 'No file uploaded'}, status=400)

    uploaded_file = request.FILES['file']
    print(f"📎 File: {uploaded_file.name}")

    if not uploaded_file.name.lower().endswith('.pdf'):
        return Response({'error': 'File must be a PDF'}, status=400)

    try:
        # ── Extract text ────────────────────────────────────────────────
        text = extract_text_from_pdf(uploaded_file)

        if not text.strip():
            return Response({'error': 'Could not extract text from PDF'}, status=400)

        # ── Detect bank (for fallback routing only) ─────────────────────
        bank_format = detect_bank_format(text)  
        print(f"🏦 Detected format: {bank_format}")

        # ── Parse with Groq (primary) ──────────────────────────────────
        raw_transactions = parse_with_groq(text)

        # ── Parse with fallback if Groq failed ─────────────────────────
        if not raw_transactions:
            print("⚠️ GROQ unavailable/failed — using rule-based fallback")
            raw_transactions = parse_fallback(text, bank_format)

        if not raw_transactions:
            return Response({
                'success': False,
                'error': 'No transactions found in the PDF.',
                'transactions': [],
                'count': 0
            }, status=200)

        # ── Normalize and save ───────────────────────────────────────────
        created_transactions = []

        for raw in raw_transactions:
            normalized = normalize_transaction(raw)
            if not normalized:
                continue

            try:
                category_obj, _ = Category.objects.get_or_create(
                    name=normalized['category'],
                    defaults={'icon': '📁', 'color': '#3b82f6'}
                )

                transaction = Transaction.objects.create(
                    user=request.user,
                    description=normalized['merchant'],
                    amount=normalized['amount'],
                    transaction_type=normalized['type'],
                    transaction_date=normalized['date'],
                    merchant=normalized['merchant'][:50],
                    category=category_obj,
                    predicted_category=category_obj,
                    prediction_confidence=0.92 if GROQ_AVAILABLE else 0.70,
                )

                created_transactions.append({
                    'id': transaction.id,
                    'description': transaction.description,
                    'amount': str(transaction.amount),
                    'transaction_type': transaction.transaction_type,
                    'transaction_date': str(transaction.transaction_date),
                    'merchant': transaction.merchant,
                    'category': normalized['category'],
                })

                print(f"  ✅ {normalized['type'].upper():7} | {str(normalized['date'])} | "
                      f"{normalized['merchant'][:25]:25} | ₹{normalized['amount']:.2f} | "
                      f"{normalized['category']}")

            except Exception as e:
                print(f"  ⚠️ DB save error: {e}")
                continue

        print(f"\n🎉 Done — {len(created_transactions)} transactions saved ({bank_format})")

        return Response({
            'success': True,
            'transactions': created_transactions,
            'count': len(created_transactions),
            'bank_format': bank_format,
            'parser': 'groq_ai' if GROQ_AVAILABLE else 'rule_based_fallback',
            'message': f'Successfully extracted {len(created_transactions)} transactions'
        }, status=status.HTTP_201_CREATED)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return Response({'error': str(e), 'success': False}, status=500)



# # backend/transactions/views.py - FIXED UNIVERSAL PDF PARSER
# from rest_framework import viewsets, status
# from rest_framework.decorators import api_view, permission_classes
# from rest_framework.response import Response
# from rest_framework.permissions import IsAuthenticated
# from django_filters.rest_framework import DjangoFilterBackend
# from rest_framework import filters
# import re
# from datetime import datetime
# import os
# import json

# from .models import Transaction, BankAccount, Category
# from .serializers import TransactionSerializer, BankAccountSerializer, CategorySerializer

# # Gemini AI Integration
# try:
#     from google import genai
#     GEMINI_AVAILABLE = True
# except ImportError:
#     GEMINI_AVAILABLE = False
#     print("⚠️ google-genai not installed. Install with: pip install google-genai")

# class TransactionViewSet(viewsets.ModelViewSet):
#     serializer_class = TransactionSerializer
#     permission_classes = [IsAuthenticated]
#     filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
#     filterset_fields = ['transaction_type', 'category', 'bank_account']
#     search_fields = ['description', 'merchant']
#     ordering_fields = ['transaction_date', 'amount']
#     ordering = ['-transaction_date']
    
#     def get_queryset(self):
#         return Transaction.objects.filter(user=self.request.user)
    
#     def perform_create(self, serializer):
#         serializer.save(user=self.request.user)

# class BankAccountViewSet(viewsets.ModelViewSet):
#     serializer_class = BankAccountSerializer
#     permission_classes = [IsAuthenticated]
    
#     def get_queryset(self):
#         return BankAccount.objects.filter(user=self.request.user)
    
#     def perform_create(self, serializer):
#         serializer.save(user=self.request.user)

# class CategoryViewSet(viewsets.ReadOnlyModelViewSet):
#     queryset = Category.objects.all()
#     serializer_class = CategorySerializer
#     permission_classes = [IsAuthenticated]


# def clean_description(description):
#     """Clean transaction description"""
#     cleaned = description
#     cleaned = re.sub(r'\s*(DEBIT|CREDIT)\s*₹[\d,]+', '', cleaned).strip()
#     cleaned = re.sub(r'₹[\d,]+', '', cleaned).strip()
#     cleaned = re.sub(r'\s*(DEBIT|CREDIT)\s*$', '', cleaned).strip()
#     cleaned = re.sub(r'(Transaction ID|UTR No\.|Paid by|Credited to).*', '', cleaned).strip()
#     cleaned = ' '.join(cleaned.split())
#     return cleaned


# def detect_bank_format(text):
#     """Detect bank format - checks headers first"""
#     text_lower = text.lower()
    
#     if 'uco bank' in text_lower or 'ucba' in text_lower or 'ucobank' in text_lower:
#         print("🏦 Detected: UCO Bank (from header)")
#         return 'uco_bank'
    
#     if 'state bank of india' in text_lower or 'sbi' in text_lower[:500]:
#         print("🏦 Detected: SBI (from header)")
#         return 'sbi'
    
#     if 'hdfc bank' in text_lower[:500]:
#         print("🏦 Detected: HDFC (from header)")
#         return 'hdfc'
    
#     if 'icici bank' in text_lower[:500]:
#         print("🏦 Detected: ICICI (from header)")
#         return 'icici'
    
#     if 'paid to' in text_lower and 'received from' in text_lower:
#         print("🏦 Detected: PhonePe (from transaction format)")
#         return 'phonepe'
    
#     print("⚠️ Unknown bank format")
#     return 'unknown'


# # ============================================================
# # FIX 1: Use pdfplumber table extraction for UCO Bank
# # ============================================================
# def parse_uco_bank_with_tables(pdf_file):
#     """
#     Parse UCO Bank using pdfplumber TABLE extraction.
    
#     UCO Bank statement has columns: Date | Particulars | Withdrawals | Deposits | Balance
    
#     Text extraction merges columns and loses withdrawal vs deposit info.
#     Table extraction preserves each column separately — THIS IS THE FIX.
#     """
#     transactions = []
    
#     try:
#         import pdfplumber
#     except ImportError:
#         print("⚠️ pdfplumber not available")
#         return transactions
    
#     print("📋 Parsing UCO Bank using TABLE extraction (fixed method)")
    
#     with pdfplumber.open(pdf_file) as pdf:
#         for page_num, page in enumerate(pdf.pages):
            
#             # Extract tables from this page
#             tables = page.extract_tables()
            
#             if not tables:
#                 print(f"  Page {page_num+1}: No tables found, skipping")
#                 continue
            
#             for table in tables:
#                 for row in table:
#                     if not row:
#                         continue
                    
#                     # Clean each cell
#                     row = [str(cell).strip() if cell else '' for cell in row]
                    
#                     # Skip header rows
#                     if any(h in row[0].lower() for h in ['date', 'particulars', 'opening']):
#                         continue
                    
#                     # UCO Bank row format: [Date, Particulars, Withdrawals, Deposits, Balance]
#                     # But pdfplumber may return varying column counts
#                     # We look for a row that starts with a date pattern
                    
#                     date_match = re.match(r'\d{2}-[A-Z][a-z]{2}-\d{4}', row[0])
#                     if not date_match:
#                         continue
                    
#                     transaction_date_str = row[0]
                    
#                     # ============================================================
#                     # FIX 2: Correctly identify Withdrawals vs Deposits columns
#                     # UCO Bank: col[2]=Withdrawals, col[3]=Deposits, col[4]=Balance
#                     # ============================================================
                    
#                     withdrawal_amount = None
#                     deposit_amount = None
#                     description = ''
                    
#                     if len(row) >= 5:
#                         # Standard 5-column format
#                         description = row[1].strip()
#                         withdrawal_str = row[2].strip()
#                         deposit_str = row[3].strip()
                        
#                         # Parse withdrawal (expense)
#                         if withdrawal_str and withdrawal_str not in ['-', '', 'None', 'nan']:
#                             try:
#                                 withdrawal_amount = float(withdrawal_str.replace(',', ''))
#                             except:
#                                 pass
                        
#                         # Parse deposit (income)
#                         if deposit_str and deposit_str not in ['-', '', 'None', 'nan']:
#                             try:
#                                 deposit_amount = float(deposit_str.replace(',', ''))
#                             except:
#                                 pass
                    
#                     elif len(row) == 4:
#                         # Sometimes merges to 4 cols: Date | Particulars | Amount | Balance
#                         # Can't tell withdrawal vs deposit — skip or mark as unknown
#                         description = row[1].strip()
#                         print(f"  ⚠️ 4-col row on {transaction_date_str}, can't determine direction: {row}")
#                         continue
                    
#                     else:
#                         print(f"  ⚠️ Unexpected row format ({len(row)} cols): {row}")
#                         continue
                    
#                     # Clean description
#                     clean_desc = extract_uco_description(description)
                    
#                     # Create transaction for withdrawal
#                     if withdrawal_amount and withdrawal_amount >= 1:
#                         transactions.append({
#                             'date': transaction_date_str,
#                             'description': clean_desc,
#                             'amount': withdrawal_amount,
#                             'type': 'expense',          # ✅ Withdrawal = expense
#                             'merchant': clean_desc.split()[0] if clean_desc.split() else 'Unknown'
#                         })
#                         print(f"  ✅ EXPENSE: {transaction_date_str} | {clean_desc[:25]:25} | ₹{withdrawal_amount:.2f}")
                    
#                     # Create transaction for deposit
#                     if deposit_amount and deposit_amount >= 1:
#                         transactions.append({
#                             'date': transaction_date_str,
#                             'description': clean_desc,
#                             'amount': deposit_amount,
#                             'type': 'income',           # ✅ Deposit = income
#                             'merchant': clean_desc.split()[0] if clean_desc.split() else 'Unknown'
#                         })
#                         print(f"  ✅ INCOME:  {transaction_date_str} | {clean_desc[:25]:25} | ₹{deposit_amount:.2f}")
    
#     print(f"📊 UCO Bank table extraction: {len(transactions)} transactions")
#     return transactions


# def extract_uco_description(raw_description):
#     """
#     Clean UCO Bank UPI description strings.
#     Input:  'MPAYUPITRTR654765337668DHARMASURYAPICICXXX1'
#     Output: 'DHARMASURYA'
#     """
#     if not raw_description:
#         return 'Unknown'
    
#     # Special cases first
#     if 'UPIRRC' in raw_description:
#         return 'Refund'
#     if 'ONE97' in raw_description or 'PAYTM' in raw_description.upper():
#         return 'Paytm'
#     if 'SWIGGY' in raw_description.upper():
#         return 'Swiggy'
#     if 'ZOMATO' in raw_description.upper():
#         return 'Zomato'
#     if 'BSNL' in raw_description.upper():
#         return 'BSNL Bill'
#     if 'DMRC' in raw_description.upper():
#         return 'Delhi Metro'
#     if 'MOSAIC' in raw_description.upper() or 'WELLNESS' in raw_description.upper():
#         return 'MosaicWellness'
#     if 'BBNOW' in raw_description.upper():
#         return 'BigBasket'
#     if 'APEPDCL' in raw_description.upper():
#         return 'Electricity Bill'
#     if 'NOIDA' in raw_description.upper() or 'METRO' in raw_description.upper():
#         return 'Metro'
#     if 'ETERNAL' in raw_description.upper() or 'AIRP' in raw_description.upper():
#         return 'Airport/Travel'
    
#     # Extract merchant from UPI string: MPAYUPITRTR<digits><MERCHANTNAME><BANKCODE>
#     merchant_match = re.search(r'MPAYUPITRTR\d+([A-Za-z]+)', raw_description)
#     if merchant_match:
#         merchant = merchant_match.group(1)
#         # Remove trailing bank codes
#         merchant = re.sub(
#             r'(PICIC|SBIN|YESBXXX|PUNB|HDFC|ICIC|INDB|KKBK|PPIW|UTIB|AIRP|BARB|NSPB|UCBA|CBAX).*',
#             '', merchant
#         ).strip()
#         if merchant:
#             return merchant
    
#     # Fallback: first 50 chars
#     return raw_description[:50].strip()


# # ============================================================
# # FIX 3: Fallback text parser (if table extraction fails)
# # Uses balance change to determine withdrawal vs deposit
# # ============================================================
# def parse_uco_bank_text_fallback(text):
#     """
#     Fallback text parser for UCO Bank.
#     Uses balance INCREASE = deposit (income), DECREASE = withdrawal (expense).
#     This works regardless of column order issues.
#     """
#     transactions = []
#     lines = text.split('\n')
    
#     print(f"📋 UCO Bank text fallback parser ({len(lines)} lines)")
    
#     # Regex: date + ONE amount + balance
#     # e.g. "04-Jan-2026 1800 2300.58"
#     # We determine type by comparing with previous balance
    
#     prev_balance = None
    
#     for i, line in enumerate(lines):
#         line = line.strip()
        
#         # Match: date, single amount, balance
#         m = re.match(
#             r'^(\d{2}-[A-Z][a-z]{2}-\d{4})\s+([\d,]+(?:\.\d{1,2})?)\s+([\d,]+(?:\.\d{1,2})?)$',
#             line
#         )
        
#         if m:
#             date_str = m.group(1)
#             amount = float(m.group(2).replace(',', ''))
#             balance = float(m.group(3).replace(',', ''))
            
#             # Get description from previous line
#             description_raw = lines[i-1].strip() if i > 0 else ''
#             description = extract_uco_description(description_raw)
            
#             # ✅ FIX: Use balance change to determine type
#             # If balance went UP → deposit (income)
#             # If balance went DOWN → withdrawal (expense)
#             if prev_balance is not None:
#                 balance_change = balance - prev_balance
#                 if balance_change > 0:
#                     transaction_type = 'income'
#                 else:
#                     transaction_type = 'expense'
#             else:
#                 # First transaction — use amount vs balance heuristic
#                 # If amount == balance - opening_balance (approx), it's a deposit
#                 transaction_type = 'income' if amount < balance else 'expense'
            
#             prev_balance = balance
            
#             if amount >= 1:
#                 transactions.append({
#                     'date': date_str,
#                     'description': description,
#                     'amount': amount,
#                     'type': transaction_type,
#                     'merchant': description.split()[0] if description.split() else 'Unknown'
#                 })
#                 print(f"  ✅ {transaction_type.upper():7}: {date_str} | {description[:25]:25} | ₹{amount:.2f} | bal:{balance:.2f}")
    
#     print(f"📊 UCO Bank text fallback: {len(transactions)} transactions")
#     return transactions


# def parse_uco_bank_statement(pdf_file_or_text, is_file=False):
#     """
#     Main UCO Bank parser.
#     Tries table extraction first (most accurate), falls back to text with balance-diff logic.
    
#     Args:
#         pdf_file_or_text: file object if is_file=True, else text string
#         is_file: True if passing file object, False if passing text
#     """
#     if is_file:
#         # Try table-based extraction first
#         transactions = parse_uco_bank_with_tables(pdf_file_or_text)
#         if transactions:
#             return transactions
        
#         # Table extraction failed, extract text and use fallback
#         print("⚠️ Table extraction returned 0 results, trying text fallback...")
#         import pdfplumber
#         pdf_file_or_text.seek(0)  # Reset file pointer
#         with pdfplumber.open(pdf_file_or_text) as pdf:
#             text = ''
#             for page in pdf.pages:
#                 t = page.extract_text()
#                 if t:
#                     text += t + '\n'
#         return parse_uco_bank_text_fallback(text)
#     else:
#         # Text was already extracted, use fallback
#         return parse_uco_bank_text_fallback(pdf_file_or_text)


# def parse_phonepe_statement(text):
#     """Parse PhonePe statement format"""
#     transactions = []
#     lines = text.split('\n')
    
#     print(f"📋 Parsing PhonePe format")
    
#     current_date = None
#     current_description = None
#     current_type = None
#     current_amount = None
    
#     for line in lines:
#         line = line.strip()
#         if not line:
#             continue
        
#         date_match = re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),\s+(\d{4})', line)
#         type_match = re.search(r'\b(DEBIT|CREDIT)\b', line)
#         amount_match = re.search(r'₹([\d,]+)', line)
#         paid_to = re.search(r'Paid to (.+)', line)
#         received_from = re.search(r'Received from (.+)', line)
#         payment_to = re.search(r'Payment to (.+)', line)
        
#         if date_match:
#             current_date = date_match.group(0)
        
#         if type_match:
#             current_type = type_match.group(1)
        
#         if amount_match:
#             current_amount = amount_match.group(1)
        
#         if paid_to or received_from or payment_to:
#             if paid_to:
#                 current_description = paid_to.group(1).strip()
#             elif received_from:
#                 current_description = received_from.group(1).strip()
#             elif payment_to:
#                 current_description = payment_to.group(1).strip()
            
#             current_description = clean_description(current_description)
        
#         if all([current_date, current_type, current_amount, current_description]):
#             try:
#                 amount = float(current_amount.replace(',', ''))
                
#                 if amount >= 1:
#                     # ✅ PhonePe is simple: DEBIT=expense, CREDIT=income
#                     transaction_type = 'expense' if current_type == 'DEBIT' else 'income'
                    
#                     transactions.append({
#                         'date': current_date,
#                         'description': current_description,
#                         'amount': amount,
#                         'type': transaction_type,
#                         'merchant': current_description.split()[0] if current_description else 'Unknown'
#                     })
                    
#                     print(f"✅ PhonePe: {current_date} - {current_description[:30]} - ₹{amount} ({transaction_type})")
                
#                 current_date = current_description = current_type = current_amount = None
                
#             except:
#                 current_date = current_description = current_type = current_amount = None
    
#     return transactions


# def detect_category_with_gemini(description, merchant, amount=None):
#     """AI-powered category detection using Gemini"""
#     if not GEMINI_AVAILABLE:
#         return detect_category_fallback(description, merchant)
    
#     api_key = os.getenv('GEMINI_API_KEY')
#     if not api_key:
#         return detect_category_fallback(description, merchant)
    
#     try:
#         client = genai.Client(api_key=api_key)
        
#         prompt = f"""Categorize this Indian bank transaction into ONE category:

# Categories:
# 1. Food & Dining
# 2. Healthcare  
# 3. Online Shopping
# 4. Education
# 5. Shopping
# 6. Transportation
# 7. Entertainment
# 8. Bills & Utilities
# 9. Personal Care
# 10. Personal Transfer
# 11. Miscellaneous

# Transaction:
# - Description: {description}
# - Merchant: {merchant}
# - Amount: ₹{amount}

# Rules:
# - Names like DHARMASURYA, Bailapudi, Hemant, Saatvik → Personal Transfer
# - PHONEPE, PhonePe → Online Shopping
# - DMRC, Metro → Transportation
# - Swiggy, Zomato → Food & Dining
# - BSNL, recharge → Bills & Utilities

# Response format: {{"category": "Name", "confidence": 0.95}}"""
        
#         response = client.models.generate_content(model='gemini-2.0-flash', contents=prompt)
#         result_text = response.text.strip()
        
#         if '```json' in result_text:
#             result_text = result_text.split('```json')[1].split('```')[0].strip()
#         elif '```' in result_text:
#             result_text = result_text.split('```')[1].split('```')[0].strip()
        
#         result = json.loads(result_text)
#         return result.get('category', 'Miscellaneous'), float(result.get('confidence', 0.7))
        
#     except Exception as e:
#         print(f"⚠️ Gemini failed: {e}")
#         return detect_category_fallback(description, merchant)


# def detect_category_fallback(description, merchant):
#     """Keyword-based fallback"""
#     text = (description + ' ' + merchant).lower()
    
#     keywords = {
#         'Food & Dining': ['food', 'swiggy', 'zomato', 'restaurant', 'juice'],
#         'Transportation': ['metro', 'dmrc', 'uber', 'ola', 'taxi', 'airport'],
#         'Bills & Utilities': ['bsnl', 'recharge', 'electricity', 'internet', 'apepdcl'],
#         'Online Shopping': ['amazon', 'flipkart', 'bigbasket', 'bbnow'],
#         'Entertainment': ['netflix', 'spotify', 'movie'],
#         'Healthcare': ['medical', 'hospital', 'pharmacy', 'mosaic', 'wellness'],
#         'Education': ['book', 'stationery', 'tuition'],
#     }
    
#     for category, words in keywords.items():
#         if any(word in text for word in words):
#             return category, 0.85
    
#     if len(description.split()) <= 3 and not any(x in text for x in ['pvt', 'ltd', 'shop']):
#         return 'Personal Transfer', 0.75
    
#     return 'Miscellaneous', 0.40


# @api_view(['POST'])
# @permission_classes([IsAuthenticated])
# def extract_pdf_transactions(request):
#     """
#     UNIVERSAL PDF PARSER - Supports all bank formats
#     """
#     print("📄 Universal PDF Extraction Started")
    
#     if 'file' not in request.FILES:
#         return Response({'error': 'No file uploaded'}, status=400)
    
#     uploaded_file = request.FILES['file']
#     print(f"📎 File: {uploaded_file.name}")
    
#     if not uploaded_file.name.endswith('.pdf'):
#         return Response({'error': 'File must be a PDF'}, status=400)
    
#     try:
#         # Extract text for bank format detection
#         try:
#             import pdfplumber
#             with pdfplumber.open(uploaded_file) as pdf:
#                 text = ''
#                 for page in pdf.pages:
#                     page_text = page.extract_text()
#                     if page_text:
#                         text += page_text + '\n'
#             uploaded_file.seek(0)  # Reset for table extraction later
#         except ImportError:
#             import PyPDF2
#             pdf_reader = PyPDF2.PdfReader(uploaded_file)
#             text = ''
#             for page in pdf_reader.pages:
#                 text += page.extract_text() + '\n'
        
#         print(f"📝 Extracted {len(text)} characters")
        
#         if not text.strip():
#             return Response({'error': 'Could not extract text from PDF'}, status=400)
        
#         # Detect bank format
#         bank_format = detect_bank_format(text)
#         print(f"🏦 Detected format: {bank_format}")
        
#         # ============================================================
#         # KEY CHANGE: Pass the file object for UCO Bank so we can use
#         # table extraction (much more accurate than text extraction)
#         # ============================================================
#         if bank_format == 'uco_bank':
#             try:
#                 parsed_transactions = parse_uco_bank_statement(uploaded_file, is_file=True)
#             except Exception as e:
#                 print(f"⚠️ Table extraction error: {e}, falling back to text")
#                 parsed_transactions = parse_uco_bank_statement(text, is_file=False)
#         elif bank_format == 'phonepe':
#             parsed_transactions = parse_phonepe_statement(text)
#         else:
#             # Try both parsers
#             parsed_transactions = parse_uco_bank_statement(text, is_file=False)
#             if not parsed_transactions:
#                 parsed_transactions = parse_phonepe_statement(text)
        
#         if not parsed_transactions:
#             return Response({
#                 'success': False,
#                 'error': f'Could not parse {bank_format} format. No transactions found.',
#                 'transactions': [],
#                 'count': 0
#             }, status=200)
        
#         # Create transactions with AI categorization
#         created_transactions = []
        
#         for trans in parsed_transactions:
#             try:
#                 # ✅ FIX: Use .date() to avoid naive datetime timezone warning
#                 # Django's DateField expects a date object, not datetime
#                 try:
#                     if '-' in trans['date']:  # UCO format: 04-Jan-2026
#                         transaction_date = datetime.strptime(trans['date'], '%d-%b-%Y').date()
#                     else:  # PhonePe format: Jan 5, 2026
#                         transaction_date = datetime.strptime(trans['date'], '%b %d, %Y').date()
#                 except:
#                     from django.utils import timezone
#                     transaction_date = timezone.now().date()
                
#                 # AI categorization
#                 detected_category_name, confidence = detect_category_with_gemini(
#                     trans['description'],
#                     trans['merchant'],
#                     trans['amount']
#                 )
                
#                 category, _ = Category.objects.get_or_create(
#                     name=detected_category_name,
#                     defaults={'icon': '📁', 'color': '#3b82f6'}
#                 )
                
#                 transaction = Transaction.objects.create(
#                     user=request.user,
#                     description=trans['description'][:200],
#                     amount=trans['amount'],
#                     transaction_type=trans['type'],
#                     transaction_date=transaction_date,
#                     merchant=trans['merchant'][:50],
#                     category=category,
#                     predicted_category=category,
#                     prediction_confidence=confidence
#                 )
                
#                 created_transactions.append({
#                     'id': transaction.id,
#                     'description': transaction.description,
#                     'amount': str(transaction.amount),
#                     'transaction_type': transaction.transaction_type,
#                     'transaction_date': str(transaction.transaction_date),
#                     'merchant': transaction.merchant,
#                     'category': detected_category_name,
#                     'confidence': round(confidence * 100, 1)
#                 })
                
#             except Exception as e:
#                 print(f"⚠️ Error creating transaction: {str(e)}")
#                 continue
        
#         print(f"🎉 Extracted {len(created_transactions)} transactions from {bank_format}")
        
#         return Response({
#             'success': True,
#             'transactions': created_transactions,
#             'count': len(created_transactions),
#             'bank_format': bank_format,
#             'message': f'Successfully extracted {len(created_transactions)} transactions'
#         }, status=status.HTTP_201_CREATED)
        
#     except Exception as e:
#         print(f"❌ Failed: {str(e)}")
#         import traceback
#         traceback.print_exc()
#         return Response({'error': str(e), 'success': False}, status=500)



# # # backend/transactions/views.py - FIXED UNIVERSAL PDF PARSER
# # from rest_framework import viewsets, status
# # from rest_framework.decorators import api_view, permission_classes
# # from rest_framework.response import Response
# # from rest_framework.permissions import IsAuthenticated
# # from django_filters.rest_framework import DjangoFilterBackend
# # from rest_framework import filters
# # import re
# # from datetime import datetime
# # import os
# # import json

# # from .models import Transaction, BankAccount, Category
# # from .serializers import TransactionSerializer, BankAccountSerializer, CategorySerializer

# # # Gemini AI Integration
# # try:
# #     from google import genai
# #     GEMINI_AVAILABLE = True
# # except ImportError:
# #     GEMINI_AVAILABLE = False
# #     print("⚠️ google-genai not installed. Install with: pip install google-genai")

# # class TransactionViewSet(viewsets.ModelViewSet):
# #     serializer_class = TransactionSerializer
# #     permission_classes = [IsAuthenticated]
# #     filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
# #     filterset_fields = ['transaction_type', 'category', 'bank_account']
# #     search_fields = ['description', 'merchant']
# #     ordering_fields = ['transaction_date', 'amount']
# #     ordering = ['-transaction_date']
    
# #     def get_queryset(self):
# #         return Transaction.objects.filter(user=self.request.user)
    
# #     def perform_create(self, serializer):
# #         serializer.save(user=self.request.user)

# # class BankAccountViewSet(viewsets.ModelViewSet):
# #     serializer_class = BankAccountSerializer
# #     permission_classes = [IsAuthenticated]
    
# #     def get_queryset(self):
# #         return BankAccount.objects.filter(user=self.request.user)
    
# #     def perform_create(self, serializer):
# #         serializer.save(user=self.request.user)

# # class CategoryViewSet(viewsets.ReadOnlyModelViewSet):
# #     queryset = Category.objects.all()
# #     serializer_class = CategorySerializer
# #     permission_classes = [IsAuthenticated]


# # def clean_description(description):
# #     """Clean transaction description"""
# #     cleaned = description
# #     cleaned = re.sub(r'\s*(DEBIT|CREDIT)\s*₹[\d,]+', '', cleaned).strip()
# #     cleaned = re.sub(r'₹[\d,]+', '', cleaned).strip()
# #     cleaned = re.sub(r'\s*(DEBIT|CREDIT)\s*$', '', cleaned).strip()
# #     cleaned = re.sub(r'(Transaction ID|UTR No\.|Paid by|Credited to).*', '', cleaned).strip()
# #     cleaned = ' '.join(cleaned.split())
# #     return cleaned


# # def detect_bank_format(text):
# #     """Detect bank format - checks headers first"""
# #     text_lower = text.lower()
    
# #     if 'uco bank' in text_lower or 'ucba' in text_lower or 'ucobank' in text_lower:
# #         print("🏦 Detected: UCO Bank (from header)")
# #         return 'uco_bank'
    
# #     if 'state bank of india' in text_lower or 'sbi' in text_lower[:500]:
# #         print("🏦 Detected: SBI (from header)")
# #         return 'sbi'
    
# #     if 'hdfc bank' in text_lower[:500]:
# #         print("🏦 Detected: HDFC (from header)")
# #         return 'hdfc'
    
# #     if 'icici bank' in text_lower[:500]:
# #         print("🏦 Detected: ICICI (from header)")
# #         return 'icici'
    
# #     if 'paid to' in text_lower and 'received from' in text_lower:
# #         print("🏦 Detected: PhonePe (from transaction format)")
# #         return 'phonepe'
    
# #     print("⚠️ Unknown bank format")
# #     return 'unknown'


# # # ============================================================
# # # FIX 1: Use pdfplumber table extraction for UCO Bank
# # # ============================================================
# # def parse_uco_bank_with_tables(pdf_file):
# #     """
# #     Parse UCO Bank using pdfplumber TABLE extraction.
    
# #     UCO Bank statement has columns: Date | Particulars | Withdrawals | Deposits | Balance
    
# #     Text extraction merges columns and loses withdrawal vs deposit info.
# #     Table extraction preserves each column separately — THIS IS THE FIX.
# #     """
# #     transactions = []
    
# #     try:
# #         import pdfplumber
# #     except ImportError:
# #         print("⚠️ pdfplumber not available")
# #         return transactions
    
# #     print("📋 Parsing UCO Bank using TABLE extraction (fixed method)")
    
# #     with pdfplumber.open(pdf_file) as pdf:
# #         for page_num, page in enumerate(pdf.pages):
            
# #             # Extract tables from this page
# #             tables = page.extract_tables()
            
# #             if not tables:
# #                 print(f"  Page {page_num+1}: No tables found, skipping")
# #                 continue
            
# #             for table in tables:
# #                 for row in table:
# #                     if not row:
# #                         continue
                    
# #                     # Clean each cell
# #                     row = [str(cell).strip() if cell else '' for cell in row]
                    
# #                     # Skip header rows
# #                     if any(h in row[0].lower() for h in ['date', 'particulars', 'opening']):
# #                         continue
                    
# #                     # UCO Bank row format: [Date, Particulars, Withdrawals, Deposits, Balance]
# #                     # But pdfplumber may return varying column counts
# #                     # We look for a row that starts with a date pattern
                    
# #                     date_match = re.match(r'\d{2}-[A-Z][a-z]{2}-\d{4}', row[0])
# #                     if not date_match:
# #                         continue
                    
# #                     transaction_date_str = row[0]
                    
# #                     # ============================================================
# #                     # FIX 2: Correctly identify Withdrawals vs Deposits columns
# #                     # UCO Bank: col[2]=Withdrawals, col[3]=Deposits, col[4]=Balance
# #                     # ============================================================
                    
# #                     withdrawal_amount = None
# #                     deposit_amount = None
# #                     description = ''
                    
# #                     if len(row) >= 5:
# #                         # Standard 5-column format
# #                         description = row[1].strip()
# #                         withdrawal_str = row[2].strip()
# #                         deposit_str = row[3].strip()
                        
# #                         # Parse withdrawal (expense)
# #                         if withdrawal_str and withdrawal_str not in ['-', '', 'None', 'nan']:
# #                             try:
# #                                 withdrawal_amount = float(withdrawal_str.replace(',', ''))
# #                             except:
# #                                 pass
                        
# #                         # Parse deposit (income)
# #                         if deposit_str and deposit_str not in ['-', '', 'None', 'nan']:
# #                             try:
# #                                 deposit_amount = float(deposit_str.replace(',', ''))
# #                             except:
# #                                 pass
                    
# #                     elif len(row) == 4:
# #                         # Sometimes merges to 4 cols: Date | Particulars | Amount | Balance
# #                         # Can't tell withdrawal vs deposit — skip or mark as unknown
# #                         description = row[1].strip()
# #                         print(f"  ⚠️ 4-col row on {transaction_date_str}, can't determine direction: {row}")
# #                         continue
                    
# #                     else:
# #                         print(f"  ⚠️ Unexpected row format ({len(row)} cols): {row}")
# #                         continue
                    
# #                     # Clean description
# #                     clean_desc = extract_uco_description(description)
                    
# #                     # Create transaction for withdrawal
# #                     if withdrawal_amount and withdrawal_amount >= 1:
# #                         transactions.append({
# #                             'date': transaction_date_str,
# #                             'description': clean_desc,
# #                             'amount': withdrawal_amount,
# #                             'type': 'expense',          # ✅ Withdrawal = expense
# #                             'merchant': clean_desc.split()[0] if clean_desc.split() else 'Unknown'
# #                         })
# #                         print(f"  ✅ EXPENSE: {transaction_date_str} | {clean_desc[:25]:25} | ₹{withdrawal_amount:.2f}")
                    
# #                     # Create transaction for deposit
# #                     if deposit_amount and deposit_amount >= 1:
# #                         transactions.append({
# #                             'date': transaction_date_str,
# #                             'description': clean_desc,
# #                             'amount': deposit_amount,
# #                             'type': 'income',           # ✅ Deposit = income
# #                             'merchant': clean_desc.split()[0] if clean_desc.split() else 'Unknown'
# #                         })
# #                         print(f"  ✅ INCOME:  {transaction_date_str} | {clean_desc[:25]:25} | ₹{deposit_amount:.2f}")
    
# #     print(f"📊 UCO Bank table extraction: {len(transactions)} transactions")
# #     return transactions


# # def extract_uco_description(raw_description):
# #     """
# #     Clean UCO Bank UPI description strings.
# #     Input:  'MPAYUPITRTR654765337668DHARMASURYAPICICXXX1'
# #     Output: 'DHARMASURYA'
# #     """
# #     if not raw_description:
# #         return 'Unknown'
    
# #     # Special cases first
# #     if 'UPIRRC' in raw_description:
# #         return 'Refund'
# #     if 'ONE97' in raw_description or 'PAYTM' in raw_description.upper():
# #         return 'Paytm'
# #     if 'SWIGGY' in raw_description.upper():
# #         return 'Swiggy'
# #     if 'ZOMATO' in raw_description.upper():
# #         return 'Zomato'
# #     if 'BSNL' in raw_description.upper():
# #         return 'BSNL Bill'
# #     if 'DMRC' in raw_description.upper():
# #         return 'Delhi Metro'
# #     if 'MOSAIC' in raw_description.upper() or 'WELLNESS' in raw_description.upper():
# #         return 'MosaicWellness'
# #     if 'BBNOW' in raw_description.upper():
# #         return 'BigBasket'
# #     if 'APEPDCL' in raw_description.upper():
# #         return 'Electricity Bill'
# #     if 'NOIDA' in raw_description.upper() or 'METRO' in raw_description.upper():
# #         return 'Metro'
# #     if 'ETERNAL' in raw_description.upper() or 'AIRP' in raw_description.upper():
# #         return 'Airport/Travel'
    
# #     # Extract merchant from UPI string: MPAYUPITRTR<digits><MERCHANTNAME><BANKCODE>
# #     merchant_match = re.search(r'MPAYUPITRTR\d+([A-Za-z]+)', raw_description)
# #     if merchant_match:
# #         merchant = merchant_match.group(1)
# #         # Remove trailing bank codes
# #         merchant = re.sub(
# #             r'(PICIC|SBIN|YESBXXX|PUNB|HDFC|ICIC|INDB|KKBK|PPIW|UTIB|AIRP|BARB|NSPB|UCBA|CBAX).*',
# #             '', merchant
# #         ).strip()
# #         if merchant:
# #             return merchant
    
# #     # Fallback: first 50 chars
# #     return raw_description[:50].strip()


# # # ============================================================
# # # FIX 3: Fallback text parser (if table extraction fails)
# # # Uses balance change to determine withdrawal vs deposit
# # # ============================================================
# # def parse_uco_bank_text_fallback(text):
# #     """
# #     Fallback text parser for UCO Bank.
# #     Uses balance INCREASE = deposit (income), DECREASE = withdrawal (expense).
# #     This works regardless of column order issues.
# #     """
# #     transactions = []
# #     lines = text.split('\n')
    
# #     print(f"📋 UCO Bank text fallback parser ({len(lines)} lines)")
    
# #     # Regex: date + ONE amount + balance
# #     # e.g. "04-Jan-2026 1800 2300.58"
# #     # We determine type by comparing with previous balance
    
# #     prev_balance = None
    
# #     for i, line in enumerate(lines):
# #         line = line.strip()
        
# #         # Match: date, single amount, balance
# #         m = re.match(
# #             r'^(\d{2}-[A-Z][a-z]{2}-\d{4})\s+([\d,]+(?:\.\d{1,2})?)\s+([\d,]+(?:\.\d{1,2})?)$',
# #             line
# #         )
        
# #         if m:
# #             date_str = m.group(1)
# #             amount = float(m.group(2).replace(',', ''))
# #             balance = float(m.group(3).replace(',', ''))
            
# #             # Get description from previous line
# #             description_raw = lines[i-1].strip() if i > 0 else ''
# #             description = extract_uco_description(description_raw)
            
# #             # ✅ FIX: Use balance change to determine type
# #             # If balance went UP → deposit (income)
# #             # If balance went DOWN → withdrawal (expense)
# #             if prev_balance is not None:
# #                 balance_change = balance - prev_balance
# #                 if balance_change > 0:
# #                     transaction_type = 'income'
# #                 else:
# #                     transaction_type = 'expense'
# #             else:
# #                 # First transaction — use amount vs balance heuristic
# #                 # If amount == balance - opening_balance (approx), it's a deposit
# #                 transaction_type = 'income' if amount < balance else 'expense'
            
# #             prev_balance = balance
            
# #             if amount >= 1:
# #                 transactions.append({
# #                     'date': date_str,
# #                     'description': description,
# #                     'amount': amount,
# #                     'type': transaction_type,
# #                     'merchant': description.split()[0] if description.split() else 'Unknown'
# #                 })
# #                 print(f"  ✅ {transaction_type.upper():7}: {date_str} | {description[:25]:25} | ₹{amount:.2f} | bal:{balance:.2f}")
    
# #     print(f"📊 UCO Bank text fallback: {len(transactions)} transactions")
# #     return transactions


# # def parse_uco_bank_statement(pdf_file_or_text, is_file=False):
# #     """
# #     Main UCO Bank parser.
# #     Tries table extraction first (most accurate), falls back to text with balance-diff logic.
    
# #     Args:
# #         pdf_file_or_text: file object if is_file=True, else text string
# #         is_file: True if passing file object, False if passing text
# #     """
# #     if is_file:
# #         # Try table-based extraction first
# #         transactions = parse_uco_bank_with_tables(pdf_file_or_text)
# #         if transactions:
# #             return transactions
        
# #         # Table extraction failed, extract text and use fallback
# #         print("⚠️ Table extraction returned 0 results, trying text fallback...")
# #         import pdfplumber
# #         pdf_file_or_text.seek(0)  # Reset file pointer
# #         with pdfplumber.open(pdf_file_or_text) as pdf:
# #             text = ''
# #             for page in pdf.pages:
# #                 t = page.extract_text()
# #                 if t:
# #                     text += t + '\n'
# #         return parse_uco_bank_text_fallback(text)
# #     else:
# #         # Text was already extracted, use fallback
# #         return parse_uco_bank_text_fallback(pdf_file_or_text)


# # def parse_phonepe_statement(text):
# #     """Parse PhonePe statement format"""
# #     transactions = []
# #     lines = text.split('\n')
    
# #     print(f"📋 Parsing PhonePe format")
    
# #     current_date = None
# #     current_description = None
# #     current_type = None
# #     current_amount = None
    
# #     for line in lines:
# #         line = line.strip()
# #         if not line:
# #             continue
        
# #         date_match = re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),\s+(\d{4})', line)
# #         type_match = re.search(r'\b(DEBIT|CREDIT)\b', line)
# #         amount_match = re.search(r'₹([\d,]+)', line)
# #         paid_to = re.search(r'Paid to (.+)', line)
# #         received_from = re.search(r'Received from (.+)', line)
# #         payment_to = re.search(r'Payment to (.+)', line)
        
# #         if date_match:
# #             current_date = date_match.group(0)
        
# #         if type_match:
# #             current_type = type_match.group(1)
        
# #         if amount_match:
# #             current_amount = amount_match.group(1)
        
# #         if paid_to or received_from or payment_to:
# #             if paid_to:
# #                 current_description = paid_to.group(1).strip()
# #             elif received_from:
# #                 current_description = received_from.group(1).strip()
# #             elif payment_to:
# #                 current_description = payment_to.group(1).strip()
            
# #             current_description = clean_description(current_description)
        
# #         if all([current_date, current_type, current_amount, current_description]):
# #             try:
# #                 amount = float(current_amount.replace(',', ''))
                
# #                 if amount >= 1:
# #                     # ✅ PhonePe is simple: DEBIT=expense, CREDIT=income
# #                     transaction_type = 'expense' if current_type == 'DEBIT' else 'income'
                    
# #                     transactions.append({
# #                         'date': current_date,
# #                         'description': current_description,
# #                         'amount': amount,
# #                         'type': transaction_type,
# #                         'merchant': current_description.split()[0] if current_description else 'Unknown'
# #                     })
                    
# #                     print(f"✅ PhonePe: {current_date} - {current_description[:30]} - ₹{amount} ({transaction_type})")
                
# #                 current_date = current_description = current_type = current_amount = None
                
# #             except:
# #                 current_date = current_description = current_type = current_amount = None
    
# #     return transactions


# # def detect_category_with_gemini(description, merchant, amount=None):
# #     """AI-powered category detection using Gemini"""
# #     if not GEMINI_AVAILABLE:
# #         return detect_category_fallback(description, merchant)
    
# #     api_key = os.getenv('GEMINI_API_KEY')
# #     if not api_key:
# #         return detect_category_fallback(description, merchant)
    
# #     try:
# #         client = genai.Client(api_key=api_key)
        
# #         prompt = f"""Categorize this Indian bank transaction into ONE category:

# # Categories:
# # 1. Food & Dining
# # 2. Healthcare  
# # 3. Online Shopping
# # 4. Education
# # 5. Shopping
# # 6. Transportation
# # 7. Entertainment
# # 8. Bills & Utilities
# # 9. Personal Care
# # 10. Personal Transfer
# # 11. Miscellaneous

# # Transaction:
# # - Description: {description}
# # - Merchant: {merchant}
# # - Amount: ₹{amount}

# # Rules:
# # - Names like DHARMASURYA, Bailapudi, Hemant, Saatvik → Personal Transfer
# # - PHONEPE, PhonePe → Online Shopping
# # - DMRC, Metro → Transportation
# # - Swiggy, Zomato → Food & Dining
# # - BSNL, recharge → Bills & Utilities

# # Response format: {{"category": "Name", "confidence": 0.95}}"""
        
# #         response = client.models.generate_content(model='gemini-2.0-flash', contents=prompt)
# #         result_text = response.text.strip()
        
# #         if '```json' in result_text:
# #             result_text = result_text.split('```json')[1].split('```')[0].strip()
# #         elif '```' in result_text:
# #             result_text = result_text.split('```')[1].split('```')[0].strip()
        
# #         result = json.loads(result_text)
# #         return result.get('category', 'Miscellaneous'), float(result.get('confidence', 0.7))
        
# #     except Exception as e:
# #         print(f"⚠️ Gemini failed: {e}")
# #         return detect_category_fallback(description, merchant)


# # def detect_category_fallback(description, merchant):
# #     """Keyword-based fallback"""
# #     text = (description + ' ' + merchant).lower()
    
# #     keywords = {
# #         'Food & Dining': ['food', 'swiggy', 'zomato', 'restaurant', 'juice'],
# #         'Transportation': ['metro', 'dmrc', 'uber', 'ola', 'taxi', 'airport'],
# #         'Bills & Utilities': ['bsnl', 'recharge', 'electricity', 'internet', 'apepdcl'],
# #         'Online Shopping': ['amazon', 'flipkart', 'bigbasket', 'bbnow'],
# #         'Entertainment': ['netflix', 'spotify', 'movie'],
# #         'Healthcare': ['medical', 'hospital', 'pharmacy', 'mosaic', 'wellness'],
# #         'Education': ['book', 'stationery', 'tuition'],
# #     }
    
# #     for category, words in keywords.items():
# #         if any(word in text for word in words):
# #             return category, 0.85
    
# #     if len(description.split()) <= 3 and not any(x in text for x in ['pvt', 'ltd', 'shop']):
# #         return 'Personal Transfer', 0.75
    
# #     return 'Miscellaneous', 0.40


# # @api_view(['POST'])
# # @permission_classes([IsAuthenticated])
# # def extract_pdf_transactions(request):
# #     """
# #     UNIVERSAL PDF PARSER - Supports all bank formats
# #     """
# #     print("📄 Universal PDF Extraction Started")
    
# #     if 'file' not in request.FILES:
# #         return Response({'error': 'No file uploaded'}, status=400)
    
# #     uploaded_file = request.FILES['file']
# #     print(f"📎 File: {uploaded_file.name}")
    
# #     if not uploaded_file.name.endswith('.pdf'):
# #         return Response({'error': 'File must be a PDF'}, status=400)
    
# #     try:
# #         # Extract text for bank format detection
# #         try:
# #             import pdfplumber
# #             with pdfplumber.open(uploaded_file) as pdf:
# #                 text = ''
# #                 for page in pdf.pages:
# #                     page_text = page.extract_text()
# #                     if page_text:
# #                         text += page_text + '\n'
# #             uploaded_file.seek(0)  # Reset for table extraction later
# #         except ImportError:
# #             import PyPDF2
# #             pdf_reader = PyPDF2.PdfReader(uploaded_file)
# #             text = ''
# #             for page in pdf_reader.pages:
# #                 text += page.extract_text() + '\n'
        
# #         print(f"📝 Extracted {len(text)} characters")
        
# #         if not text.strip():
# #             return Response({'error': 'Could not extract text from PDF'}, status=400)
        
# #         # Detect bank format
# #         bank_format = detect_bank_format(text)
# #         print(f"🏦 Detected format: {bank_format}")
        
# #         # ============================================================
# #         # KEY CHANGE: Pass the file object for UCO Bank so we can use
# #         # table extraction (much more accurate than text extraction)
# #         # ============================================================
# #         if bank_format == 'uco_bank':
# #             try:
# #                 parsed_transactions = parse_uco_bank_statement(uploaded_file, is_file=True)
# #             except Exception as e:
# #                 print(f"⚠️ Table extraction error: {e}, falling back to text")
# #                 parsed_transactions = parse_uco_bank_statement(text, is_file=False)
# #         elif bank_format == 'phonepe':
# #             parsed_transactions = parse_phonepe_statement(text)
# #         else:
# #             # Try both parsers
# #             parsed_transactions = parse_uco_bank_statement(text, is_file=False)
# #             if not parsed_transactions:
# #                 parsed_transactions = parse_phonepe_statement(text)
        
# #         if not parsed_transactions:
# #             return Response({
# #                 'success': False,
# #                 'error': f'Could not parse {bank_format} format. No transactions found.',
# #                 'transactions': [],
# #                 'count': 0
# #             }, status=200)
        
# #         # Create transactions with AI categorization
# #         created_transactions = []
        
# #         for trans in parsed_transactions:
# #             try:
# #                 # Parse date
# #                 try:
# #                     if '-' in trans['date']:  # UCO format: 04-Jan-2026
# #                         transaction_date = datetime.strptime(trans['date'], '%d-%b-%Y').date()
# #                     else:  # PhonePe format: Jan 5, 2026
# #                         transaction_date = datetime.strptime(trans['date'], '%b %d, %Y').date()
# #                 except:
# #                     transaction_date = datetime.now().date()
                
# #                 # AI categorization
# #                 detected_category_name, confidence = detect_category_with_gemini(
# #                     trans['description'],
# #                     trans['merchant'],
# #                     trans['amount']
# #                 )
                
# #                 category, _ = Category.objects.get_or_create(
# #                     name=detected_category_name,
# #                     defaults={'icon': '📁', 'color': '#3b82f6'}
# #                 )
                
# #                 transaction = Transaction.objects.create(
# #                     user=request.user,
# #                     description=trans['description'][:200],
# #                     amount=trans['amount'],
# #                     transaction_type=trans['type'],
# #                     transaction_date=transaction_date,
# #                     merchant=trans['merchant'][:50],
# #                     category=category,
# #                     predicted_category=category,
# #                     prediction_confidence=confidence
# #                 )
                
# #                 created_transactions.append({
# #                     'id': transaction.id,
# #                     'description': transaction.description,
# #                     'amount': str(transaction.amount),
# #                     'transaction_type': transaction.transaction_type,
# #                     'transaction_date': str(transaction.transaction_date),
# #                     'merchant': transaction.merchant,
# #                     'category': detected_category_name,
# #                     'confidence': round(confidence * 100, 1)
# #                 })
                
# #             except Exception as e:
# #                 print(f"⚠️ Error creating transaction: {str(e)}")
# #                 continue
        
# #         print(f"🎉 Extracted {len(created_transactions)} transactions from {bank_format}")
        
# #         return Response({
# #             'success': True,
# #             'transactions': created_transactions,
# #             'count': len(created_transactions),
# #             'bank_format': bank_format,
# #             'message': f'Successfully extracted {len(created_transactions)} transactions'
# #         }, status=status.HTTP_201_CREATED)
        
# #     except Exception as e:
# #         print(f"❌ Failed: {str(e)}")
# #         import traceback
# #         traceback.print_exc()
# #         return Response({'error': str(e), 'success': False}, status=500)


















# #     # from rest_framework import viewsets, status
# #     # from rest_framework.decorators import api_view, permission_classes
# #     # from rest_framework.response import Response
# #     # from rest_framework.permissions import IsAuthenticated
# #     # from django_filters.rest_framework import DjangoFilterBackend
# #     # from rest_framework import filters
# #     # import re
# #     # from datetime import datetime
# #     # import os
# #     # import json

# #     # from .models import Transaction, BankAccount, Category
# #     # from .serializers import TransactionSerializer, BankAccountSerializer, CategorySerializer

# #     # # Gemini AI Integration
# #     # try:
# #     #     import google.generativeai as genai
# #     #     GEMINI_AVAILABLE = True
# #     # except ImportError:             
# #     #     GEMINI_AVAILABLE = False
# #     #     print("⚠️ google-generativeai not installed. Install with: pip install google-generativeai")

# #     # class TransactionViewSet(viewsets.ModelViewSet):
# #     #     serializer_class = TransactionSerializer
# #     #     permission_classes = [IsAuthenticated]
# #     #     filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
# #     #     filterset_fields = ['transaction_type', 'category', 'bank_account']
# #     #     search_fields = ['description', 'merchant']
# #     #     ordering_fields = ['transaction_date', 'amount']
# #     #     ordering = ['-transaction_date']
        
# #     #     def get_queryset(self):
# #     #         return Transaction.objects.filter(user=self.request.user)
        
# #     #     def perform_create(self, serializer):
# #     #         serializer.save(user=self.request.user)

# #     # class BankAccountViewSet(viewsets.ModelViewSet):
# #     #     serializer_class = BankAccountSerializer
# #     #     permission_classes = [IsAuthenticated]
        
# #     #     def get_queryset(self):
# #     #         return BankAccount.objects.filter(user=self.request.user)
        
# #     #     def perform_create(self, serializer):
# #     #         serializer.save(user=self.request.user)

# #     # class CategoryViewSet(viewsets.ReadOnlyModelViewSet):
# #     #     queryset = Category.objects.all()
# #     #     serializer_class = CategorySerializer
# #     #     permission_classes = [IsAuthenticated]


# #     # def detect_category_with_gemini(description, merchant, amount=None):
# #     #     """
# #     #     AI-powered category detection using Google Gemini (FREE!)
# #     #     Falls back to keyword-based if Gemini unavailable
# #     #     """
        
# #     #     # Check if Gemini is available and configured
# #     #     if not GEMINI_AVAILABLE:
# #     #         print("⚠️ Gemini not available, using keyword fallback")
# #     #         return detect_category_fallback(description, merchant)
        
# #     #     api_key = os.getenv('GEMINI_API_KEY')
# #     #     if not api_key:
# #     #         print("⚠️ GEMINI_API_KEY not set, using keyword fallback")
# #     #         return detect_category_fallback(description, merchant)
        
# #     #     try:
# #     #         # Configure and initialize Gemini
# #     #         genai.configure(api_key=api_key)
# #     #         model = genai.GenerativeModel('gemini-1.5-flash')
            
# #     #         # Prepare prompt
# #     #         prompt = f"""You are a financial transaction categorizer for Indian users. Analyze this transaction and assign it to ONE category.

# #     # Categories:
# #     # 1. Food & Dining - Restaurants, groceries, cafes, food delivery, snacks, juice, beverages
# #     # 2. Healthcare - Medical bills, pharmacy, hospital, doctor visits
# #     # 3. Online Shopping - Amazon, Flipkart, e-commerce purchases, deliveries
# #     # 4. Education - Books, stationery, tuition, courses, school/college fees
# #     # 5. Shopping - Clothing, electronics, general shopping, accessories, mobile
# #     # 6. Transportation - Fuel, taxi, public transport, parking, Uber, Ola
# #     # 7. Entertainment - Movies, subscriptions (Netflix, Spotify), games, concerts, cinema
# #     # 8. Bills & Utilities - Electricity, water, internet, mobile recharge
# #     # 9. Personal Care - Salon, spa, grooming products
# #     # 10. Personal Transfer - Money sent to friends, family, or personal contacts (names like Amma, Vishal, etc.)
# #     # 11. Miscellaneous - Anything that doesn't fit above

# #     # Transaction:
# #     # - Description: {description}
# #     # - Merchant: {merchant}
# #     # {f'- Amount: ₹{amount}' if amount else ''}

# #     # Rules:
# #     # - Personal names without business context → Personal Transfer
# #     # - "movie" → Entertainment
# #     # - "juice" → Food & Dining
# #     # - Business names → Appropriate category
# #     # - Consider Indian context (PhonePe, UPI, Indian merchants)

# #     # Respond ONLY with JSON (no markdown, no explanation):
# #     # {{"category": "Category Name", "confidence": 0.95}}"""

# #     #         # Call Gemini API
# #     #         response = model.generate_content(prompt)
# #     #         result_text = response.text.strip()
            
# #     #         # Parse JSON response
# #     #         if '```json' in result_text:
# #     #             result_text = result_text.split('```json')[1].split('```')[0].strip()
# #     #         elif '```' in result_text:
# #     #             result_text = result_text.split('```')[1].split('```')[0].strip()
            
# #     #         result = json.loads(result_text)
            
# #     #         category = result.get('category', 'Miscellaneous')
# #     #         confidence = float(result.get('confidence', 0.7))
            
# #     #         print(f"🤖 Gemini AI: {description[:30]} → {category} ({confidence*100:.0f}%)")
            
# #     #         return category, confidence
            
# #     #     except Exception as e:
# #     #         print(f"⚠️ Gemini AI failed: {str(e)}, using fallback")
# #     #         return detect_category_fallback(description, merchant)


# #     # def detect_category_fallback(description, merchant):
# #     #     """
# #     #     Fallback keyword-based categorization
# #     #     Used when Gemini API fails or is unavailable
# #     #     """
# #     #     text = (description + ' ' + merchant).lower()
# #     #     original_text = description
        
# #     #     # Enhanced keywords with scoring
# #     #     category_keywords = {
# #     #         'Entertainment': {
# #     #             'keywords': ['movie', 'cinema', 'netflix', 'prime', 'spotify', 'hotstar', 
# #     #                         'zee5', 'saavn', 'film', 'theater', 'theatre', 'show'],
# #     #             'score': 3
# #     #         },
# #     #         'Food & Dining': {
# #     #             'keywords': ['food', 'juice', 'restaurant', 'cafe', 'coffee', 'tiffin',
# #     #                         'meal', 'dinner', 'lunch', 'breakfast', 'snack', 'drink',
# #     #                         'cool drink', 'sweet', 'biryani', 'pizza'],
# #     #             'score': 3
# #     #         },
# #     #         'Healthcare': {
# #     #             'keywords': ['medical', 'medicals', 'hospital', 'clinic', 'pharmacy',
# #     #                         'medicine', 'doctor', 'health', 'apollo'],
# #     #             'score': 3
# #     #         },
# #     #         'Online Shopping': {
# #     #             'keywords': ['amazon', 'flipkart', 'ekart', 'meesho', 'myntra', 'ajio',
# #     #                         'delivery', 'courier', 'online', 'ecommerce'],
# #     #             'score': 3
# #     #         },
# #     #         'Education': {
# #     #             'keywords': ['book', 'books', 'stationery', 'stationary', 'xerox',
# #     #                         'library', 'school', 'college', 'anits', 'university',
# #     #                         'tuition', 'education', 'cse'],
# #     #             'score': 3
# #     #         },
# #     #         'Shopping': {
# #     #             'keywords': ['shop', 'store', 'mall', 'mart', 'accessories', 'mobile',
# #     #                         'electronics', 'electrical', 'fancy', 'general'],
# #     #             'score': 2
# #     #         },
# #     #         'Transportation': {
# #     #             'keywords': ['uber', 'ola', 'rapido', 'taxi', 'cab', 'fuel', 'petrol',
# #     #                         'auto', 'bus', 'train'],
# #     #             'score': 2
# #     #         },
# #     #         'Bills & Utilities': {
# #     #             'keywords': ['electricity', 'water bill', 'internet', 'broadband', 
# #     #                         'recharge', 'bill', 'utility'],
# #     #             'score': 3
# #     #         }
# #     #     }
        
# #     #     best_match = None
# #     #     highest_score = 0
        
# #     #     for category, data in category_keywords.items():
# #     #         score = 0
# #     #         for keyword in data['keywords']:
# #     #             if keyword in text:
# #     #                 score += data['score']
            
# #     #         if score > highest_score:
# #     #             highest_score = score
# #     #             best_match = category
        
# #     #     if best_match and highest_score >= 2:
# #     #         return best_match, 0.85
        
# #     #     # Personal transfer detection
# #     #     words = original_text.split()
# #     #     if len(words) <= 3:
# #     #         business_indicators = ['shop', 'store', 'mart', 'pvt', 'ltd', 'services',
# #     #                               'center', 'centre', 'hospital', 'clinic', 'pharmacy']
# #     #         has_business = any(biz in text for biz in business_indicators)
            
# #     #         if not has_business:
# #     #             # Check for personal name patterns
# #     #             personal_patterns = ['amma', 'akka', 'anna', '******', '@', 
# #     #                                'kumar', 'anil', 'chitti', 'yerra', 'vamsi',
# #     #                                'kalyan', 'preethi', 'vishal']
# #     #             has_personal = any(pattern in text for pattern in personal_patterns)
                
# #     #             if has_personal or len(words) <= 2:
# #     #                 return 'Personal Transfer', 0.75
        
# #     #     return 'Miscellaneous', 0.40


# #     # def detect_category(description, merchant):
# #     #     """
# #     #     Main category detection function
# #     #     Uses Gemini AI if available, otherwise falls back to keywords
# #     #     """
# #     #     return detect_category_with_gemini(description, merchant)


# #     # @api_view(['POST'])
# #     # @permission_classes([IsAuthenticated])
# #     # def extract_pdf_transactions(request):
# #     #     """
# #     #     Extract transactions from PhonePe PDF statement with AI category detection
# #     #     """
# #     #     print("📄 PhonePe PDF Extraction Started")
        
# #     #     if 'file' not in request.FILES:
# #     #         print("❌ No file uploaded")
# #     #         return Response({'error': 'No file uploaded'}, status=400)
        
# #     #     uploaded_file = request.FILES['file']
# #     #     print(f"📎 File received: {uploaded_file.name}")
        
# #     #     if not uploaded_file.name.endswith('.pdf'):
# #     #         print("❌ Not a PDF file")
# #     #         return Response({'error': 'File must be a PDF'}, status=400)
        
# #     #     try:
# #     #         # Extract text from PDF
# #     #         try:
# #     #             import pdfplumber
# #     #             print("✅ Using pdfplumber")
# #     #             with pdfplumber.open(uploaded_file) as pdf:
# #     #                 text = ''
# #     #                 for page in pdf.pages:
# #     #                     page_text = page.extract_text()
# #     #                     if page_text:
# #     #                         text += page_text + '\n'
# #     #             print(f"📝 Extracted {len(text)} characters")
# #     #         except ImportError:
# #     #             print("⚠️ pdfplumber not available, trying PyPDF2")
# #     #             import PyPDF2
# #     #             pdf_reader = PyPDF2.PdfReader(uploaded_file)
# #     #             text = ''
# #     #             for page in pdf_reader.pages:
# #     #                 text += page.extract_text() + '\n'
# #     #             print(f"📝 Extracted {len(text)} characters with PyPDF2")
            
# #     #         if not text.strip():
# #     #             print("❌ No text extracted from PDF")
# #     #             return Response({'error': 'Could not extract text from PDF'}, status=400)
            
# #     #         # Parse PhonePe transactions
# #     #         transactions = []
            
# #     #         lines = text.split('\n')
# #     #         print(f"📋 Processing {len(lines)} lines")
            
# #     #         current_date = None
# #     #         current_description = None
# #     #         current_type = None
# #     #         current_amount = None
            
# #     #         extracted_count = 0
            
# #     #         for i, line in enumerate(lines):
# #     #             line = line.strip()
# #     #             if not line:
# #     #                 continue
                
# #     #             # Look for date pattern
# #     #             date_match = re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),\s+(\d{4})', line)
                
# #     #             # Look for transaction type
# #     #             type_match = re.search(r'\b(DEBIT|CREDIT)\b', line)
                
# #     #             # Look for amount with Rupee symbol
# #     #             amount_match = re.search(r'₹([\d,]+)', line)
                
# #     #             # Look for description patterns
# #     #             paid_to = re.search(r'Paid to (.+)', line)
# #     #             received_from = re.search(r'Received from (.+)', line)
# #     #             payment_to = re.search(r'Payment to (.+)', line)
                
# #     #             # Collect transaction details
# #     #             if date_match:
# #     #                 current_date = date_match.group(0)
                
# #     #             if type_match:
# #     #                 current_type = type_match.group(1)
                
# #     #             if amount_match:
# #     #                 current_amount = amount_match.group(1)
                
# #     #             if paid_to or received_from or payment_to:
# #     #                 if paid_to:
# #     #                     current_description = paid_to.group(1).strip()
# #     #                 elif received_from:
# #     #                     current_description = received_from.group(1).strip()
# #     #                 elif payment_to:
# #     #                     current_description = payment_to.group(1).strip()
                    
# #     #                 # Clean up description
# #     #                 # current_description = re.sub(r'Transaction ID.*', '', current_description).strip()
# #     #                 # current_description = re.sub(r'UTR No\..*', '', current_description).strip()
# #     #                 # current_description = re.sub(r'Paid by.*', '', current_description).strip()
# #     #                 # current_description = re.sub(r'Credited to.*', '', current_description).strip()

# #     #                     # Remove Transaction ID, UTR, Paid by, Credited to
# #     #                 current_description = re.sub(r'(Transaction ID|UTR No\.|Paid by|Credited to).*', '', current_description).strip()
# #     #                     # ✅ NEW: Remove "DEBIT ₹80" or "CREDIT ₹500" from description
# #     #                 current_description = re.sub(r'\s*(DEBIT|CREDIT)\s*₹[\d,]+', '', current_description).strip()
# #     #                     # ✅ NEW: Remove any remaining ₹ amounts
# #     #                 current_description = re.sub(r'₹[\d,]+', '', current_description).strip()
# #     #                     # ✅ NEW: Remove trailing DEBIT/CREDIT words
# #     #                 current_description = re.sub(r'\s*(DEBIT|CREDIT)\s*$', '', current_description).strip()
                
# #     #             # When we have all components, create transaction
# #     #             if current_date and current_type and current_amount and current_description:
# #     #                 try:
# #     #                     # Parse amount
# #     #                     amount = float(current_amount.replace(',', ''))
                        
# #     #                     # Skip very small amounts
# #     #                     if amount < 1:
# #     #                         current_date = None
# #     #                         current_description = None
# #     #                         current_type = None
# #     #                         current_amount = None
# #     #                         continue
                        
# #     #                     # Determine transaction type
# #     #                     transaction_type = 'expense' if current_type == 'DEBIT' else 'income'
                        
# #     #                     # Parse date
# #     #                     try:
# #     #                         transaction_date = datetime.strptime(current_date, '%b %d, %Y').date()
# #     #                     except:
# #     #                         transaction_date = datetime.now().date()
                        
# #     #                     # Extract merchant name
# #     #                     merchant = current_description.split()[0] if current_description else ''
# #     #                     merchant = merchant[:50]
                        
# #     #                     # 🤖 GEMINI AI CATEGORY DETECTION (with fallback)
# #     #                     detected_category_name, confidence = detect_category_with_gemini(
# #     #                         current_description, 
# #     #                         merchant,
# #     #                         amount
# #     #                     )
                        
# #     #                     # Get or create category
# #     #                     category, created = Category.objects.get_or_create(
# #     #                         name=detected_category_name,
# #     #                         defaults={'icon': '📁', 'color': '#3b82f6'}
# #     #                     )
                        
# #     #                     # Create transaction with category
# #     #                     transaction = Transaction.objects.create(
# #     #                         user=request.user,
# #     #                         description=current_description[:200],
# #     #                         amount=amount,
# #     #                         transaction_type=transaction_type,
# #     #                         transaction_date=transaction_date,
# #     #                         merchant=merchant,
# #     #                         category=category,
# #     #                         predicted_category=category,
# #     #                         prediction_confidence=confidence
# #     #                     )
                        
# #     #                     transactions.append({
# #     #                         'id': transaction.id,
# #     #                         'description': transaction.description,
# #     #                         'amount': str(transaction.amount),
# #     #                         'transaction_type': transaction.transaction_type,
# #     #                         'transaction_date': str(transaction.transaction_date),
# #     #                         'merchant': transaction.merchant,
# #     #                         'category': detected_category_name,
# #     #                         'confidence': round(confidence * 100, 1)
# #     #                     })
                        
# #     #                     extracted_count += 1
# #     #                     print(f"✅ Transaction {extracted_count}: {current_description[:30]} - ₹{amount} ({transaction_type}) → {detected_category_name} ({confidence*100:.0f}%)")
                        
# #     #                     # Reset for next transaction
# #     #                     current_date = None
# #     #                     current_description = None
# #     #                     current_type = None
# #     #                     current_amount = None
                        
# #     #                 except Exception as e:
# #     #                     print(f"⚠️ Error creating transaction: {str(e)}")
# #     #                     current_date = None
# #     #                     current_description = None
# #     #                     current_type = None
# #     #                     current_amount = None
# #     #                     continue
            
# #     #         print(f"🎉 Successfully extracted {len(transactions)} transactions with AI categories")
            
# #     #         if len(transactions) == 0:
# #     #             return Response({
# #     #                 'success': False,
# #     #                 'transactions': [],
# #     #                 'count': 0,
# #     #                 'error': 'No valid transactions found in PDF'
# #     #             }, status=200)
            
# #     #         return Response({
# #     #             'success': True,
# #     #             'transactions': transactions,
# #     #             'count': len(transactions),
# #     #             'message': f'Successfully extracted {len(transactions)} transactions with AI categorization'
# #     #         }, status=status.HTTP_201_CREATED)
            
# #     #     except Exception as e:
# #     #         print(f"❌ PDF extraction failed: {str(e)}")
# #     #         import traceback
# #     #         traceback.print_exc()
# #     #         return Response({
# #     #             'error': f'Failed to process PDF: {str(e)}',
# #     #             'success': False
# #     #         }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)