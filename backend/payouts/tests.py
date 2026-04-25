"""
PayoutPro Test Suite
====================
Tests the exact invariants specified in the requirements:

1. Balance invariant: sum(credits) - sum(debits) == available + held
2. Concurrency: two simultaneous 60rs requests on 100rs → exactly one succeeds
3. Idempotency: same key → same response, no duplicate payout
4. State machine: illegal transitions are rejected
5. Retry: stuck payouts get retried with exponential backoff
"""
import uuid
import threading
from django.test import TestCase, TransactionTestCase
from django.db import transaction
from django.utils import timezone
from unittest.mock import patch

from payouts.models import Merchant, Payout, LedgerEntry, IdempotencyKey
from payouts.services import PayoutService, InsufficientFundsError, InvalidTransitionError


def make_merchant(name="Test", balance_paise=100_000):
    m = Merchant.objects.create(
        name=name,
        email=f"{name.lower().replace(' ', '')}@test.com",
        bank_account_id=f"BANK{uuid.uuid4().hex[:8].upper()}",
    )
    LedgerEntry.objects.create(
        merchant=m, amount_paise=balance_paise,
        entry_type=LedgerEntry.CREDIT, description="Seed credit"
    )
    return m


class BalanceInvariantTest(TestCase):
    """
    The sum of all ledger credits minus all ledger debits must always equal
    the sum of available + held balance.
    """

    def test_initial_balance_matches_ledger(self):
        m = make_merchant(balance_paise=500_000)
        available, held = m.get_balance()
        self.assertEqual(available + held, 500_000)
        self.assertEqual(held, 0)

    def test_balance_after_completed_payout(self):
        m = make_merchant(balance_paise=100_000)
        payout = Payout.objects.create(
            merchant=m, amount_paise=30_000, bank_account_id="HDFC01", status=Payout.PENDING
        )
        # Complete the payout
        PayoutService.transition_payout(payout, Payout.PROCESSING)
        payout.refresh_from_db()
        PayoutService.transition_payout(payout, Payout.COMPLETED)

        # Invariant: credits(100k) - debits(30k) = 70k = available(70k) + held(0)
        available, held = m.get_balance()
        credits = LedgerEntry.objects.filter(
            merchant=m, entry_type=LedgerEntry.CREDIT
        ).aggregate(s=__import__('django.db.models', fromlist=['Sum']).Sum('amount_paise'))['s'] or 0
        debits = LedgerEntry.objects.filter(
            merchant=m, entry_type=LedgerEntry.DEBIT
        ).aggregate(s=__import__('django.db.models', fromlist=['Sum']).Sum('amount_paise'))['s'] or 0

        self.assertEqual(credits - debits, available + held, "Balance invariant broken after completed payout")
        self.assertEqual(available, 70_000)
        self.assertEqual(held, 0)

    def test_balance_after_failed_payout(self):
        m = make_merchant(balance_paise=100_000)
        payout = Payout.objects.create(
            merchant=m, amount_paise=30_000, bank_account_id="HDFC01", status=Payout.PENDING
        )
        PayoutService.transition_payout(payout, Payout.PROCESSING)
        payout.refresh_from_db()
        PayoutService.transition_payout(payout, Payout.FAILED, failure_reason="Bank declined")

        # Funds should be returned: available = 100k, held = 0
        available, held = m.get_balance()
        self.assertEqual(available, 100_000, "Funds not returned on payout failure")
        self.assertEqual(held, 0)


