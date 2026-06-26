"""
agent/remediation/engine.py

Phase 3: Auto-remediation engine.
Matches an incident to a playbook and attempts a fix.
Each playbook returns {"outcome": "success|failed|skipped", "detail": "..."}
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
from typing import Optional

import docker
import requests

logger = logging.getLogger("incident-agent.remediation")

DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "unix://var/run/docker.sock")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")

# How many times we'll attempt to restart a container before giving up
MAX_RESTART_ATTEMPTS = int(os.environ.get("MAX_RESTART_ATTEMPTS", "2"))


# ── Docker client ──────────────────────────────────────────────────────────

def _get_docker_client():
    try:
        return docker.from_env()
    except Exception as e:
        logger.error(f"Cannot connect to Docker socket: {e}")
        return None


# ── Playbooks ──────────────────────────────────────────────────────────────

def restart_container(service: str) -> dict:
    """
    Restart a Docker container by service name.
    Used for: ServiceDown, high error rate, OOM crashes.
    """
    client = _get_docker_client()
    if not client:
        return {"outcome": "failed", "detail": "Docker socket unavailable"}

    # Try exact name first, then partial match
    try:
        containers = client.containers.list(all=True)
        target = None
        for c in containers:
            if c.name == service or service in c.name:
                target = c
                break

        if not target:
            return {"outcome": "skipped", "detail": f"No container found matching '{service}'"}

        previous_status = target.status
        logger.info(f"Restarting container '{target.name}' (current status: {previous_status})")

        target.restart(timeout=30)

        # Verify it came back up
        target.reload()
        new_status = target.status

        if new_status == "running":
            return {
                "outcome": "success",
                "detail": f"Container '{target.name}' restarted successfully ({previous_status} → running)"
            }
        else:
            return {
                "outcome": "failed",
                "detail": f"Container '{target.name}' restarted but status is '{new_status}'"
            }

    except docker.errors.APIError as e:
        return {"outcome": "failed", "detail": f"Docker API error: {e}"}


def clear_disk(target_paths: Optional[list] = None) -> dict:
    """
    Free up disk space by clearing common cache and temp directories.
    Used for: DiskAlmostFull alert.
    """
    if target_paths is None:
        target_paths = [
            "/tmp",
            "/var/log/journal",        # systemd journal logs
            "/root/.cache",
            "/home/*/.cache",
        ]

    freed_bytes = 0
    cleaned = []
    errors = []

    for path in target_paths:
        import glob
        expanded = glob.glob(path)
        for p in expanded:
            if not os.path.exists(p):
                continue
            try:
                before = _get_dir_size(p)
                # Remove contents but not the directory itself
                for item in os.listdir(p):
                    item_path = os.path.join(p, item)
                    try:
                        if os.path.isfile(item_path) or os.path.islink(item_path):
                            os.unlink(item_path)
                        elif os.path.isdir(item_path):
                            shutil.rmtree(item_path)
                    except Exception as e:
                        errors.append(f"{item_path}: {e}")
                after = _get_dir_size(p)
                freed = before - after
                freed_bytes += freed
                cleaned.append(f"{p} (freed {_human_size(freed)})")
            except Exception as e:
                errors.append(f"{p}: {e}")

    detail = f"Freed {_human_size(freed_bytes)} from: {', '.join(cleaned)}"
    if errors:
        detail += f" | Errors: {'; '.join(errors)}"

    outcome = "success" if freed_bytes > 0 else "skipped"
    return {"outcome": outcome, "detail": detail}


def scale_down_and_restart(service: str) -> dict:
    """
    For OOM or high memory: stop the container, wait, restart it.
    Gives the OS time to reclaim memory before bringing the service back.
    """
    import time

    client = _get_docker_client()
    if not client:
        return {"outcome": "failed", "detail": "Docker socket unavailable"}

    try:
        containers = client.containers.list(all=True)
        target = next((c for c in containers if service in c.name), None)

        if not target:
            return {"outcome": "skipped", "detail": f"No container found matching '{service}'"}

        logger.info(f"Stopping '{target.name}' for memory recovery...")
        target.stop(timeout=15)

        import time
        time.sleep(5)  # Brief pause for memory reclaim

        logger.info(f"Restarting '{target.name}'...")
        target.start()
        target.reload()

        return {
            "outcome": "success" if target.status == "running" else "failed",
            "detail": f"Stop-wait-start cycle complete. Status: {target.status}"
        }

    except docker.errors.APIError as e:
        return {"outcome": "failed", "detail": f"Docker API error: {e}"}


def run_health_check(service: str) -> dict:
    """
    Hit the service's /health endpoint and report back.
    Used as a verification step after other remediation actions.
    """
    # Map service names to their internal URLs
    health_urls = {
        "api-service": "http://api-service:8000/health",
        "worker-service": "http://worker-service:8001/health",
    }

    url = health_urls.get(service)
    if not url:
        return {"outcome": "skipped", "detail": f"No health URL configured for '{service}'"}

    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            return {"outcome": "success", "detail": f"Health check passed: {r.json()}"}
        else:
            return {"outcome": "failed", "detail": f"Health check returned HTTP {r.status_code}"}
    except requests.exceptions.RequestException as e:
        return {"outcome": "failed", "detail": f"Health check unreachable: {e}"}


# ── Playbook matching ──────────────────────────────────────────────────────

# Maps alert names and patterns to a list of remediation steps.
# Steps are tried in order; if one succeeds we stop.
PLAYBOOKS = {
    "ServiceDown": [
        {"action": "restart_container", "description": "Restart the crashed container"},
    ],
    "HighMemory": [
        {"action": "scale_down_and_restart", "description": "Stop, wait for memory reclaim, restart"},
    ],
    "DiskAlmostFull": [
        {"action": "clear_disk", "description": "Clear temp files and caches"},
    ],
    "HighCPU": [
        {"action": "restart_container", "description": "Restart container to reset runaway process"},
    ],
    # Pattern match fallback for log-based incidents
    "high_error_rate": [
        {"action": "restart_container", "description": "High error rate — restart service"},
    ],
}


def match_playbook(title: str, alert_name: str = None) -> Optional[list]:
    """Find the right playbook for this incident."""
    # Try exact alert name first
    if alert_name and alert_name in PLAYBOOKS:
        return PLAYBOOKS[alert_name]

    # Try matching title keywords
    title_lower = title.lower()
    if "down" in title_lower or "not running" in title_lower:
        return PLAYBOOKS["ServiceDown"]
    if "memory" in title_lower or "oom" in title_lower:
        return PLAYBOOKS["HighMemory"]
    if "disk" in title_lower or "storage" in title_lower:
        return PLAYBOOKS["DiskAlmostFull"]
    if "cpu" in title_lower:
        return PLAYBOOKS["HighCPU"]
    if "error rate" in title_lower:
        return PLAYBOOKS["high_error_rate"]

    return None


def run_playbook(incident_id: int, service: str, title: str, alert_name: str = None) -> dict:
    """
    Main entry point. Match a playbook and execute it.
    Returns a summary of what was attempted and the outcome.
    """
    playbook = match_playbook(title, alert_name)

    if not playbook:
        logger.info(f"Incident #{incident_id}: no playbook matched for '{title}' — escalating to human")
        return {
            "outcome": "skipped",
            "detail": "No matching playbook — requires human intervention",
            "action": "none"
        }

    for step in playbook:
        action = step["action"]
        logger.info(f"Incident #{incident_id}: attempting '{action}' — {step['description']}")

        if action == "restart_container":
            result = restart_container(service)
        elif action == "clear_disk":
            result = clear_disk()
        elif action == "scale_down_and_restart":
            result = scale_down_and_restart(service)
        else:
            result = {"outcome": "skipped", "detail": f"Unknown action: {action}"}

        result["action"] = action
        logger.info(f"Incident #{incident_id}: '{action}' → {result['outcome']}: {result['detail']}")

        if result["outcome"] == "success":
            return result

    # All steps failed
    return {"outcome": "failed", "detail": "All playbook steps failed", "action": action}


# ── Helpers ────────────────────────────────────────────────────────────────

def _get_dir_size(path: str) -> int:
    total = 0
    try:
        for dirpath, _, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    total += os.path.getsize(fp)
                except Exception:
                    pass
    except Exception:
        pass
    return total


def _human_size(size_bytes: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"