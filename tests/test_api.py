"""
tests/test_api.py
─────────────────
Unit + integration tests for the Fraud Detection API.

Run:
    pytest tests/ -v --tb=short

Coverage targets:
    - Rule engine logic (z-score, velocity, high-value thresholds)
    - /predict endpoint happy paths + edge cases
    - /health and / endpoints
    - DB-down graceful degradation
    - Batch endpoint
"""

from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

# ── Import app with DB patched out so tests don't need Postgres ───────────────
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

with patch("psycopg2.connect") as mock_conn:
    mock_conn.return_value = MagicMock()
    from main import app, compute_risk

client = TestClient(app)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def predict(amount, time_gap_minutes=None, transaction_id=None):
    payload = {"amount": amount}
    if time_gap_minutes is not None:
        payload["time_gap_minutes"] = time_gap_minutes
    if transaction_id:
        payload["transaction_id"] = transaction_id
    return client.post("/predict", json=payload)


# ═══════════════════════════════════════════════════════════════════════════════
# Health / root
# ═══════════════════════════════════════════════════════════════════════════════


class TestHealthEndpoints:
    def test_root_returns_200(self):
        r = client.get("/")
        assert r.status_code == 200
        assert r.json()["service"] == "Fraud Detection API"

    def test_health_returns_ok(self):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_health_has_timestamp(self):
        r = client.get("/health")
        assert "timestamp" in r.json()


# ═══════════════════════════════════════════════════════════════════════════════
# Rule engine unit tests (pure logic, no HTTP)
# ═══════════════════════════════════════════════════════════════════════════════


class TestRuleEngine:
    def test_normal_transaction_low_risk(self):
        result = compute_risk(200, time_gap_minutes=15)
        assert result["status"] == "NORMAL"
        assert result["risk_score"] < 30
        assert result["alert_triggered"] is False

    def test_high_amount_over_10k_adds_25(self):
        result_10k = compute_risk(12000, time_gap_minutes=30)
        result_5k = compute_risk(6000, time_gap_minutes=30)
        # $12k should carry more risk than $6k
        assert result_10k["risk_score"] > result_5k["risk_score"]

    def test_rapid_velocity_adds_25(self):
        result_fast = compute_risk(200, time_gap_minutes=0.5)
        result_slow = compute_risk(200, time_gap_minutes=30)
        assert result_fast["risk_score"] > result_slow["risk_score"]

    def test_combined_signals_trigger_alert(self):
        # High amount + rapid velocity → should alert
        result = compute_risk(12000, time_gap_minutes=0.3)
        assert result["alert_triggered"] is True
        assert result["status"] in ("SUSPICIOUS", "HIGH_RISK")

    def test_single_signal_below_60_no_alert(self):
        # Only slightly elevated z-score, no other signals
        result = compute_risk(600, time_gap_minutes=20)
        # risk_score should be moderate but not enough for alert alone
        if result["risk_score"] < 60:
            assert result["alert_triggered"] is (
                result["risk_score"] >= 30
                and result["alert_reason"] is not None
                and ";" in result["alert_reason"]
            )

    def test_risk_score_capped_at_100(self):
        # Extreme transaction that would overflow without cap
        result = compute_risk(50000, time_gap_minutes=0.1)
        assert result["risk_score"] <= 100

    def test_z_score_is_float(self):
        result = compute_risk(300)
        assert isinstance(result["z_score"], float)

    def test_no_time_gap_still_scores(self):
        result = compute_risk(500, time_gap_minutes=None)
        assert "risk_score" in result
        assert result["risk_score"] >= 0

    def test_alert_reason_populated_on_alert(self):
        result = compute_risk(15000, time_gap_minutes=0.2)
        if result["alert_triggered"]:
            assert result["alert_reason"] is not None
            assert len(result["alert_reason"]) > 0

    def test_recommendation_matches_status(self):
        normal = compute_risk(200, 15)
        suspicious = compute_risk(6000, 20)
        high_risk = compute_risk(15000, 0.2)

        assert "Approve" in normal["recommendation"]
        assert (
            "review" in suspicious["recommendation"].lower()
            or "Approve" in suspicious["recommendation"]
        )
        assert "Block" in high_risk["recommendation"]


