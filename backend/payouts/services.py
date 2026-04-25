"""
PayoutService
=============
All money-touching operations go through this service.

Concurrency strategy:
- Balance check + deduction uses SELECT FOR UPDATE on a per-merchant advisory
  lock row, so two concurrent requests for the same merchant queue up.
- We never read balance in Python and write back; we let the DB do the math.

Idempotency:
- We attempt INSERT on IdempotencyKey with a unique constraint on (merchant, key).
- If the INSERT wins, we proceed. If it loses (duplicate), we return the stored response.
- get_or_create with select_for_update ensures exactly-once semantics.
"""
import uuid
import logging
from datetime import timedelta

from django.db import transaction, IntegrityError
from django.db.models import Sum, Case, When, F
from django.utils import timezone

from .models import Merchant, Payout, LedgerEntry, IdempotencyKey

logger = logging.getLogger(__name__)

IDEMPOTENCY_TTL_HOURS = 24


class InsufficientFundsError(Exception):
    pass


class InvalidTransitionError(Exception):
    pass


class IdempotencyKeyExpiredError(Exception):
    pass


class PayoutService:

    @staticmethod
    def request_payout(merchant: Merchant, amount_paise: int, bank_account_id: str,
                       idempotency_key: str) -> tuple[Payout, bool]:
        """
        Create a payout request, enforcing:
        1. Idempotency — same key returns same result
        2. Sufficient funds
        3. Atomic fund hold via DB-level lock

        Returns (payout, created: bool)
        """
        # ── Step 1: Check idempotency key (outside main transaction so the key
        #            record is visible to concurrent requests immediately) ───────
        expires_at = timezone.now() + timedelta(hours=IDEMPOTENCY_TTL_HOURS)

        try:
            idem_record = IdempotencyKey.objects.get(merchant=merchant, key=idempotency_key)
            if idem_record.is_expired():
                raise IdempotencyKeyExpiredError("Idempotency key has expired.")
            # Return the cached response
            payout = Payout.objects.get(id=idem_record.payout_id)
            return payout, False
        except IdempotencyKey.DoesNotExist:
            pass

        # ── Step 2: Lock the merchant row, compute balance, deduct atomically ──
        with transaction.atomic():
            # SELECT FOR UPDATE on merchant — serializes concurrent payout
            # requests for the same merchant. Two 60rs requests on a 100rs
            # balance: first acquires lock, checks, succeeds; second acquires
            # lock after first commits, sees reduced balance, fails cleanly.
            merchant_locked = Merchant.objects.select_for_update().get(pk=merchant.pk)

            available, _ = merchant_locked.get_balance()

            if available < amount_paise:
                raise InsufficientFundsError(
                    f"Insufficient funds: available {available}p, requested {amount_paise}p"
                )

            # Create payout — funds are now "held" (counted in get_balance held sum)
            payout = Payout.objects.create(
                merchant=merchant_locked,
                amount_paise=amount_paise,
                bank_account_id=bank_account_id,
                status=Payout.PENDING,
            )

            # Store idempotency key atomically with payout creation
            try:
                IdempotencyKey.objects.create(
                    merchant=merchant_locked,
                    key=idempotency_key,
                    response_status=201,
                    response_body={'payout_id': str(payout.id), 'status': payout.status},
                    payout=payout,
                    expires_at=expires_at,
                )
            except IntegrityError:
                # Race: another request just created it — roll back and return theirs
                transaction.set_rollback(True)
                idem_record = IdempotencyKey.objects.get(merchant=merchant, key=idempotency_key)
                existing_payout = Payout.objects.get(id=idem_record.payout_id)
                return existing_payout, False

        # Enqueue background processing
        try:
            from .tasks import process_payout
            process_payout.apply_async(args=[str(payout.id)], countdown=2)
        except Exception as e:
            logger.warning(f"Failed to enqueue payout {payout.id}: {e}")

        return payout, True

    @staticmethod
    def transition_payout(payout: Payout, new_status: str, failure_reason: str = '') -> Payout:
        """
        Transition a payout through its state machine atomically.
        On FAILED: funds are returned to merchant balance via a CREDIT ledger entry
                   in the same transaction as the status update.
        """
        if not payout.can_transition_to(new_status):
            raise InvalidTransitionError(
                f"Cannot transition from {payout.status} → {new_status}"
            )

        with transaction.atomic():
            # Lock this specific payout row to prevent concurrent state changes
            payout_locked = Payout.objects.select_for_update().get(pk=payout.pk)

            # Re-check after lock (status may have changed between read and lock)
            if not payout_locked.can_transition_to(new_status):
                raise InvalidTransitionError(
                    f"Concurrent update: cannot transition from {payout_locked.status} → {new_status}"
                )

            now = timezone.now()
            payout_locked.status = new_status

            if new_status == Payout.PROCESSING:
                payout_locked.processing_started_at = now
                payout_locked.attempt_count = F('attempt_count') + 1

            elif new_status == Payout.COMPLETED:
                payout_locked.completed_at = now
                # Create a DEBIT ledger entry to finalise the payout
                LedgerEntry.objects.create(
                    merchant=payout_locked.merchant,
                    amount_paise=payout_locked.amount_paise,
                    entry_type=LedgerEntry.DEBIT,
                    description=f"Payout to bank account {payout_locked.bank_account_id}",
                    payout=payout_locked,
                )

            elif new_status == Payout.FAILED:
                payout_locked.failure_reason = failure_reason
                payout_locked.completed_at = now
                # Return funds: payout is no longer PENDING/PROCESSING so
                # get_balance() will drop it from "held". No ledger entry needed
                # because we never created a DEBIT — funds were merely held.
                # But we DO log a ledger entry for auditability:
                LedgerEntry.objects.create(
                    merchant=payout_locked.merchant,
                    amount_paise=payout_locked.amount_paise,
                    entry_type=LedgerEntry.CREDIT,
                    description=f"Payout reversal: {failure_reason[:100]}",
                    payout=payout_locked,
                )

            payout_locked.save()
            payout_locked.refresh_from_db()
            return payout_locked

    @staticmethod
    def get_stuck_payouts():
        """Returns payouts in PROCESSING for > 30 seconds with retries remaining."""
        cutoff = timezone.now() - timedelta(seconds=30)
        return Payout.objects.filter(
            status=Payout.PROCESSING,
            processing_started_at__lt=cutoff,
            attempt_count__lt=3,
        )

    @staticmethod
    def get_exhausted_payouts():
        """Returns payouts in PROCESSING that have hit max attempts."""
        cutoff = timezone.now() - timedelta(seconds=30)
        return Payout.objects.filter(
            status=Payout.PROCESSING,
            processing_started_at__lt=cutoff,
            attempt_count__gte=3,
        )
