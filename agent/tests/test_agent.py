"""
agent/tests/test_agent.py
Unit tests for the incident response agent.
All external calls are mocked.
"""

import json
import os
import sqlite3
import sys
import tempfile
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Database tests ─────────────────────────────────────────────────────────

class TestDatabase:

    def setup_method(self):
        self.db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.db_file.name
        self.db_file.close()
        # Patch DB_PATH at the module level before each test
        import core.database as db_module
        db_module.DB_PATH = self.db_path

    def teardown_method(self):
        os.unlink(self.db_path)

    def test_init_db_creates_tables(self):
        from core.database import init_db
        init_db()
        conn = sqlite3.connect(self.db_path)
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        table_names = [t[0] for t in tables]
        assert "incidents" in table_names
        assert "actions" in table_names
        conn.close()

    def test_create_incident(self):
        from core.database import init_db, create_incident
        init_db()
        incident_id = create_incident(
            service="api-service",
            severity="critical",
            title="Test incident",
            raw_logs='["error log line"]'
        )
        assert incident_id == 1

    def test_create_multiple_incidents(self):
        from core.database import init_db, create_incident
        init_db()
        id1 = create_incident("api-service", "critical", "Incident 1", None)
        id2 = create_incident("worker-service", "warning", "Incident 2", None)
        assert id1 == 1
        assert id2 == 2

    def test_update_incident_diagnosis(self):
        from core.database import init_db, create_incident, update_incident
        init_db()
        incident_id = create_incident("api-service", "critical", "Test", None)
        update_incident(incident_id, diagnosis="Database connection timed out")
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT diagnosis FROM incidents WHERE id=?", (incident_id,)).fetchone()
        conn.close()
        assert row[0] == "Database connection timed out"

    def test_update_incident_status(self):
        from core.database import init_db, create_incident, update_incident
        init_db()
        incident_id = create_incident("api-service", "critical", "Test", None)
        update_incident(incident_id, status="remediated")
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT status FROM incidents WHERE id=?", (incident_id,)).fetchone()
        conn.close()
        assert row[0] == "remediated"

    def test_log_action(self):
        from core.database import init_db, create_incident, log_action
        init_db()
        incident_id = create_incident("api-service", "critical", "Test", None)
        log_action(
            incident_id=incident_id,
            action_type="restart_container",
            payload='{"service": "api-service"}',
            outcome="success",
        )
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT action_type, outcome FROM actions WHERE incident_id=?", (incident_id,)).fetchone()
        conn.close()
        assert row[0] == "restart_container"
        assert row[1] == "success"


# ── LLM client tests ───────────────────────────────────────────────────────

class TestLLMClient:

    def test_is_ollama_healthy_when_down(self):
        from core.llm_client import is_ollama_healthy
        with patch("requests.get", side_effect=Exception("Connection refused")):
            assert is_ollama_healthy() is False

    def test_is_ollama_healthy_when_up(self):
        from core.llm_client import is_ollama_healthy
        mock_response = MagicMock()
        mock_response.status_code = 200
        with patch("requests.get", return_value=mock_response):
            assert is_ollama_healthy() is True

    def test_diagnose_returns_none_when_ollama_down(self):
        from core.llm_client import diagnose
        with patch("core.llm_client.is_ollama_healthy", return_value=False):
            result = diagnose("api-service", "Service crashed", "[]")
            assert result is None

    def test_diagnose_parses_valid_response(self):
        from core.llm_client import diagnose
        valid_diagnosis = {
            "what_broke": "API service ran out of database connections",
            "why_it_broke": "Connection pool exhausted due to slow queries",
            "severity": "critical",
            "try_first": "Restart the api-service container",
            "confidence": "high"
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"response": json.dumps(valid_diagnosis)}
        with patch("core.llm_client.is_ollama_healthy", return_value=True):
            with patch("requests.post", return_value=mock_response):
                result = diagnose("api-service", "High error rate", '["error log"]')
        assert result is not None
        assert result["what_broke"] == "API service ran out of database connections"
        assert result["severity"] == "critical"

    def test_diagnose_handles_malformed_json(self):
        from core.llm_client import diagnose
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"response": "This is not JSON at all"}
        with patch("core.llm_client.is_ollama_healthy", return_value=True):
            with patch("requests.post", return_value=mock_response):
                result = diagnose("api-service", "Test", "[]")
        assert result is None

    def test_format_diagnosis_text(self):
        from core.llm_client import format_diagnosis_text
        diagnosis = {
            "what_broke": "API crashed",
            "why_it_broke": "OOM kill",
            "try_first": "Restart container",
            "severity": "critical",
            "confidence": "high"
        }
        text = format_diagnosis_text(diagnosis)
        assert "API crashed" in text
        assert "OOM kill" in text
        assert "Restart container" in text


