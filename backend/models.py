"""
PayoutPro Models
================
All money stored as paise (integer). No floats. No decimals.
Balance is DERIVED from ledger entries — never stored as a mutable field.
"""
import uuid
from django.db import models
from django.db.models import Sum, Q
from django.utils import timezone


class Merchant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    bank_account_id = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def get_balance(self):
        """
        Computes balance at DB level using a single aggregation query.
        Returns (available_paise, held_paise).
        available = credits - debits (completed payouts)
        held     = sum of pending/processing payouts
        """
        from django.db.models import Sum, Case, When, IntegerField
        result = LedgerEntry.objects.filter(merchant=self).aggregate(
            total_credits=Sum(
                Case(When(entry_type=LedgerEntry.CREDIT, then='amount_paise'),
                     default=0, output_field=models.BigIntegerField())
            ),
            total_debits=Sum(
                Case(When(entry_type=LedgerEntry.DEBIT, then='amount_paise'),
                     default=0, output_field=models.BigIntegerField())
            ),
        )
        credits = result['total_credits'] or 0
        debits = result['total_debits'] or 0

        held = Payout.objects.filter(
            merchant=self,
            status__in=[Payout.PENDING, Payout.PROCESSING]
        ).aggregate(h=Sum('amount_paise'))['h'] or 0

        total_net = credits - debits
        available = total_net - held
        return available, held

    def __str__(self):
        return self.name


class LedgerEntry(models.Model):
    CREDIT = 'credit'
    DEBIT  = 'debit'
    ENTRY_TYPES = [(CREDIT, 'Credit'), (DEBIT, 'Debit')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name='ledger_entries')
    amount_paise = models.BigIntegerField()  # Always positive
    entry_type = models.CharField(max_length=6, choices=ENTRY_TYPES)
    description = models.TextField()
    payout = models.ForeignKey(
        'Payout', null=True, blank=True,
        on_delete=models.PROTECT, related_name='ledger_entries'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['merchant', 'created_at'])]

    def __str__(self):
        return f"{self.entry_type} {self.amount_paise}p for {self.merchant.name}"


class IdempotencyKey(models.Model):
    """
    Stores the serialized response for a given (merchant, key) pair.
    Scoped per-merchant. Expires 24 hours after first creation.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.CASCADE)
    key = models.CharField(max_length=255)
    response_status = models.IntegerField()
    response_body = models.JSONField()
    payout = models.ForeignKey(
        'Payout', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='idempotency_keys'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        unique_together = [('merchant', 'key')]
        indexes = [models.Index(fields=['merchant', 'key'])]

    def is_expired(self):
        return timezone.now() > self.expires_at


class Payout(models.Model):
    PENDING    = 'pending'
    PROCESSING = 'processing'
    COMPLETED  = 'completed'
    FAILED     = 'failed'

    STATUS_CHOICES = [
        (PENDING,    'Pending'),
        (PROCESSING, 'Processing'),
        (COMPLETED,  'Completed'),
        (FAILED,     'Failed'),
    ]

    # Valid transitions only
    VALID_TRANSITIONS = {
        PENDING:    [PROCESSING],
        PROCESSING: [COMPLETED, FAILED],
        COMPLETED:  [],
        FAILED:     [],
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merchant = models.ForeignKey(Merchant, on_delete=models.PROTECT, related_name='payouts')
    amount_paise = models.BigIntegerField()
    bank_account_id = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING)
    attempt_count = models.IntegerField(default=0)
    max_attempts = models.IntegerField(default=3)
    failure_reason = models.TextField(blank=True)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['merchant', 'status']),
            models.Index(fields=['status', 'processing_started_at']),
        ]

    def can_transition_to(self, new_status):
        return new_status in self.VALID_TRANSITIONS.get(self.status, [])

    def __str__(self):
        return f"Payout {self.id} [{self.status}] {self.amount_paise}p"
