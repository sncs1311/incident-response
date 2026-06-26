"""
agent/notification/ticket.py

Creates a structured incident ticket with full context.
Stores in SQLite (always) and optionally posts to GitHub Issues.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime

import requests

logger = logging.getLogger("incident-agent.ticket")

DB_PATH          = os.environ.get("DB_PATH", "/app/data/incidents.db")
GITHUB_TOKEN     = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO      = os.environ.get("GITHUB_REPO", "")   # e.g. "yourname/incident-response"
GRAFANA_URL      = os.environ.get("GRAFANA_URL", "http://localhost:3000")


def create_ticket(
    incident_id: int,
    service: str,
    severity: str,
    title: str,
    diagnosis: str,
    raw_logs: str,
    remediation_outcome: str,
    remediation_detail: str,
) -> dict:
    """
    Build a full incident ticket and store it.
    Returns {"ticket_id": ..., "github_url": ...}
    """
    ticket_body = _build_ticket_body(
        incident_id=incident_id,
        service=service,
        severity=severity,
        title=title,
        diagnosis=diagnosis,
        raw_logs=raw_logs,
        remediation_outcome=remediation_outcome,
        remediation_detail=remediation_detail,
    )

    # Always store ticket text back onto the incident row
    _store_ticket_in_db(incident_id, ticket_body)

    result = {"ticket_id": incident_id, "github_url": None}

    # Optionally post to GitHub Issues
    if GITHUB_TOKEN and GITHUB_REPO:
        github_url = _post_to_github(
            title=f"[{severity.upper()}] {service}: {title}",
            body=ticket_body,
            labels=[severity, service, "incident"],
        )
        result["github_url"] = github_url

    return result


def _build_ticket_body(
    incident_id: int,
    service: str,
    severity: str,
    title: str,
    diagnosis: str,
    raw_logs: str,
    remediation_outcome: str,
    remediation_detail: str,
) -> str:
    """Build a markdown-formatted ticket body."""
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    # Parse raw logs for display
    try:
        logs_parsed = json.loads(raw_logs) if raw_logs else []
        if isinstance(logs_parsed, list):
            log_lines = "\n".join(str(l) for l in logs_parsed[:10])
        else:
            log_lines = json.dumps(logs_parsed, indent=2)[:1000]
    except Exception:
        log_lines = str(raw_logs)[:1000] if raw_logs else "No logs available"

    # Remediation section
    if remediation_outcome == "success":
        remediation_section = f"✅ **Auto-remediated**\n\n{remediation_detail}"
    elif remediation_outcome == "failed":
        remediation_section = f"❌ **Remediation failed** — human action required\n\n{remediation_detail}"
    else:
        remediation_section = "⚠️ **No playbook matched** — requires manual investigation"

    diagnosis_section = diagnosis if diagnosis else "_LLM diagnosis unavailable (Ollama offline)_"

    return f"""# Incident #{incident_id} — {title}

| Field | Value |
|---|---|
| **Incident ID** | #{incident_id} |
| **Service** | `{service}` |
| **Severity** | `{severity.upper()}` |
| **Detected at** | {timestamp} |
| **Grafana** | [View Dashboard]({GRAFANA_URL}/dashboards) |

---

## Diagnosis

{diagnosis_section}

---

## Remediation

{remediation_section}

---

## Raw Logs (sample)

```
{log_lines}
```

---

*Generated automatically by the Incident Response Agent*
"""


def _store_ticket_in_db(incident_id: int, ticket_body: str):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE incidents SET diagnosis = COALESCE(diagnosis, '') || ? WHERE id = ?",
            (f"\n\n---TICKET---\n{ticket_body}", incident_id)
        )
        conn.commit()
        conn.close()
        logger.info(f"Ticket stored in SQLite for incident #{incident_id}")
    except Exception as e:
        logger.error(f"Failed to store ticket in SQLite: {e}")


def _post_to_github(title: str, body: str, labels: list) -> str | None:
    """Post to GitHub Issues. Returns the issue URL or None."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return None

    url = f"https://api.github.com/repos/{GITHUB_REPO}/issues"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    payload = {
        "title": title,
        "body": body,
        "labels": [l for l in labels if l],
    }

    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        if r.status_code == 201:
            issue_url = r.json().get("html_url")
            logger.info(f"GitHub issue created: {issue_url}")
            return issue_url
        else:
            logger.warning(f"GitHub Issues returned HTTP {r.status_code}: {r.text}")
            return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to create GitHub issue: {e}")
        return None