# ── Remediation engine tests ───────────────────────────────────────────────

class TestRemediationEngine:

    def test_match_playbook_service_down(self):
        from remediation.engine import match_playbook
        playbook = match_playbook("Service api-service is down", "ServiceDown")
        assert playbook is not None
        assert any(s["action"] == "restart_container" for s in playbook)

    def test_match_playbook_disk_full(self):
        from remediation.engine import match_playbook
        playbook = match_playbook("Disk usage above 85%", "DiskAlmostFull")
        assert playbook is not None
        assert any(s["action"] == "clear_disk" for s in playbook)

    def test_match_playbook_high_memory(self):
        from remediation.engine import match_playbook
        playbook = match_playbook("High memory on worker-service", "HighMemory")
        assert playbook is not None
        assert any(s["action"] == "scale_down_and_restart" for s in playbook)

    def test_match_playbook_no_match(self):
        from remediation.engine import match_playbook
        playbook = match_playbook("Something totally unknown happened", None)
        assert playbook is None

    def test_match_playbook_title_keyword_fallback(self):
        from remediation.engine import match_playbook
        playbook = match_playbook("High error rate: 10 errors in 5 min", None)
        assert playbook is not None

    def test_run_playbook_no_match_returns_skipped(self):
        from remediation.engine import run_playbook
        result = run_playbook(
            incident_id=1,
            service="unknown-service",
            title="Something weird happened with no known fix",
            alert_name=None
        )
        assert result["outcome"] == "skipped"

    def test_restart_container_no_docker(self):
        from remediation.engine import restart_container
        with patch("remediation.engine._get_docker_client", return_value=None):
            result = restart_container("api-service")
        assert result["outcome"] == "failed"
        assert "Docker socket" in result["detail"]

    def test_human_size_formatting(self):
        from remediation.engine import _human_size
        assert _human_size(500) == "500.0 B"
        assert _human_size(1024) == "1.0 KB"
        assert _human_size(1024 * 1024) == "1.0 MB"
        assert _human_size(1024 * 1024 * 1024) == "1.0 GB"


# ── Notification tests ─────────────────────────────────────────────────────

class TestNotifications:

    def test_slack_not_configured_when_no_webhook(self):
        import notification.slack as slack_module
        original = slack_module.SLACK_WEBHOOK_URL
        slack_module.SLACK_WEBHOOK_URL = ""
        from notification.slack import is_configured
        assert is_configured() is False
        slack_module.SLACK_WEBHOOK_URL = original

    def test_slack_configured_when_webhook_set(self):
        import notification.slack as slack_module
        slack_module.SLACK_WEBHOOK_URL = "https://hooks.slack.com/test"
        from notification.slack import is_configured
        assert is_configured() is True
        slack_module.SLACK_WEBHOOK_URL = ""

    def test_slack_skips_when_not_configured(self):
        import notification.slack as slack_module
        slack_module.SLACK_WEBHOOK_URL = ""
        from notification.slack import send_incident_alert
        result = send_incident_alert(1, "api-service", "critical", "Test", "diagnosis", "success", "restarted")
        assert result is False

    def test_email_not_configured_when_missing_fields(self):
        import notification.email as email_module
        email_module.SMTP_USER = ""
        email_module.SMTP_PASSWORD = ""
        email_module.ALERT_TO = ""
        from notification.email import is_configured
        assert is_configured() is False

    def test_ticket_body_contains_key_fields(self):
        from notification.ticket import _build_ticket_body
        body = _build_ticket_body(
            incident_id=42,
            service="api-service",
            severity="critical",
            title="Database timeout",
            diagnosis="Connection pool exhausted",
            raw_logs='["error line 1"]',
            remediation_outcome="success",
            remediation_detail="Container restarted",
        )
        assert "42" in body
        assert "api-service" in body
        assert "CRITICAL" in body
        assert "Database timeout" in body
        assert "Connection pool exhausted" in body
        assert "Container restarted" in body