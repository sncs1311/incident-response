"""
agent/notification/slack.py

Sends incident alerts to a Slack channel via webhook.
Each message includes: severity, service, diagnosis, action taken, and a link to Grafana.
"""

import json
import logging
import os
import requests

logger = logging.getLogger("incident-agent.slack")

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://localhost:3000")

# Emoji + color per severity
SEVERITY_STYLE = {
    "critical": {"emoji": "🔴", "color": "#FF0000"},
    "warning":  {"emoji": "🟡", "color": "#FFA500"},
    "info":     {"emoji": "🔵", "color": "#0000FF"},
}


def is_configured() -> bool:
    return bool(SLACK_WEBHOOK_URL)


def send_incident_alert(
    incident_id: int,
    service: str,
    severity: str,
    title: str,
    diagnosis: str,
    remediation_outcome: str,
    remediation_detail: str,
) -> bool:
    """
    Post an incident card to Slack.
    Returns True if sent successfully.
    """
    if not is_configured():
        logger.info("SLACK_WEBHOOK_URL not set — skipping Slack notification")
        return False

    style = SEVERITY_STYLE.get(severity, SEVERITY_STYLE["warning"])
    emoji = style["emoji"]
    color = style["color"]

    # Build remediation status line
    if remediation_outcome == "success":
        remediation_line = f"✅ *Auto-remediated:* {remediation_detail}"
    elif remediation_outcome == "failed":
        remediation_line = f"❌ *Remediation failed:* {remediation_detail} — *human action needed*"
    else:
        remediation_line = f"⚠️ *Escalated:* No playbook matched — *human action needed*"

    # Parse diagnosis into readable lines
    diagnosis_block = diagnosis if diagnosis else "_No diagnosis available (Ollama offline)_"

    payload = {
        "attachments": [
            {
                "color": color,
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": f"{emoji} Incident #{incident_id} — {title}"
                        }
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Service:*\n`{service}`"},
                            {"type": "mrkdwn", "text": f"*Severity:*\n`{severity.upper()}`"},
                        ]
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Diagnosis:*\n```{diagnosis_block}```"
                        }
                    },
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": remediation_line}
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "View Grafana"},
                                "url": f"{GRAFANA_URL}/dashboards"
                            },
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "View Logs"},
                                "url": f"{GRAFANA_URL}/explore?orgId=1&left=%5B%22now-1h%22,%22now%22,%22Loki%22,%7B%22expr%22:%22%7Bservice%3D%5C%22{service}%5C%22%7D%22%7D%5D"
                            }
                        ]
                    }
                ]
            }
        ]
    }

    try:
        r = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        if r.status_code == 200:
            logger.info(f"Slack notification sent for incident #{incident_id}")
            return True
        else:
            logger.warning(f"Slack returned HTTP {r.status_code}: {r.text}")
            return False
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to send Slack notification: {e}")
        return False