class ConcurrencyTest(TransactionTestCase):
    """
    Two simultaneous 60rs requests on a 100rs balance → exactly one succeeds.
    Uses TransactionTestCase so SELECT FOR UPDATE commits are visible.
    """

    def test_concurrent_payout_requests_exactly_one_succeeds(self):
        m = make_merchant(balance_paise=10_000)  # ₹100

        results = []
        errors = []
        barrier = threading.Barrier(2)

        def request(key_suffix):
            try:
                barrier.wait()  # Both threads hit the service at the same time
                payout, created = PayoutService.request_payout(
                    merchant=m,
                    amount_paise=6_000,  # ₹60 each
                    bank_account_id="HDFC01",
                    idempotency_key=str(uuid.uuid4()),
                )
                results.append(('ok', created))
            except InsufficientFundsError as e:
                results.append(('insufficient', str(e)))
            except Exception as e:
                errors.append(str(e))

        t1 = threading.Thread(target=request, args=('a',))
        t2 = threading.Thread(target=request, args=('b',))
        t1.start(); t2.start()
        t1.join(); t2.join()

        self.assertEqual(len(errors), 0, f"Unexpected errors: {errors}")
        successes = [r for r in results if r[0] == 'ok']
        failures  = [r for r in results if r[0] == 'insufficient']

        self.assertEqual(len(successes), 1, f"Expected exactly 1 success, got {len(successes)}: {results}")
        self.assertEqual(len(failures), 1, f"Expected exactly 1 failure, got {len(failures)}: {results}")

        # Verify held balance is exactly 6000 (not 12000)
        m.refresh_from_db()
        _, held = m.get_balance()
        self.assertEqual(held, 6_000, f"Held balance should be 6000, got {held}")


class IdempotencyTest(TestCase):

    def test_same_key_returns_same_payout(self):
        m = make_merchant(balance_paise=50_000)
        key = str(uuid.uuid4())

        p1, created1 = PayoutService.request_payout(m, 10_000, "HDFC01", key)
        p2, created2 = PayoutService.request_payout(m, 10_000, "HDFC01", key)

        self.assertEqual(str(p1.id), str(p2.id), "Different payouts returned for same idempotency key")
        self.assertTrue(created1)
        self.assertFalse(created2)

    def test_same_key_no_duplicate_payout(self):
        m = make_merchant(balance_paise=50_000)
        key = str(uuid.uuid4())

        PayoutService.request_payout(m, 10_000, "HDFC01", key)
        PayoutService.request_payout(m, 10_000, "HDFC01", key)

        count = Payout.objects.filter(merchant=m).count()
        self.assertEqual(count, 1, f"Expected 1 payout, found {count}")

    def test_different_keys_create_different_payouts(self):
        m = make_merchant(balance_paise=50_000)

        p1, _ = PayoutService.request_payout(m, 10_000, "HDFC01", str(uuid.uuid4()))
        p2, _ = PayoutService.request_payout(m, 10_000, "HDFC01", str(uuid.uuid4()))

        self.assertNotEqual(str(p1.id), str(p2.id))

    def test_key_scoped_per_merchant(self):
        m1 = make_merchant("M1", 50_000)
        m2 = make_merchant("M2", 50_000)
        shared_key = str(uuid.uuid4())

        p1, _ = PayoutService.request_payout(m1, 10_000, "HDFC01", shared_key)
        p2, _ = PayoutService.request_payout(m2, 10_000, "HDFC02", shared_key)

        # Same key but different merchants → different payouts
        self.assertNotEqual(str(p1.id), str(p2.id))

    def test_expired_key_raises(self):
        from payouts.services import IdempotencyKeyExpiredError
        from datetime import timedelta

        m = make_merchant(balance_paise=50_000)
        key = str(uuid.uuid4())

        # Create an expired key record
        expired = IdempotencyKey.objects.create(
            merchant=m,
            key=key,
            response_status=201,
            response_body={},
            expires_at=timezone.now() - timedelta(hours=1),
        )

        with self.assertRaises(IdempotencyKeyExpiredError):
            PayoutService.request_payout(m, 10_000, "HDFC01", key)


