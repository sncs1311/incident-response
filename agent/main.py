"""
agent/main.py

Phase 4: Detection + Diagnosis + Remediation + Notify + Ticket
Full pipeline. Every incident is detected, diagnosed, auto-fixed if possible,
ticketed, and the right human is notified.
"""

import json
import logging
import os
import time
from datetime import datetime

from core.database import init_db, create_incident, update_incident, log_action
from core.loki_client import get_all_errors, is_loki_healthy
from core.prometheus_client import get_firing_alerts, is_prometheus_healthy
from core.llm_client import diagnose, format_diagnosis_text, is_ollama_healthy, model_is_available
from remediation.engine import run_playbook
from notification.slack import send_incident_alert as slack_alert, is_configured as slack_configured
from notification.email import send_incident_alert as email_alert, is_configured as email_configured
from notification.ticket import create_ticket

logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}'
)
logger = logging.getLogger("incident-agent")

POLL_INTERVAL   = int(os.environ.get("POLL_INTERVAL", "30"))
ERROR_THRESHOLD = int(os.environ.get("ERROR_THRESHOLD", "5"))
AUTO_REMEDIATE  = os.environ.get("AUTO_REMEDIATE", "true").lower() == "true"

# Only notify on these severities (info is too noisy)
NOTIFY_SEVERITIES = {"critical", "warning"}

_recent_incident_keys: set[str] = set()


def check_log_anomalies() -> list[dict]:
    incidents = []
    try:
        errors = get_all_errors(minutes_back=5)
    except Exception as e:
        logger.warning(f"Could not query Loki: {e}")
        return incidents

    by_service: dict[str, list] = {}
    for entry in errors:
        svc = entry.get("service", "unknown")
        by_service.setdefault(svc, []).append(entry)

    for service, entries in by_service.items():
        if len(entries) >= ERROR_THRESHOLD:
            key = f"log_errors:{service}:{datetime.utcnow().strftime('%Y-%m-%dT%H:%M')}"
            if key not in _recent_incident_keys:
                _recent_incident_keys.add(key)
                incidents.append({
                    "service": service,
                    "severity": "warning",
                    "title": f"High error rate: {len(entries)} errors in 5 min",
                    "raw_logs": json.dumps([e["line"] for e in entries[:20]]),
                    "alert_name": None,
                })
    return incidents


def check_prometheus_alerts() -> list[dict]:
    incidents = []
    try:
        alerts = get_firing_alerts()
    except Exception as e:
        logger.warning(f"Could not query Prometheus: {e}")
        return incidents

    for alert in alerts:
        service = alert["labels"].get("name", alert["labels"].get("job", "host"))
        key = f"alert:{alert['name']}:{service}"
        if key not in _recent_incident_keys:
            _recent_incident_keys.add(key)
            incidents.append({
                "service": service,
                "severity": alert["severity"],
                "title": alert["annotations"].get("summary", alert["name"]),
                "raw_logs": json.dumps(alert),
                "alert_name": alert["name"],
            })
    return incidents


