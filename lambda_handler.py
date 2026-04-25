"""
lambda_handler.py
─────────────────
AWS Lambda entry point. API Gateway routes POST /predict to this function.

Deploy steps (quick reference — see README for full walkthrough):
  1. zip -r function.zip . (from inside fraud_detection_api/)
  2. Upload to Lambda in AWS Console
  3. Set environment variables (DB_HOST, DB_NAME, DB_USER, DB_PASSWORD)
  4. Attach API Gateway trigger with route POST /predict
"""

import json
import pandas as pd

# ── Load baseline stats once (Lambda keeps the container warm between calls) ──
df = pd.read_csv("transactions.csv")
mean_amount = df["amount"].mean()
std_amount = df["amount"].std()
mean_time_gap = df["time_gap_minutes"].mean()


def compute_risk(amount: float, time_gap_minutes=None) -> dict:
    """Identical rule engine to main.py — kept in sync manually."""
    z_score = (amount - mean_amount) / std_amount
    risk_score = 0
    reasons = []

    if abs(z_score) > 3.0:
        risk_score += 50
        reasons.append(f"extreme z-score ({z_score:.2f})")
    elif abs(z_score) > 2.0:
        risk_score += 30
        reasons.append(f"high z-score ({z_score:.2f})")
    elif abs(z_score) > 1.5:
        risk_score += 15
        reasons.append(f"elevated z-score ({z_score:.2f})")

    if amount > 10_000:
        risk_score += 25
        reasons.append("amount exceeds $10,000")
    elif amount > 5_000:
        risk_score += 10
        reasons.append("amount exceeds $5,000")

    if time_gap_minutes is not None:
        if time_gap_minutes < 1:
            risk_score += 25
            reasons.append(f"rapid succession ({time_gap_minutes:.1f} min gap)")
        elif time_gap_minutes < mean_time_gap * 0.3:
            risk_score += 10
            reasons.append(f"below-average time gap ({time_gap_minutes:.1f} min)")

    risk_score = min(risk_score, 100)

    if risk_score >= 60:
        status = "HIGH_RISK"
    elif risk_score >= 30:
        status = "SUSPICIOUS"
    else:
        status = "NORMAL"

    alert_triggered = risk_score >= 60 or (risk_score >= 30 and len(reasons) >= 2)
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
        "alert_reason": "; ".join(reasons) if reasons else None,
        "recommendation": recommendations[status],
    }


def handler(event, context):
    """
    AWS Lambda handler.
    Expects API Gateway proxy event with JSON body:
        { "transaction_id": "...", "amount": 5000, "time_gap_minutes": 0.5 }
    """
    try:
        body = json.loads(event.get("body", "{}"))
        amount = float(body["amount"])
        time_gap_minutes = body.get("time_gap_minutes")
        transaction_id = body.get("transaction_id")
    except (KeyError, ValueError) as e:
        return {"statusCode": 400, "body": json.dumps({"error": f"Bad request: {e}"})}

    result = compute_risk(amount, time_gap_minutes)
    result["transaction_id"] = transaction_id
    result["amount"] = amount

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(result),
    }
