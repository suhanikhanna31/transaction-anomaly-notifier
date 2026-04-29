from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List
import pandas as pd
import psycopg2
import psycopg2.extras
import os
import logging
from datetime import datetime




# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── App ─────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Fraud Detection API",
    description="Real-time fraud detection with rule-based alerting and audit logging",
    version="2.0.0",
)

# ─── Load & compute baseline stats ───────────────────────────────────────────
df = pd.read_csv("transactions.csv")
mean_amount = df["amount"].mean()
std_amount = df["amount"].std()
mean_time_gap = df["time_gap_minutes"].mean()


# ─── DB helpers ──────────────────────────────────────────────────────────────
def get_db():
    """Create a PostgreSQL connection. Yields connection, closes after use."""
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", 5432),
        dbname=os.getenv("DB_NAME", "fraud_db"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "password"),
    )
    try:
        yield conn
    finally:
        conn.close()


def create_audit_table(conn):
    """Create audit_logs table with indexes if it doesn't exist yet."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id               SERIAL PRIMARY KEY,
                transaction_id   TEXT,
                amount           FLOAT,
                time_gap_minutes FLOAT,
                z_score          FLOAT,
                risk_score       INT,
                status           TEXT,
                alert_triggered  BOOLEAN,
                alert_reason     TEXT,
                created_at       TIMESTAMP DEFAULT NOW()
            );

            -- Index for fast status filtering (e.g. WHERE status = 'ANOMALY')
            CREATE INDEX IF NOT EXISTS idx_audit_status
                ON audit_logs(status);

            -- Index for time-range queries on the audit trail
            CREATE INDEX IF NOT EXISTS idx_audit_created_at
                ON audit_logs(created_at);

            -- Index for looking up a specific transaction
            CREATE INDEX IF NOT EXISTS idx_audit_transaction_id
                ON audit_logs(transaction_id);
        """)
        conn.commit()


# ─── Pydantic models ─────────────────────────────────────────────────────────
class TransactionInput(BaseModel):
    transaction_id: Optional[str] = None
    amount: float
    time_gap_minutes: Optional[float] = None  # minutes since last transaction


class PredictionResponse(BaseModel):
    transaction_id: Optional[str]
    amount: float
    z_score: float
    risk_score: int  # 0-100 normalised score
    status: str  # NORMAL | SUSPICIOUS | HIGH_RISK
    alert_triggered: bool
    alert_reason: Optional[str]
    recommendation: str


# ─── Rule-based alerting engine ──────────────────────────────────────────────
def compute_risk(amount: float, time_gap_minutes: Optional[float] = None) -> dict:  # ← FIX 1: added = None
    """
    Multi-factor rule engine.
    Returns z_score, risk_score (0-100), status, alert flag, and reason.
    Reduces false positives by requiring multiple signals before escalating.
    """
    z_score = (amount - mean_amount) / std_amount
    risk_score = 0
    reasons = []

    # ── Rule 1: statistical outlier (z-score) ──────────────────────────────
    if abs(z_score) > 3.0:
        risk_score += 50
        reasons.append(f"extreme z-score ({z_score:.2f})")
    elif abs(z_score) > 2.0:
        risk_score += 30
        reasons.append(f"high z-score ({z_score:.2f})")
    elif abs(z_score) > 1.5:
        risk_score += 15
        reasons.append(f"elevated z-score ({z_score:.2f})")

    # ── Rule 2: absolute high-value threshold ──────────────────────────────
    if amount > 10_000:
        risk_score += 25
        reasons.append("amount exceeds $10,000")
    elif amount > 5_000:
        risk_score += 10
        reasons.append("amount exceeds $5,000")

    # ── Rule 3: velocity check (unusually fast transaction) ─────────────────
    if time_gap_minutes is not None:
        if time_gap_minutes < 1:
            risk_score += 25
            reasons.append(f"rapid succession ({time_gap_minutes:.1f} min gap)")
        elif time_gap_minutes < mean_time_gap * 0.3:
            risk_score += 10
            reasons.append(f"below-average time gap ({time_gap_minutes:.1f} min)")

    # ── Clamp & classify ───────────────────────────────────────────────────
    risk_score = min(risk_score, 100)

    if risk_score >= 60:
        status = "HIGH_RISK"
    elif risk_score >= 30:
        status = "SUSPICIOUS"
    else:
        status = "NORMAL"

    # Alert only when multiple independent rules fired (reduces false positives)
    alert_triggered = risk_score >= 60 or (risk_score >= 30 and len(reasons) >= 2)
    alert_reason = "; ".join(reasons) if reasons else None

    recommendations = {
        "HIGH_RISK": "Block transaction and notify compliance team immediately.",
        "SUSPICIOUS": "Flag for manual review before processing.",
        "NORMAL": "Approve transaction.",
    }

    return {
        "z_score": round(z_score, 4),
        "risk_score": risk_score,
        "status": status,
        "alert_triggered": alert_triggered,
        "alert_reason": alert_reason,
        "recommendation": recommendations[status],
    }


