"""
API Views
=========
All endpoints use explicit merchant scoping — in production this would come
from JWT auth middleware. For the demo we accept merchant_id as a query param
or in the URL.
"""
import uuid
import logging

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Merchant, Payout, LedgerEntry
from .serializers import (
    MerchantDashboardSerializer, PayoutSerializer,
    PayoutRequestSerializer, LedgerEntrySerializer
)
from .services import (
    PayoutService, InsufficientFundsError,
    InvalidTransitionError, IdempotencyKeyExpiredError
)

logger = logging.getLogger(__name__)


def get_merchant_or_404(merchant_id):
    try:
        return Merchant.objects.get(pk=merchant_id)
    except (Merchant.DoesNotExist, ValueError):
        return None


# ── Merchant endpoints ────────────────────────────────────────────────────────

@api_view(['GET'])
def list_merchants(request):
    """List all merchants (demo — real app would scope to authed user)."""
    merchants = Merchant.objects.all()
    data = []
    for m in merchants:
        available, held = m.get_balance()
        data.append({
            'id': str(m.id),
            'name': m.name,
            'email': m.email,
            'bank_account_id': m.bank_account_id,
            'available_balance_paise': available,
            'held_balance_paise': held,
        })
    return Response(data)


@api_view(['GET'])
def merchant_dashboard(request, merchant_id):
    merchant = get_merchant_or_404(merchant_id)
    if not merchant:
        return Response({'error': 'Merchant not found'}, status=404)

    available, held = merchant.get_balance()

    ledger = LedgerEntry.objects.filter(merchant=merchant).order_by('-created_at')[:50]
    ledger_data = LedgerEntrySerializer(ledger, many=True).data

    payouts = Payout.objects.filter(merchant=merchant).order_by('-created_at')[:50]
    payouts_data = PayoutSerializer(payouts, many=True).data

    return Response({
        'merchant': {
            'id': str(merchant.id),
            'name': merchant.name,
            'email': merchant.email,
            'bank_account_id': merchant.bank_account_id,
        },
        'balance': {
            'available_paise': available,
            'held_paise': held,
            'total_paise': available + held,
        },
        'ledger': ledger_data,
        'payouts': payouts_data,
    })


# ── Payout endpoints ──────────────────────────────────────────────────────────

@api_view(['POST'])
def request_payout(request, merchant_id):
    """
    POST /api/v1/merchants/{id}/payouts
    Header: Idempotency-Key: <uuid>
    Body:   { amount_paise: int, bank_account_id: str }
    """
    merchant = get_merchant_or_404(merchant_id)
    if not merchant:
        return Response({'error': 'Merchant not found'}, status=404)

    # Validate idempotency key
    idempotency_key = request.headers.get('Idempotency-Key', '').strip()
    if not idempotency_key:
        return Response(
            {'error': 'Idempotency-Key header is required'},
            status=status.HTTP_400_BAD_REQUEST
        )
    try:
        uuid.UUID(idempotency_key)
    except ValueError:
        return Response(
            {'error': 'Idempotency-Key must be a valid UUID'},
            status=status.HTTP_400_BAD_REQUEST
        )

    # Validate body
    serializer = PayoutRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    amount_paise = serializer.validated_data['amount_paise']
    bank_account_id = serializer.validated_data['bank_account_id']

    try:
        payout, created = PayoutService.request_payout(
            merchant=merchant,
            amount_paise=amount_paise,
            bank_account_id=bank_account_id,
            idempotency_key=idempotency_key,
        )
    except InsufficientFundsError as e:
        return Response({'error': str(e)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
    except IdempotencyKeyExpiredError as e:
        return Response({'error': str(e)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
    except Exception as e:
        logger.exception(f"Unexpected error creating payout: {e}")
        return Response({'error': 'Internal server error'}, status=500)

    payout_data = PayoutSerializer(payout).data
    resp_status = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return Response(payout_data, status=resp_status)


@api_view(['GET'])
def payout_detail(request, merchant_id, payout_id):
    merchant = get_merchant_or_404(merchant_id)
    if not merchant:
        return Response({'error': 'Merchant not found'}, status=404)
    try:
        payout = Payout.objects.get(pk=payout_id, merchant=merchant)
    except (Payout.DoesNotExist, ValueError):
        return Response({'error': 'Payout not found'}, status=404)

    return Response(PayoutSerializer(payout).data)


@api_view(['GET'])
def list_payouts(request, merchant_id):
    merchant = get_merchant_or_404(merchant_id)
    if not merchant:
        return Response({'error': 'Merchant not found'}, status=404)

    payouts = Payout.objects.filter(merchant=merchant).order_by('-created_at')[:100]
    return Response(PayoutSerializer(payouts, many=True).data)


# ── Debug/Admin endpoints ─────────────────────────────────────────────────────

@api_view(['GET'])
def balance_invariant_check(request, merchant_id):
    """
    Verifies: sum(credits) - sum(debits) == available + held
    Returns the audit result. Used by tests.
    """
    from django.db.models import Sum, Case, When
    from .models import LedgerEntry
    import django.db.models as m

    merchant = get_merchant_or_404(merchant_id)
    if not merchant:
        return Response({'error': 'Merchant not found'}, status=404)

    agg = LedgerEntry.objects.filter(merchant=merchant).aggregate(
        credits=Sum(
            Case(When(entry_type=LedgerEntry.CREDIT, then='amount_paise'),
                 default=0, output_field=m.BigIntegerField())
        ),
        debits=Sum(
            Case(When(entry_type=LedgerEntry.DEBIT, then='amount_paise'),
                 default=0, output_field=m.BigIntegerField())
        ),
    )
    credits = agg['credits'] or 0
    debits = agg['debits'] or 0
    ledger_net = credits - debits

    available, held = merchant.get_balance()
    balance_total = available + held

    invariant_holds = ledger_net == balance_total

    return Response({
        'invariant_holds': invariant_holds,
        'ledger_net_paise': ledger_net,
        'available_paise': available,
        'held_paise': held,
        'balance_total_paise': balance_total,
        'credits_paise': credits,
        'debits_paise': debits,
    })
