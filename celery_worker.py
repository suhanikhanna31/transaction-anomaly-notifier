"""
celery_worker.py
────────────────
Async alert dispatcher using Celery + Redis.

When /predict flags a HIGH_RISK or SUSPICIOUS transaction, the API enqueues
a task here instead of blocking the HTTP response on slow I/O (email, Slack).

Architecture:
    FastAPI  ──enqueue──▶  Redis (broker)  ──consume──▶  Celery worker
                                                               │
                                          ┌────────────────────┤
                                          ▼                    ▼
                                    Email (SMTP)         Slack webhook

Start the worker:
    celery -A celery_worker worker --loglevel=info --queues=alerts
"""

import os
import smtplib
import logging
import requests
from email.mime.text import MIMEText
from celery import Celery
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ── Broker / backend ──────────────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "fraud_alerts",
    broker=REDIS_URL,
    backend=REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Kolkata",          # Pine Labs is based in India
    task_routes={"celery_worker.*": {"queue": "alerts"}},
    task_acks_late=True,              # re-queue if worker crashes mid-task
    worker_prefetch_multiplier=1,     # one task at a time per worker process
)


# ── Email alert ───────────────────────────────────────────────────────────────
def _send_email(subject: str, body: str) -> bool:
    """
    Send an email via SMTP.  Reads credentials from .env:
        SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, ALERT_EMAIL_TO
    Returns True on success, False on failure (non-fatal).
    """
    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", 587))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")
    to_addr   = os.getenv("ALERT_EMAIL_TO")

    if not all([smtp_host, smtp_user, smtp_pass, to_addr]):
        logger.warning("Email not configured — skipping email alert.")
        return False

    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"]    = smtp_user
    msg["To"]      = to_addr

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        logger.info(f"Email alert sent to {to_addr}")
        return True
    except Exception as e:
        logger.error(f"Email send failed: {e}")
        return False


# ── Slack alert ───────────────────────────────────────────────────────────────
def _send_slack(message: str) -> bool:
    """
    Post a message to a Slack channel via Incoming Webhook.
    Set SLACK_WEBHOOK_URL in .env.
    """
    webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        logger.warning("SLACK_WEBHOOK_URL not set — skipping Slack alert.")
        return False

    try:
        resp = requests.post(webhook_url, json={"text": message}, timeout=5)
        resp.raise_for_status()
        logger.info("Slack alert sent.")
        return True
    except Exception as e:
        logger.error(f"Slack send failed: {e}")
        return False


# ── Celery task ───────────────────────────────────────────────────────────────
@celery_app.task(
    name="celery_worker.dispatch_alert",
    bind=True,
    max_retries=3,
    default_retry_delay=10,   # seconds between retries
)
def dispatch_alert(self, transaction_id: str, amount: float, status: str,
                   risk_score: int, alert_reason: str):
    """
    Async task: send email + Slack notification for flagged transactions.
    Retried up to 3 times with exponential back-off if network errors occur.
    """
    subject = f"[FRAUD ALERT] {status} transaction detected — {transaction_id}"
    body = (
        f"Transaction ID : {transaction_id}\n"
        f"Amount         : ₹{amount:,.2f}\n"
        f"Status         : {status}\n"
        f"Risk Score     : {risk_score}/100\n"
        f"Reason         : {alert_reason}\n\n"
        f"Please review immediately in the audit dashboard."
    )

    slack_msg = (
        f":rotating_light: *{status} ALERT* | `{transaction_id}`\n"
        f"Amount: ₹{amount:,.2f} | Risk: {risk_score}/100\n"
        f"_{alert_reason}_"
    )

    try:
        _send_email(subject, body)
        _send_slack(slack_msg)
    except Exception as exc:
        logger.error(f"Alert dispatch failed, retrying: {exc}")
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)

    return {"sent": True, "transaction_id": transaction_id}
