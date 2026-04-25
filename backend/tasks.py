"""
Celery Tasks
============
process_payout   — simulates bank settlement, handles 70/20/10 outcomes
retry_stuck      — periodic task that finds stuck payouts and retries them
cleanup_exhausted — moves max-attempt payouts to FAILED and returns funds

Retry strategy:
  attempt 1 → wait 5s
  attempt 2 → wait 10s
  attempt 3 → wait 20s (exponential backoff)
  After 3 attempts still PROCESSING for 30s → force FAILED
"""
import random
import logging
import time

from celery import shared_task
from django.utils import timezone

from .models import Payout
from .services import PayoutService, InvalidTransitionError

logger = logging.getLogger(__name__)

BACKOFF = [5, 10, 20]  # seconds per attempt


def _simulate_bank(payout_id: str) -> str:
    """
    Simulates external bank API call.
    Returns: 'success' | 'failure' | 'hang'
    Distribution: 70% / 20% / 10%
    """
    roll = random.random()
    if roll < 0.70:
        return 'success'
    elif roll < 0.90:
        return 'failure'
    else:
        return 'hang'


@shared_task(bind=True, max_retries=3, name='payouts.process_payout')
def process_payout(self, payout_id: str):
    """
    Main payout processor.
    1. Transitions payout to PROCESSING
    2. Calls simulated bank
    3. Transitions to COMPLETED or FAILED
    4. On 'hang', leaves in PROCESSING — retry_stuck_payouts will pick it up
    """
    try:
        payout = Payout.objects.get(id=payout_id)
    except Payout.DoesNotExist:
        logger.error(f"Payout {payout_id} not found")
        return

    if payout.status not in [Payout.PENDING, Payout.PROCESSING]:
        logger.info(f"Payout {payout_id} already in terminal state {payout.status}, skipping")
        return

    # Move to PROCESSING
    if payout.status == Payout.PENDING:
        try:
            payout = PayoutService.transition_payout(payout, Payout.PROCESSING)
        except InvalidTransitionError as e:
            logger.warning(f"Could not transition {payout_id} to processing: {e}")
            return

    logger.info(f"Processing payout {payout_id}, attempt {payout.attempt_count}")

    # Simulate bank call
    outcome = _simulate_bank(payout_id)
    logger.info(f"Bank outcome for {payout_id}: {outcome}")

    if outcome == 'success':
        try:
            PayoutService.transition_payout(payout, Payout.COMPLETED)
            logger.info(f"Payout {payout_id} completed successfully")
        except InvalidTransitionError as e:
            logger.error(f"Failed to complete {payout_id}: {e}")

    elif outcome == 'failure':
        reason = "Bank declined the transfer"
        try:
            PayoutService.transition_payout(payout, Payout.FAILED, failure_reason=reason)
            logger.info(f"Payout {payout_id} failed: {reason}")
        except InvalidTransitionError as e:
            logger.error(f"Failed to fail {payout_id}: {e}")

    else:  # 'hang'
        # Leave in PROCESSING — the periodic retry task will handle it
        logger.warning(
            f"Payout {payout_id} hung in bank call (attempt {payout.attempt_count}). "
            f"Retry worker will pick it up after 30s."
        )


@shared_task(name='payouts.retry_stuck_payouts')
def retry_stuck_payouts():
    """
    Periodic task (runs every 15s).
    Finds payouts stuck in PROCESSING > 30s with attempts remaining and retries them.
    Uses exponential backoff: attempt N gets BACKOFF[N-1] seconds delay.
    """
    stuck = PayoutService.get_stuck_payouts()
    count = stuck.count()
    if count:
        logger.info(f"Found {count} stuck payouts, retrying...")

    for payout in stuck:
        attempt = payout.attempt_count
        delay = BACKOFF[min(attempt - 1, len(BACKOFF) - 1)]
        logger.info(
            f"Retrying stuck payout {payout.id} "
            f"(attempt {attempt}, delay {delay}s)"
        )
        # Reset to PENDING so process_payout can re-acquire PROCESSING state
        # We do this via a direct update to avoid state-machine check
        # (PROCESSING → PENDING is intentional for retry, not an illegal user transition)
        Payout.objects.filter(pk=payout.pk, status=Payout.PROCESSING).update(
            status=Payout.PENDING,
            processing_started_at=None,
        )
        process_payout.apply_async(args=[str(payout.id)], countdown=delay)

    # Mark exhausted payouts as FAILED and return funds
    exhausted = PayoutService.get_exhausted_payouts()
    for payout in exhausted:
        logger.warning(f"Payout {payout.id} exhausted all {payout.attempt_count} attempts → FAILED")
        try:
            PayoutService.transition_payout(
                payout, Payout.FAILED,
                failure_reason=f"Exhausted {payout.attempt_count} retry attempts — bank unresponsive"
            )
        except InvalidTransitionError as e:
            logger.error(f"Could not fail exhausted payout {payout.id}: {e}")
