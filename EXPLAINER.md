# EXPLAINER.md

---

## 1. The Ledger

### Balance Calculation Query

```python
# From payouts/models.py — Merchant.get_balance()

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
debits  = result['total_debits'] or 0

held = Payout.objects.filter(
    merchant=self,
    status__in=[Payout.PENDING, Payout.PROCESSING]
).aggregate(h=Sum('amount_paise'))['h'] or 0

available = (credits - debits) - held
```

**Why this model?**

Balance is never stored as a column. It is always derived from the ledger in a single aggregation query. This means:

- There is no "update balance" step that could be missed, duplicated, or race-conditioned.
- The source of truth is the append-only ledger, not a mutable number.
- Credits and debits are separate entry types (not signed integers) so the audit trail is human-readable and queryable by direction without sign arithmetic.
- `held` is computed from live payout rows — payouts in `pending` or `processing` status represent money that is spoken for but not yet debited. They vanish from `held` and become a permanent `DEBIT` ledger entry only when `completed`.
- `amount_paise` is always a positive `BigIntegerField`. No floats, no decimals. Rupee fractions are expressed as paise (integer). `₹1 = 100 paise`.

The invariant this enforces: `Σ(credits) − Σ(debits) = available + held`. This is verified by the `/api/v1/merchants/{id}/invariant/` endpoint and tested in `payouts/tests.py`.

---

## 2. The Lock

### Exact code that prevents overdraw

```python
# From payouts/services.py — PayoutService.request_payout()

with transaction.atomic():
    # SELECT FOR UPDATE on merchant row — serialises concurrent payout
    # requests for the same merchant at the database level.
    # Two simultaneous requests for the same merchant queue up here.
    # The second waits until the first commits before acquiring the lock.
    merchant_locked = Merchant.objects.select_for_update().get(pk=merchant.pk)

    # Balance is computed INSIDE the lock via a DB aggregation.
    # No Python arithmetic on previously-fetched rows.
    available, _ = merchant_locked.get_balance()

    if available < amount_paise:
        raise InsufficientFundsError(
            f"Insufficient funds: available {available}p, requested {amount_paise}p"
        )

    # Payout created here — funds immediately appear in "held"
    # because get_balance() counts PENDING/PROCESSING payouts as held.
    payout = Payout.objects.create(
        merchant=merchant_locked,
        amount_paise=amount_paise,
        ...
        status=Payout.PENDING,
    )
```

**The database primitive: `SELECT FOR UPDATE`**

`SELECT FOR UPDATE` acquires a row-level exclusive lock on the merchant row for the duration of the transaction. PostgreSQL guarantees that no other transaction can acquire the same lock until this one commits or rolls back.

Concurrency scenario — merchant has ₹100, two simultaneous ₹60 requests:

```
Request A                          Request B
─────────────────────────────────────────────────────
BEGIN TRANSACTION
SELECT FOR UPDATE (merchant)       SELECT FOR UPDATE (merchant)
  → acquires lock                    → BLOCKS, waiting for A

get_balance() → 100p available
100 >= 60 → OK
INSERT payout (60p, PENDING)
COMMIT
  → releases lock                  → unblocks, acquires lock

                                   get_balance() → 40p available
                                   40 < 60 → InsufficientFundsError
                                   ROLLBACK
```

Exactly one succeeds. The check-then-deduct is atomic — there is no window between reading the balance and creating the payout where another request can sneak in.

---

## 3. The Idempotency

### How the system recognises a seen key

```python
# From payouts/services.py — PayoutService.request_payout()

# Step 1: Check BEFORE entering the main transaction.
# Fast path — if we have seen this key, return immediately.
try:
    idem_record = IdempotencyKey.objects.get(merchant=merchant, key=idempotency_key)
    if idem_record.is_expired():
        raise IdempotencyKeyExpiredError("Idempotency key has expired.")
    payout = Payout.objects.get(id=idem_record.payout_id)
    return payout, False          # ← same payout, created=False
except IdempotencyKey.DoesNotExist:
    pass                          # first time we see this key

# Step 2: Inside the SELECT FOR UPDATE transaction, after creating the payout:
try:
    IdempotencyKey.objects.create(
        merchant=merchant_locked,
        key=idempotency_key,
        response_status=201,
        response_body={'payout_id': str(payout.id), 'status': payout.status},
        payout=payout,
        expires_at=expires_at,    # now + 24 hours
    )
except IntegrityError:
    # Step 3: Race condition — another request created the key between
    # our check and our insert. Roll back and return the winner's record.
    transaction.set_rollback(True)
    idem_record = IdempotencyKey.objects.get(merchant=merchant, key=idempotency_key)
    existing_payout = Payout.objects.get(id=idem_record.payout_id)
    return existing_payout, False
```

**What happens if the first request is in-flight when the second arrives?**

The `IdempotencyKey` table has a `UNIQUE constraint on (merchant_id, key)`. The flow has three layers of protection:

1. **Fast path (before transaction):** Second request checks for the key. If first has already committed, it finds the record and returns immediately — no duplicate work.

