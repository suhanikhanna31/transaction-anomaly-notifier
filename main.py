from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List
import pandas as pd
import psycopg2
import psycopg2.extras
import os
import logging
from datetime import datetime

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── App ─────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Fraud Detection API",
    description="Real-time fraud detection with rule-based alerting and audit logging",
    version="2.0.0",
    # Swagger UI is available at /docs  ← this is the default, no extra config needed
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
                location         TEXT,
                device           TEXT,
                z_score          FLOAT,
                risk_score       INT,
                status           TEXT,
                confidence       FLOAT,
                alert_triggered  BOOLEAN,
                alert_reason     TEXT,
                created_at       TIMESTAMP DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_audit_status
                ON audit_logs(status);
            CREATE INDEX IF NOT EXISTS idx_audit_created_at
                ON audit_logs(created_at);
            CREATE INDEX IF NOT EXISTS idx_audit_transaction_id
                ON audit_logs(transaction_id);
        """)
    conn.commit()


# ─── Pydantic models ─────────────────────────────────────────────────────────

class TransactionInput(BaseModel):
    transaction_id: Optional[str] = None
    amount: float
    time_gap_minutes: Optional[float] = None
    # NEW: location and device fields
    location: Optional[str] = "Unknown"   # e.g. "Delhi", "Mumbai", "International"
    device: Optional[str] = "mobile"      # e.g. "mobile", "desktop", "new_device", "pos"


class PredictionResponse(BaseModel):
    transaction_id: Optional[str]
    amount: float
    location: Optional[str]
    device: Optional[str]
    z_score: float
    risk_score: int          # 0–100
    confidence: float        # 0.0–1.0  ← NEW
    status: str              # NORMAL | SUSPICIOUS | HIGH_RISK
    alert_triggered: bool
    alert_reason: Optional[str]
    recommendation: str


# ─── Rule-based alerting engine ──────────────────────────────────────────────

def compute_risk(
    amount: float,
    time_gap_minutes: Optional[float] = None,
    location: Optional[str] = "Unknown",
    device: Optional[str] = "mobile",
) -> dict:
    """
    Multi-factor rule engine.
    Rules:
      1. Z-score (statistical outlier)
      2. Absolute value threshold
      3. Velocity (time since last transaction)
      4. Location risk          ← NEW
      5. Device risk            ← NEW

    Returns z_score, risk_score (0-100), confidence, status, alert flag, reason.
    """
    z_score = (amount - mean_amount) / std_amount
    risk_score = 0
    reasons = []

    # ── Rule 1: statistical outlier ──────────────────────────────────────────
    if abs(z_score) > 3.0:
        risk_score += 50
        reasons.append(f"extreme z-score ({z_score:.2f})")
    elif abs(z_score) > 2.0:
        risk_score += 30
        reasons.append(f"high z-score ({z_score:.2f})")
    elif abs(z_score) > 1.5:
        risk_score += 15
        reasons.append(f"elevated z-score ({z_score:.2f})")

    # ── Rule 2: absolute high-value threshold ────────────────────────────────
    if amount > 10_000:
        risk_score += 25
        reasons.append("amount exceeds ₹10,000")
    elif amount > 5_000:
        risk_score += 10
        reasons.append("amount exceeds ₹5,000")

    # ── Rule 3: velocity check ───────────────────────────────────────────────
    if time_gap_minutes is not None:
        if time_gap_minutes < 1:
            risk_score += 25
            reasons.append(f"rapid succession ({time_gap_minutes:.1f} min gap)")
        elif time_gap_minutes < mean_time_gap * 0.3:
            risk_score += 10
            reasons.append(f"below-average time gap ({time_gap_minutes:.1f} min)")

    # ── Rule 4: location risk (NEW) ──────────────────────────────────────────
    if location:
        loc = location.strip().lower()
        if loc in ["unknown", ""]:
            risk_score += 20
            reasons.append("unknown location detected")
        elif loc == "international":
            risk_score += 15
            reasons.append("international transaction flagged")

    # ── Rule 5: device risk (NEW) ────────────────────────────────────────────
    if device:
        dev = device.strip().lower()
        if dev == "new_device":
            risk_score += 15
            reasons.append("new or unrecognized device")

    # ── Clamp & classify ─────────────────────────────────────────────────────
    risk_score = min(risk_score, 100)

    if risk_score >= 60:
        status = "HIGH_RISK"
    elif risk_score >= 30:
        status = "SUSPICIOUS"
    else:
        status = "NORMAL"

    # ── Confidence score (NEW) ───────────────────────────────────────────────
    # More rules fired = higher confidence in the prediction
    rule_count = len(reasons)
    if status == "HIGH_RISK":
        confidence = min(0.70 + (rule_count * 0.06), 0.99)
    elif status == "SUSPICIOUS":
        confidence = min(0.55 + (rule_count * 0.05), 0.85)
    else:
        confidence = max(0.90 - (rule_count * 0.05), 0.70)

    confidence = round(confidence, 2)

    # Alert fires when risk is high enough OR multiple rules triggered together
    alert_triggered = risk_score >= 60 or (risk_score >= 30 and len(reasons) >= 2)
    alert_reason = "; ".join(reasons) if reasons else None

    recommendations = {
        "HIGH_RISK":  "Block transaction and notify compliance team immediately.",
        "SUSPICIOUS": "Flag for manual review before processing.",
        "NORMAL":     "Approve transaction.",
    }

    return {
        "z_score":         round(z_score, 4),
        "risk_score":      risk_score,
        "confidence":      confidence,
        "status":          status,
        "alert_triggered": alert_triggered,
        "alert_reason":    alert_reason,
        "recommendation":  recommendations[status],
    }


# ─── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "service": "Fraud Detection API",
        "version": "2.0.0",
        "status":  "running",
        "docs":    "/docs",        # ← tells people where Swagger UI is
        "health":  "/health",
    }


@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.post("/predict", response_model=PredictionResponse)
def predict(txn: TransactionInput):
    """
    Score a single transaction for fraud risk.

    **Example request body:**
    ```json
    {
      "transaction_id": "txn_001",
      "amount": 14500,
      "time_gap_minutes": 0.3,
      "location": "International",
      "device": "new_device"
    }
    ```

    **Example response:**
    ```json
    {
      "status": "HIGH_RISK",
      "risk_score": 85,
      "confidence": 0.88,
      "alert_triggered": true
    }
    ```
    """
    result = compute_risk(
        txn.amount,
        txn.time_gap_minutes,
        txn.location,
        txn.device,
    )

    # ── Attempt DB write (non-fatal: API still responds if DB is down) ────────
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
                    (transaction_id, amount, time_gap_minutes, location, device,
                     z_score, risk_score, confidence, status, alert_triggered, alert_reason)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    txn.transaction_id,
                    txn.amount,
                    txn.time_gap_minutes,
                    txn.location,
                    txn.device,
                    result["z_score"],
                    result["risk_score"],
                    result["confidence"],
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
        location=txn.location,
        device=txn.device,
        **result,
    )


# ─── Batch predict ────────────────────────────────────────────────────────────

class BatchInput(BaseModel):
    transactions: List[TransactionInput] = Field(..., max_length=100)


@app.post("/batch-predict")
def batch_predict(batch: BatchInput):
    """
    Score up to 100 transactions in one request.
    Returns per-transaction results plus an aggregate summary.

    **Example request body:**
    ```json
    {
      "transactions": [
        {"transaction_id": "t1", "amount": 200, "time_gap_minutes": 30, "location": "Delhi", "device": "mobile"},
        {"transaction_id": "t2", "amount": 99000, "time_gap_minutes": 0.1, "location": "International", "device": "new_device"}
      ]
    }
    ```
    """
    if not batch.transactions:
        raise HTTPException(status_code=400, detail="Transaction list cannot be empty.")

    results = []
    for txn in batch.transactions:
        risk = compute_risk(txn.amount, txn.time_gap_minutes, txn.location, txn.device)
        results.append({
            "transaction_id": txn.transaction_id,
            "amount":         txn.amount,
            "location":       txn.location,
            "device":         txn.device,
            **risk,
        })

    high_risk_count  = sum(1 for r in results if r["status"] == "HIGH_RISK")
    suspicious_count = sum(1 for r in results if r["status"] == "SUSPICIOUS")
    normal_count     = sum(1 for r in results if r["status"] == "NORMAL")
    avg_risk         = round(sum(r["risk_score"] for r in results) / len(results), 2)

    return {
        "count":   len(results),
        "results": results,
        "summary": {
            "high_risk_count":  high_risk_count,
            "suspicious_count": suspicious_count,
            "normal_count":     normal_count,
            "avg_risk_score":   avg_risk,
            "alert_rate_pct":   round(
                (high_risk_count + suspicious_count) / len(results) * 100, 1
            ),
        },
    }


# ─── Audit logs ──────────────────────────────────────────────────────────────

@app.get("/audit-logs")
def get_audit_logs(status: Optional[str] = None, limit: int = 50):
    """
    Retrieve recent audit logs from PostgreSQL.
    Optionally filter by status: NORMAL | SUSPICIOUS | HIGH_RISK
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
                cur.execute(
                    "SELECT * FROM audit_logs WHERE status = %s "
                    "ORDER BY created_at DESC LIMIT %s",
                    (status.upper(), limit),
                )
            else:
                cur.execute(
                    "SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT %s",
                    (limit,),
                )
            rows = cur.fetchall()
        conn.close()
        return {"count": len(rows), "logs": rows}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database unavailable: {e}")