# ─── Endpoints ───────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {
        "service": "Fraud Detection API",
        "version": "2.0.0",
        "status": "running",
    }


@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.post("/predict", response_model=PredictionResponse)
def predict(txn: TransactionInput):
    """
    Score a single transaction and write the result to the PostgreSQL audit log.
    """
    result = compute_risk(txn.amount, txn.time_gap_minutes)

    # ── Attempt DB write (non-fatal: API still responds if DB is down) ──────
    try:
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST", "localhost"),
            port=os.getenv("DB_PORT", 5432),
            dbname=os.getenv("DB_NAME", "fraud_db"),
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASSWORD", "password"),
        )
        create_audit_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_logs
                    (transaction_id, amount, time_gap_minutes, z_score,
                     risk_score, status, alert_triggered, alert_reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
                (
                    txn.transaction_id,
                    txn.amount,
                    txn.time_gap_minutes,
                    result["z_score"],
                    result["risk_score"],
                    result["status"],
                    result["alert_triggered"],
                    result["alert_reason"],
                ),
            )
            conn.commit()
        conn.close()
        logger.info(f"Audit log written for transaction {txn.transaction_id}")
    except Exception as e:
        logger.warning(f"DB write skipped (DB may be offline): {e}")

    return PredictionResponse(
        transaction_id=txn.transaction_id,
        amount=txn.amount,
        **result,
    )


# ─── Batch predict ────────────────────────────────────────────────────────────
class BatchInput(BaseModel):
    transactions: List[TransactionInput] = Field(..., min_length=1, max_length=100)


@app.post("/batch-predict")
def batch_predict(batch: BatchInput):
    """
    Score up to 100 transactions in one request.
    Returns per-transaction results plus an aggregate summary.
    """
    if not batch.transactions:
        raise HTTPException(status_code=400, detail="Transaction list cannot be empty.")

    results = []
    for txn in batch.transactions:
        risk = compute_risk(txn.amount, txn.time_gap_minutes)
        results.append(
            {
                "transaction_id": txn.transaction_id,
                "amount": txn.amount,
                **risk,
            }
        )

    high_risk_count = sum(1 for r in results if r["status"] == "HIGH_RISK")
    suspicious_count = sum(1 for r in results if r["status"] == "SUSPICIOUS")
    normal_count = sum(1 for r in results if r["status"] == "NORMAL")
    avg_risk = round(sum(r["risk_score"] for r in results) / len(results), 2)

    return {
        "count": len(results),
        "results": results,
        "summary": {
            "high_risk_count": high_risk_count,
            "suspicious_count": suspicious_count,
            "normal_count": normal_count,
            "avg_risk_score": avg_risk,
            "alert_rate_pct": round(
                (high_risk_count + suspicious_count) / len(results) * 100, 1
            ),
        },
    }


@app.get("/audit-logs")
def get_audit_logs(status: Optional[str] = None, limit: int = 50):
    """
    Retrieve recent audit logs from PostgreSQL.
    Optionally filter by status (NORMAL | SUSPICIOUS | HIGH_RISK).
    Uses indexed columns for fast retrieval.
    """
    try:
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST", "localhost"),
            port=os.getenv("DB_PORT", 5432),
            dbname=os.getenv("DB_NAME", "fraud_db"),
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASSWORD", "password"),
        )
        create_audit_table(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if status:
                # Uses idx_audit_status index
                cur.execute(
                    "SELECT * FROM audit_logs WHERE status = %s "
                    "ORDER BY created_at DESC LIMIT %s",
                    (status.upper(), limit),
                )
            else:
                # Uses idx_audit_created_at index
                cur.execute(
                    "SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT %s",
                    (limit,),
                )
            rows = cur.fetchall()
        conn.close()
        return {"count": len(rows), "logs": rows}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {e}")