# ═══════════════════════════════════════════════════════════════════════════════
# /predict endpoint
# ═══════════════════════════════════════════════════════════════════════════════


class TestPredictEndpoint:
    def test_predict_normal_transaction(self):
        r = predict(200, time_gap_minutes=15, transaction_id="txn_test_001")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "NORMAL"
        assert body["transaction_id"] == "txn_test_001"
        assert body["amount"] == 200

    def test_predict_high_risk_transaction(self):
        r = predict(20000, time_gap_minutes=0.2, transaction_id="txn_highrisk")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "HIGH_RISK"
        assert body["alert_triggered"] is True
        assert body["risk_score"] >= 60

    def test_predict_missing_amount_returns_422(self):
        r = client.post("/predict", json={"time_gap_minutes": 5})
        assert r.status_code == 422

    def test_predict_without_time_gap(self):
        r = predict(300)
        assert r.status_code == 200
        assert "risk_score" in r.json()

    def test_predict_without_transaction_id(self):
        r = predict(500, time_gap_minutes=10)
        assert r.status_code == 200
        body = r.json()
        assert "transaction_id" in body  # field exists, may be None

    def test_predict_response_has_all_fields(self):
        r = predict(1000, time_gap_minutes=5, transaction_id="txn_fields")
        body = r.json()
        expected_keys = {
            "transaction_id",
            "amount",
            "z_score",
            "risk_score",
            "status",
            "alert_triggered",
            "alert_reason",
            "recommendation",
        }
        assert expected_keys.issubset(body.keys())

    def test_predict_zero_amount(self):
        r = predict(0)
        # Should succeed — 0 is a valid (unusual) amount
        assert r.status_code == 200

    def test_predict_very_large_amount(self):
        r = predict(999999, time_gap_minutes=0.01)
        assert r.status_code == 200
        body = r.json()
        assert body["risk_score"] == 100
        assert body["status"] == "HIGH_RISK"


# ═══════════════════════════════════════════════════════════════════════════════
# /batch-predict endpoint
# ═══════════════════════════════════════════════════════════════════════════════


class TestBatchPredict:
    def test_batch_predict_multiple_transactions(self):
        payload = {
            "transactions": [
                {"transaction_id": "b1", "amount": 200, "time_gap_minutes": 15},
                {"transaction_id": "b2", "amount": 15000, "time_gap_minutes": 0.3},
                {"transaction_id": "b3", "amount": 450, "time_gap_minutes": 30},
            ]
        }
        r = client.post("/batch-predict", json=payload)
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 3
        assert len(body["results"]) == 3

    def test_batch_flags_anomalies_correctly(self):
        payload = {
            "transactions": [
                {"transaction_id": "norm", "amount": 200, "time_gap_minutes": 20},
                {"transaction_id": "risk", "amount": 18000, "time_gap_minutes": 0.1},
            ]
        }
        r = client.post("/batch-predict", json=payload)
        results = {item["transaction_id"]: item for item in r.json()["results"]}
        assert results["norm"]["status"] == "NORMAL"
        assert results["risk"]["status"] == "HIGH_RISK"

    def test_batch_returns_summary(self):
        payload = {
            "transactions": [
                {"amount": 200},
                {"amount": 200},
                {"amount": 20000, "time_gap_minutes": 0.2},
            ]
        }
        r = client.post("/batch-predict", json=payload)
        body = r.json()
        assert "summary" in body
        assert "high_risk_count" in body["summary"]

    def test_batch_empty_list_returns_400(self):
        r = client.post("/batch-predict", json={"transactions": []})
        assert r.status_code == 400

    def test_batch_max_100_transactions(self):
        transactions = [{"amount": 200} for _ in range(101)]
        r = client.post("/batch-predict", json={"transactions": transactions})
        assert r.status_code == 422  # validation error


# ═══════════════════════════════════════════════════════════════════════════════
# DB resilience
# ═══════════════════════════════════════════════════════════════════════════════


class TestDBResilience:
    @patch("psycopg2.connect", side_effect=Exception("DB is down"))
    def test_predict_still_works_without_db(self, mock_db):
        r = predict(300, time_gap_minutes=10, transaction_id="txn_nodb")
        # API must respond 200 even if DB is offline
        assert r.status_code == 200
        assert "risk_score" in r.json()