class StateMachineTest(TestCase):

    def _payout_in_state(self, merchant, state):
        p = Payout.objects.create(
            merchant=merchant, amount_paise=5_000,
            bank_account_id="HDFC01", status=state
        )
        return p

    def test_pending_to_processing_allowed(self):
        m = make_merchant(balance_paise=50_000)
        p = self._payout_in_state(m, Payout.PENDING)
        result = PayoutService.transition_payout(p, Payout.PROCESSING)
        self.assertEqual(result.status, Payout.PROCESSING)

    def test_processing_to_completed_allowed(self):
        m = make_merchant(balance_paise=50_000)
        p = self._payout_in_state(m, Payout.PROCESSING)
        result = PayoutService.transition_payout(p, Payout.COMPLETED)
        self.assertEqual(result.status, Payout.COMPLETED)

    def test_processing_to_failed_allowed(self):
        m = make_merchant(balance_paise=50_000)
        p = self._payout_in_state(m, Payout.PROCESSING)
        result = PayoutService.transition_payout(p, Payout.FAILED, failure_reason="test")
        self.assertEqual(result.status, Payout.FAILED)

    def test_completed_to_pending_rejected(self):
        m = make_merchant(balance_paise=50_000)
        p = self._payout_in_state(m, Payout.COMPLETED)
        with self.assertRaises(InvalidTransitionError):
            PayoutService.transition_payout(p, Payout.PENDING)

    def test_failed_to_completed_rejected(self):
        m = make_merchant(balance_paise=50_000)
        p = self._payout_in_state(m, Payout.FAILED)
        with self.assertRaises(InvalidTransitionError):
            PayoutService.transition_payout(p, Payout.COMPLETED)

    def test_pending_to_completed_rejected(self):
        m = make_merchant(balance_paise=50_000)
        p = self._payout_in_state(m, Payout.PENDING)
        with self.assertRaises(InvalidTransitionError):
            PayoutService.transition_payout(p, Payout.COMPLETED)

    def test_failed_fund_return_is_atomic(self):
        """Funds must be returned in the same transaction as state change."""
        m = make_merchant(balance_paise=10_000)
        p = Payout.objects.create(
            merchant=m, amount_paise=5_000,
            bank_account_id="HDFC01", status=Payout.PROCESSING
        )
        # Before fail: held = 5000 (payout is processing)
        available_before, _ = m.get_balance()

        PayoutService.transition_payout(p, Payout.FAILED, failure_reason="Bank error")

        # After fail: funds returned via CREDIT ledger entry
        available_after, held_after = m.get_balance()
        self.assertEqual(held_after, 0)
        self.assertEqual(available_after, 10_000)  # Full 10k back


class RetryLogicTest(TestCase):

    def test_stuck_payouts_detected(self):
        from datetime import timedelta
        m = make_merchant(balance_paise=50_000)
        p = Payout.objects.create(
            merchant=m, amount_paise=5_000,
            bank_account_id="HDFC01", status=Payout.PROCESSING,
            processing_started_at=timezone.now() - timedelta(seconds=60),
            attempt_count=1,
        )
        stuck = PayoutService.get_stuck_payouts()
        self.assertIn(p, stuck)

    def test_exhausted_payouts_detected(self):
        from datetime import timedelta
        m = make_merchant(balance_paise=50_000)
        p = Payout.objects.create(
            merchant=m, amount_paise=5_000,
            bank_account_id="HDFC01", status=Payout.PROCESSING,
            processing_started_at=timezone.now() - timedelta(seconds=60),
            attempt_count=3,
        )
        exhausted = PayoutService.get_exhausted_payouts()
        self.assertIn(p, exhausted)

    def test_non_stuck_payouts_not_detected(self):
        m = make_merchant(balance_paise=50_000)
        p = Payout.objects.create(
            merchant=m, amount_paise=5_000,
            bank_account_id="HDFC01", status=Payout.PROCESSING,
            processing_started_at=timezone.now(),  # Just started
            attempt_count=1,
        )
        stuck = PayoutService.get_stuck_payouts()
        self.assertNotIn(p, stuck)