def run_detection_cycle():
    logger.info("Running detection cycle")
    all_incidents = check_log_anomalies() + check_prometheus_alerts()

    for inc in all_incidents:

        # ── Phase 2: LLM Diagnosis ──────────────────────────────
        diagnosis = None
        diagnosis_text = None

        if is_ollama_healthy():
            diagnosis = diagnose(
                service=inc["service"],
                title=inc["title"],
                raw_logs=inc.get("raw_logs", ""),
            )
            if diagnosis:
                diagnosis_text = format_diagnosis_text(diagnosis)
                if diagnosis.get("severity") == "critical":
                    inc["severity"] = "critical"
        else:
            logger.warning("Ollama unavailable — skipping diagnosis")

        # ── Write to SQLite ─────────────────────────────────────
        incident_id = create_incident(
            service=inc["service"],
            severity=inc["severity"],
            title=inc["title"],
            raw_logs=inc.get("raw_logs"),
        )
        if diagnosis_text:
            update_incident(incident_id, diagnosis=diagnosis_text)

        logger.info(
            f"Incident #{incident_id} opened: [{inc['severity']}] "
            f"{inc['service']} — {inc['title']}"
        )

        # ── Phase 3: Auto-Remediation ───────────────────────────
        remediation_outcome = "skipped"
        remediation_detail = "Auto-remediation disabled"

        if AUTO_REMEDIATE:
            try:
                result = run_playbook(
                    incident_id=incident_id,
                    service=inc["service"],
                    title=inc["title"],
                    alert_name=inc.get("alert_name"),
                )
                remediation_outcome = result["outcome"]
                remediation_detail = result["detail"]

                log_action(
                    incident_id=incident_id,
                    action_type=result.get("action", "none"),
                    payload=json.dumps({"service": inc["service"], "title": inc["title"]}),
                    outcome=remediation_outcome,
                    error=remediation_detail if remediation_outcome == "failed" else None,
                )

                if remediation_outcome == "success":
                    update_incident(incident_id, status="remediated")
                else:
                    update_incident(incident_id, status="escalated")

                logger.info(f"Incident #{incident_id} remediation: {remediation_outcome} — {remediation_detail}")

            except Exception as e:
                logger.error(f"Remediation error for incident #{incident_id}: {e}", exc_info=True)
                remediation_outcome = "failed"
                remediation_detail = str(e)
                update_incident(incident_id, status="escalated")

        # ── Phase 4: Ticket + Notifications ────────────────────
        try:
            ticket = create_ticket(
                incident_id=incident_id,
                service=inc["service"],
                severity=inc["severity"],
                title=inc["title"],
                diagnosis=diagnosis_text,
                raw_logs=inc.get("raw_logs", ""),
                remediation_outcome=remediation_outcome,
                remediation_detail=remediation_detail,
            )
            if ticket.get("github_url"):
                logger.info(f"Incident #{incident_id} GitHub issue: {ticket['github_url']}")
        except Exception as e:
            logger.error(f"Ticket creation failed for incident #{incident_id}: {e}")

        # Only notify for warning and critical
        if inc["severity"] in NOTIFY_SEVERITIES:
            notify_kwargs = dict(
                incident_id=incident_id,
                service=inc["service"],
                severity=inc["severity"],
                title=inc["title"],
                diagnosis=diagnosis_text,
                remediation_outcome=remediation_outcome,
                remediation_detail=remediation_detail,
            )
            if slack_configured():
                slack_alert(**notify_kwargs)
            if email_configured():
                email_alert(**notify_kwargs)

            if not slack_configured() and not email_configured():
                logger.warning(
                    f"Incident #{incident_id} needs human attention but no notification channel configured. "
                    "Set SLACK_WEBHOOK_URL or SMTP_USER/SMTP_PASSWORD/ALERT_TO in docker-compose.yml"
                )

    if not all_incidents:
        logger.info("No incidents detected")


def wait_for_dependencies():
    logger.info("Waiting for Loki and Prometheus...")
    while True:
        loki_ok = is_loki_healthy()
        prom_ok = is_prometheus_healthy()
        if loki_ok and prom_ok:
            logger.info("Dependencies ready")
            return
        logger.info(f"Not ready — loki={loki_ok} prometheus={prom_ok} — retrying in 5s")
        time.sleep(5)


def check_ollama_on_startup():
    if not is_ollama_healthy():
        logger.warning("Ollama not running — diagnosis will be skipped")
        return
    if not model_is_available():
        logger.warning("Ollama running but llama3 not pulled — run: ollama pull llama3")
    else:
        logger.info("Ollama + Llama 3 ready")


def log_startup_config():
    logger.info(f"Auto-remediate: {AUTO_REMEDIATE}")
    logger.info(f"Slack configured: {slack_configured()}")
    logger.info(f"Email configured: {email_configured()}")
    logger.info(f"Poll interval: {POLL_INTERVAL}s | Error threshold: {ERROR_THRESHOLD}")


def main():
    logger.info("Incident response agent starting — Phase 4 (full pipeline)")
    init_db()
    wait_for_dependencies()
    check_ollama_on_startup()
    log_startup_config()

    while True:
        try:
            run_detection_cycle()
        except Exception as e:
            logger.error(f"Detection cycle failed: {e}", exc_info=True)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()