2. **Race path (both in-flight simultaneously):** Both requests pass the initial check (key doesn't exist yet), both enter `SELECT FOR UPDATE`, both queue up on the merchant lock. The first commits and creates the `IdempotencyKey` record. The second, after acquiring the lock, attempts `INSERT` on `IdempotencyKey` — PostgreSQL raises `IntegrityError` on the unique constraint. The `except IntegrityError` block catches this, rolls back the payout creation, and returns the first request's payout.

3. **Expiry:** Keys expire after 24 hours. Expired keys raise `IdempotencyKeyExpiredError` so stale keys from yesterday can't suppress legitimate new requests.

Keys are scoped to `(merchant_id, key)` — the same UUID used by two different merchants creates two independent records.

---

## 4. The State Machine

### Where failed-to-completed is blocked

```python
# From payouts/models.py — Payout model

VALID_TRANSITIONS = {
    Payout.PENDING:    [Payout.PROCESSING],
    Payout.PROCESSING: [Payout.COMPLETED, Payout.FAILED],
    Payout.COMPLETED:  [],          # terminal — no transitions allowed
    Payout.FAILED:     [],          # terminal — no transitions allowed
}

def can_transition_to(self, new_status):
    return new_status in self.VALID_TRANSITIONS.get(self.status, [])
```

```python
# From payouts/services.py — PayoutService.transition_payout()

def transition_payout(payout, new_status, failure_reason=''):
    if not payout.can_transition_to(new_status):
        raise InvalidTransitionError(
            f"Cannot transition from {payout.status} → {new_status}"
        )

    with transaction.atomic():
        # Re-acquire lock and re-check AFTER lock in case of concurrent transitions
        payout_locked = Payout.objects.select_for_update().get(pk=payout.pk)

        if not payout_locked.can_transition_to(new_status):
            raise InvalidTransitionError(
                f"Concurrent update: cannot transition from {payout_locked.status} → {new_status}"
            )
        ...
```

`FAILED → COMPLETED` is blocked because `VALID_TRANSITIONS[FAILED]` is an empty list. `can_transition_to('completed')` returns `False`. `transition_payout` raises `InvalidTransitionError` before touching the database.

The check runs **twice**: once on the pre-lock object (fast fail, avoids acquiring the lock unnecessarily), and once after `SELECT FOR UPDATE` on the freshly-read row (handles the case where two workers race to transition the same payout — the second sees the post-commit status and fails cleanly).

Fund return on `FAILED` is atomic with the state change — the `CREDIT` ledger entry and the `status = 'failed'` update are inside the same `transaction.atomic()` block. They cannot diverge.

---

## 5. The AI Audit

### Where the generated code had a subtle race condition

**What the AI initially generated** for `request_payout`:

```python
# AI-generated version — WRONG
def request_payout(merchant, amount_paise, bank_account_id, idempotency_key):
    # Check idempotency
    existing = IdempotencyKey.objects.filter(merchant=merchant, key=idempotency_key).first()
    if existing:
        return Payout.objects.get(id=existing.payout_id), False

    # Check balance
    available, _ = merchant.get_balance()   # ← fetched OUTSIDE transaction
    if available < amount_paise:
        raise InsufficientFundsError(...)

    with transaction.atomic():
        payout = Payout.objects.create(...)
        IdempotencyKey.objects.create(...)

    return payout, True
```

**What's wrong:**

Two race conditions in one function:

1. **Balance read is outside the transaction and lock.** `get_balance()` runs before `transaction.atomic()`. Two concurrent requests both read `available = 10000`, both pass the `if available < amount_paise` check, then both enter the transaction and create payouts — overdrawing the balance. The check-then-deduct is not atomic.

2. **Idempotency check is not atomic with payout creation.** Two requests with the same key both pass `filter(...).first()` returning `None` (key doesn't exist yet), then both proceed to create a payout inside `transaction.atomic()`. One of them creates both the payout and the key; the other creates a second payout before hitting the unique constraint on `IdempotencyKey` — but the second payout is already committed by the time `IntegrityError` fires (it's inside the same `atomic()` block, so both rows commit together). The duplicate payout is not rolled back.

**What was replaced with:**

```python
# Corrected version
def request_payout(merchant, amount_paise, bank_account_id, idempotency_key):
    # Idempotency check BEFORE main transaction (fast path for seen keys)
    try:
        idem_record = IdempotencyKey.objects.get(merchant=merchant, key=idempotency_key)
        ...
        return payout, False
    except IdempotencyKey.DoesNotExist:
        pass

    with transaction.atomic():
        # Lock merchant row FIRST — balance check runs inside the lock
        merchant_locked = Merchant.objects.select_for_update().get(pk=merchant.pk)

        # Balance computed INSIDE lock via DB aggregation — not Python arithmetic
        available, _ = merchant_locked.get_balance()
        if available < amount_paise:
            raise InsufficientFundsError(...)

        payout = Payout.objects.create(merchant=merchant_locked, ...)

        # Idempotency key created INSIDE same transaction as payout
        try:
            IdempotencyKey.objects.create(...)
        except IntegrityError:
            # Another request won the race — roll back and return theirs
            transaction.set_rollback(True)
            ...
            return existing_payout, False

    return payout, True
```

The fix: `SELECT FOR UPDATE` serialises the balance check and payout creation at the database level. Splitting the idempotency `INSERT` inside the same transaction with an `IntegrityError` catch handles the concurrent-same-key race without a second payout ever being committed.

---

## Bonuses implemented

- **`docker-compose.yml`** — full stack: PostgreSQL, Redis, Django API, Celery worker, Celery Beat (periodic retry), Flower (task monitor).
- **Audit log** — every payout state transition creates a `LedgerEntry` (DEBIT on complete, CREDIT reversal on fail). The full money trail is queryable at all times.
- **Event sourcing-style ledger** — balance is never stored; it is always derived from the append-only ledger. The ledger is the source of truth.
