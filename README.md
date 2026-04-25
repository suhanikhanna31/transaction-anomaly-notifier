# Transaction Anomaly Notifier

> Real-time transaction fraud detection API with async alerting, batch scoring, PostgreSQL audit logging, and full CI/CD — built with Pine Labs' payments infrastructure in mind.

---

## What This Does

Payment networks process millions of transactions per minute. A single undetected fraud event costs on average ₹40,000+. This system scores each transaction in **< 10 ms** using a multi-rule engine, persists audit logs to PostgreSQL, and dispatches async email + Slack alerts via Celery — without blocking the HTTP response.

---

## Tech Stack

| Layer | Technology |
|---|---|
| API | FastAPI + Uvicorn |
| Scoring Engine | NumPy / Pandas (z-score + rule-based) |
| Async Alerts | Celery + Redis |
| Database | PostgreSQL (psycopg2) |
| Testing | Pytest + pytest-cov (80%+ coverage enforced) |
| Containerisation | Docker + Docker Compose |
| CI/CD | GitHub Actions |
| Serverless | AWS Lambda + API Gateway |

---

## Project Structure

```
transaction-anomaly-notifier/
├── main.py                    ← FastAPI app — /predict, /batch-predict, /audit-logs
├── lambda_handler.py          ← AWS Lambda entry point (serverless deploy)
├── celery_worker.py           ← Async alert dispatcher (email + Slack)
├── transactions.csv           ← 1,000-row baseline dataset (10% injected anomalies)
├── requirements.txt           ← All dependencies with pinned versions
├── Dockerfile                 ← Docker build
├── docker-compose.yml         ← API + Celery worker + Postgres + Redis
├── .env.example               ← Environment variable template
├── tests/
│   └── test_api.py            ← 25 pytest tests — unit + integration + resilience
└── .github/
    └── workflows/
        └── ci.yml             ← GitHub Actions: test → lint → Docker build
```

---

## Quickstart (Local)

### Option A — Docker (recommended, zero config)

```bash
cp .env.example .env
docker compose up --build
```

All four services start: API on `:8000`, PostgreSQL on `:5432`, Redis on `:6379`, Celery worker.

### Option B — Python only

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

Open **http://localhost:8000/docs** for Swagger UI.

---

## API Reference

### `POST /predict`

```json
// Request
{ "transaction_id": "txn_a8f3b2", "amount": 14500, "time_gap_minutes": 0.3 }

// Response
{
  "transaction_id": "txn_a8f3b2",
  "amount": 14500,
  "z_score": 2.71,
  "risk_score": 85,
  "status": "HIGH_RISK",
  "alert_triggered": true,
  "alert_reason": "high z-score (2.71); amount exceeds ₹10,000; rapid succession (0.3 min gap)",
  "recommendation": "Block transaction and notify compliance team immediately."
}
```

### `POST /batch-predict`
Score up to **100 transactions** in one request. Returns results + aggregate summary (high_risk_count, alert_rate_pct, avg_risk_score).

### `GET /audit-logs?status=HIGH_RISK&limit=20`
Recent scored transactions from PostgreSQL. Uses indexed columns for fast retrieval.

---

## Scoring Engine

Three independent rules → 0–100 risk score:

```
Rule 1 — Z-score     |z| > 3.0 → +50   |z| > 2.0 → +30   |z| > 1.5 → +15
Rule 2 — Value        > ₹10k  → +25    > ₹5k    → +10
Rule 3 — Velocity    gap < 1 min → +25  gap < 30% avg → +10
```

Alert fires when `risk_score ≥ 60` OR (`risk_score ≥ 30` AND 2+ rules fired).

---

## Async Alert Architecture

```
POST /predict → compute_risk() → PostgreSQL
                      │
              alert_triggered?
                      ↓
            Celery task → Redis → Worker
                                   ├── SMTP email
                                   └── Slack webhook
```

API returns in **< 10 ms**. Alerts delivered async with 3-retry exponential back-off.

---

## Tests

```bash
pytest tests/ -v --cov=. --cov-report=term-missing
```

25 tests across: rule engine, endpoints, batch scoring, DB resilience. CI enforces ≥ 80% coverage.

---

## AWS Deployment

- **Lambda**: zip project → upload → handler `lambda_handler.handler` → attach API Gateway
- **EC2**: `uvicorn main:app --host 0.0.0.0 --port 8000`
- **S3**: Swap `pd.read_csv("transactions.csv")` with a boto3 S3 fetch for large baselines

---

## Environment Variables

Copy `.env.example` → `.env`. Key variables: `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `REDIS_URL`, `SMTP_*`, `ALERT_EMAIL_TO`, `SLACK_WEBHOOK_URL`.
