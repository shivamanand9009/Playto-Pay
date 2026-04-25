# PayoutPro — Merchant Payout Service

A production-grade merchant payout system with real concurrency control, idempotency, and state machine enforcement.

**Stack:** Django + DRF · PostgreSQL · Celery + Redis · React + Vite

---

## Live Demo

> Frontend: https://your-frontend.vercel.app  
> API: https://your-api.railway.app/api/v1/merchants/

Test merchants are pre-seeded with credit history. Request payouts and watch them settle in real time.

---

## Local Setup

### Prerequisites
- Docker Desktop (running)
- Node.js 18+ (for frontend)

### 1. Clone

```bash
git clone https://github.com/yourusername/payoutpro.git
cd payoutpro
```

### 2. Start the backend (API + DB + Redis + Celery)

```bash
docker compose up --build
```

First run takes ~2 minutes. When you see:

```
api-1 | Starting development server at http://0.0.0.0:8000/
```

the stack is ready.

Services running:
| Service | URL |
|---|---|
| Django API | http://localhost:8000/api/v1/ |
| Celery Flower | http://localhost:5555 |
| PostgreSQL | localhost:5432 |
| Redis | localhost:6379 |

### 3. Start the frontend

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173

---

## Seed Script

Merchants are seeded automatically on first `docker compose up` via:

```bash
python manage.py seed_merchants
```

This creates 3 merchants with credit history:

| Merchant | Balance | Bank |
|---|---|---|
| Arjun Electronics | ₹7,199.99 | HDFC0001234567 |
| Priya Fashions | ₹670.00 | ICICI0009876543 |
| Mumbai Bites | ₹611.00 | AXIS0005551234 |

To reseed from scratch:

```bash
docker compose exec api python manage.py flush --no-input
docker compose exec api python manage.py migrate
docker compose exec api python manage.py seed_merchants
```

---

## Running Tests

```bash
docker compose exec api python manage.py test payouts
```

### Test coverage

| Test | What it verifies |
|---|---|
| `ConcurrencyTest.test_concurrent_payout_requests_exactly_one_succeeds` | Two simultaneous ₹60 requests on ₹100 balance — exactly one succeeds, one gets `InsufficientFundsError`. Uses `threading.Barrier` to synchronise. |
| `IdempotencyTest.test_same_key_returns_same_payout` | Same `Idempotency-Key` returns identical payout object, `created=False` on second call. |
| `IdempotencyTest.test_same_key_no_duplicate_payout` | DB has exactly 1 payout record after 2 calls with same key. |
| `IdempotencyTest.test_key_scoped_per_merchant` | Same key used by two merchants creates two independent payouts. |
| `IdempotencyTest.test_expired_key_raises` | Key past 24h TTL raises `IdempotencyKeyExpiredError`. |
| `BalanceInvariantTest` | `Σ(credits) − Σ(debits) == available + held` before and after payout lifecycle. |
| `StateMachineTest` | All legal transitions pass; `completed→pending`, `failed→completed`, `pending→completed` raise `InvalidTransitionError`. |
| `StateMachineTest.test_failed_fund_return_is_atomic` | CREDIT ledger entry and FAILED status update are in the same transaction. |
| `RetryLogicTest` | Stuck payout detection (>30s in PROCESSING), exhausted payout detection (3 attempts). |

---

## API Reference

### List merchants
```
GET /api/v1/merchants/
```

### Merchant dashboard (balance + ledger + payouts)
```
GET /api/v1/merchants/{merchant_id}/
```

### Request payout
```
POST /api/v1/merchants/{merchant_id}/payouts/
Idempotency-Key: <uuid>         ← required header
Content-Type: application/json

{
  "amount_paise": 50000,
  "bank_account_id": "HDFC0001234567"
}
```

Returns `201` on first call, `200` on duplicate key (same body both times).

### Payout status
```
GET /api/v1/merchants/{merchant_id}/payouts/{payout_id}/
```

### Balance invariant audit
```
GET /api/v1/merchants/{merchant_id}/invariant/
```

Returns `{ "invariant_holds": true, ... }`.

### cURL example

```bash
# Get merchants
curl http://localhost:8000/api/v1/merchants/

# Request a payout
curl -X POST http://localhost:8000/api/v1/merchants/<id>/payouts/ \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"amount_paise": 10000, "bank_account_id": "HDFC0001234567"}'

# Test idempotency — run twice with same key, get same response
KEY=$(uuidgen)
curl -X POST http://localhost:8000/api/v1/merchants/<id>/payouts/ \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $KEY" \
  -d '{"amount_paise": 5000, "bank_account_id": "HDFC0001234567"}'

curl -X POST http://localhost:8000/api/v1/merchants/<id>/payouts/ \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $KEY" \
  -d '{"amount_paise": 5000, "bank_account_id": "HDFC0001234567"}'
# Both return identical JSON
```

---

## Architecture

### Money integrity
- All amounts stored as `BigIntegerField` in paise. No `FloatField`. No `DecimalField`.
- Balance **derived** from ledger via `SUM` aggregation — never stored as a mutable column.
- Invariant: `Σ(credits) − Σ(debits) = available + held`.

### Concurrency
`SELECT FOR UPDATE` on the merchant row serialises concurrent payout requests for the same merchant. Balance check and payout creation happen inside the same atomic transaction under the lock.

### Idempotency
Three-layer: pre-transaction lookup → atomic insert inside lock → `IntegrityError` catch for the concurrent-same-key race. Keys scoped per `(merchant_id, key)`, expire after 24 hours.

### State machine
```
pending → processing → completed
                    ↘ failed
```
All other transitions raise `InvalidTransitionError`. Checked before lock (fast fail) and after lock (concurrent safety). Fund return on `FAILED` is atomic with state change.

### Background processor (Celery)
- `process_payout`: 70% success / 20% fail / 10% hang (simulated bank)
- `retry_stuck_payouts`: runs every 15s via Celery Beat, retries payouts stuck in `PROCESSING` >30s with exponential backoff (5s → 10s → 20s), fails at 3 attempts

---

## Deployment

### Railway (recommended — free tier)

```bash
# Install Railway CLI
npm install -g @railway/cli
railway login

# From project root
railway init
railway add postgresql
railway add redis
railway up
```

Set environment variables in Railway dashboard:
```
DJANGO_SETTINGS_MODULE=config.settings
SECRET_KEY=<generate-one>
DEBUG=false
```

### Frontend (Vercel)

```bash
cd frontend
npm install -g vercel
vercel
```

Update `BACKEND` in `src/App.jsx` to your Railway API URL before deploying.

---

## Project Structure

```
payoutpro/
├── docker-compose.yml
├── README.md
├── EXPLAINER.md
├── backend/
│   ├── Dockerfile
│   ├── manage.py
│   ├── requirements.txt
│   ├── config/
│   │   ├── settings.py
│   │   ├── urls.py
│   │   └── celery.py
│   └── payouts/
│       ├── models.py       ← Merchant, LedgerEntry, Payout, IdempotencyKey
│       ├── services.py     ← All money-touching logic (SELECT FOR UPDATE, idempotency)
│       ├── tasks.py        ← Celery: process_payout, retry_stuck_payouts
│       ├── views.py        ← DRF API endpoints
│       ├── urls.py
│       ├── serializers.py
│       ├── tests.py        ← Concurrency, idempotency, state machine, invariant tests
│       └── management/commands/seed_merchants.py
└── frontend/
    └── src/
        └── App.jsx         ← React dashboard, polls API every 3s
```
