"""
agent/notification/email.py

Sends incident alerts via email using SMTP.
Works with Gmail, Outlook, or any SMTP server.
"""

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger("incident-agent.email")

SMTP_HOST     = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
ALERT_FROM    = os.environ.get("ALERT_FROM", SMTP_USER)
ALERT_TO      = os.environ.get("ALERT_TO", "")       # Comma-separated list of recipients
GRAFANA_URL   = os.environ.get("GRAFANA_URL", "http://localhost:3000")


def is_configured() -> bool:
    return all([SMTP_USER, SMTP_PASSWORD, ALERT_TO])


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
    Send an incident alert email.
    Returns True if sent successfully.
    """
    if not is_configured():
        logger.info("Email not configured (SMTP_USER/SMTP_PASSWORD/ALERT_TO missing) — skipping")
        return False

    severity_upper = severity.upper()
    subject = f"[{severity_upper}] Incident #{incident_id} — {service}: {title}"

    # Remediation status
    if remediation_outcome == "success":
        remediation_line = f"✅ Auto-remediated: {remediation_detail}"
        action_needed = "No immediate action required — please verify service is stable."
    elif remediation_outcome == "failed":
        remediation_line = f"❌ Remediation failed: {remediation_detail}"
        action_needed = "⚠️ HUMAN ACTION REQUIRED — automated fix did not work."
    else:
        remediation_line = "⚠️ No playbook matched — escalated to on-call."
        action_needed = "⚠️ HUMAN ACTION REQUIRED — no automated fix available."

    diagnosis_text = diagnosis if diagnosis else "No diagnosis available (Ollama offline)."

    html_body = f"""
    <html><body style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
      <div style="background: {'#ff4444' if severity == 'critical' else '#ffaa00'}; 
                  color: white; padding: 16px; border-radius: 8px 8px 0 0;">
        <h2 style="margin:0">Incident #{incident_id} — {severity_upper}</h2>
        <p style="margin:4px 0 0">{title}</p>
      </div>
      <div style="border: 1px solid #ddd; border-top: none; padding: 16px; border-radius: 0 0 8px 8px;">
        <table style="width:100%; border-collapse: collapse;">
          <tr>
            <td style="padding: 8px; font-weight: bold; width: 30%;">Service</td>
            <td style="padding: 8px;"><code>{service}</code></td>
          </tr>
          <tr style="background: #f9f9f9;">
            <td style="padding: 8px; font-weight: bold;">Severity</td>
            <td style="padding: 8px;">{severity_upper}</td>
          </tr>
        </table>

        <h3 style="margin-top: 20px;">Diagnosis</h3>
        <pre style="background: #f4f4f4; padding: 12px; border-radius: 4px; 
                    white-space: pre-wrap; font-size: 13px;">{diagnosis_text}</pre>

        <h3>Remediation</h3>
        <p>{remediation_line}</p>
        <p style="font-weight: bold;">{action_needed}</p>

        <div style="margin-top: 24px;">
          <a href="{GRAFANA_URL}/dashboards" 
             style="background: #f46800; color: white; padding: 10px 20px; 
                    text-decoration: none; border-radius: 4px; margin-right: 8px;">
            View Grafana
          </a>
          <a href="{GRAFANA_URL}/explore" 
             style="background: #444; color: white; padding: 10px 20px; 
                    text-decoration: none; border-radius: 4px;">
            View Logs
          </a>
        </div>
      </div>
    </body></html>
    """

    text_body = f"""
INCIDENT #{incident_id} [{severity_upper}]
Service: {service}
Title: {title}

DIAGNOSIS:
{diagnosis_text}

REMEDIATION:
{remediation_line}

{action_needed}

Grafana: {GRAFANA_URL}/dashboards
    """.strip()

    recipients = [r.strip() for r in ALERT_TO.split(",") if r.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = ALERT_FROM
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(ALERT_FROM, recipients, msg.as_string())
        logger.info(f"Email sent for incident #{incident_id} to {recipients}")
        return True
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False