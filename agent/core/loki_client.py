"""
agent/core/loki_client.py

Thin wrapper around Loki's HTTP query API.
Loki uses LogQL — a query language similar to PromQL but for logs.

Key LogQL patterns used here:
  {service="api-service"}                        → all logs from api-service
  {service="api-service"} |= "ERROR"             → filter to lines containing ERROR
  {service="api-service"} | json | level="error" → parse JSON and filter by field
"""

import os
import requests
import time
from datetime import datetime, timedelta
from typing import Optional


LOKI_URL = os.environ.get("LOKI_URL", "http://loki:3100")


def query_range(
    logql: str,
    minutes_back: int = 5,
    limit: int = 100,
) -> list[dict]:
    """
    Run a LogQL range query and return log entries as a list of dicts.

    Each entry: {"timestamp": "...", "line": "...", "service": "..."}
    """
    end = int(time.time() * 1e9)                             # nanoseconds
    start = int((time.time() - minutes_back * 60) * 1e9)

    resp = requests.get(
        f"{LOKI_URL}/loki/api/v1/query_range",
        params={
            "query": logql,
            "start": start,
            "end": end,
            "limit": limit,
            "direction": "backward",   # Most recent first
        },
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    entries = []
    for stream in data.get("data", {}).get("result", []):
        labels = stream.get("stream", {})
        for ts_ns, line in stream.get("values", []):
            entries.append({
                "timestamp": datetime.utcfromtimestamp(int(ts_ns) / 1e9).isoformat(),
                "line": line,
                "service": labels.get("service", "unknown"),
                "level": labels.get("level", ""),
            })

    return entries


def get_error_logs(service: str, minutes_back: int = 5) -> list[dict]:
    """Fetch ERROR and CRITICAL logs for a specific service."""
    logql = f'{{service="{service}"}} | json | level=~"error|critical|ERROR|CRITICAL"'
    return query_range(logql, minutes_back=minutes_back)


def get_all_errors(minutes_back: int = 5) -> list[dict]:
    """Fetch errors across all monitored services."""
    logql = '{job="docker"} | json | level=~"error|critical|ERROR|CRITICAL"'
    return query_range(logql, minutes_back=minutes_back)


def count_errors(service: str, minutes_back: int = 5) -> int:
    """Return count of error log lines in the time window."""
    return len(get_error_logs(service, minutes_back))


def is_loki_healthy() -> bool:
    try:
        r = requests.get(f"{LOKI_URL}/ready", timeout=5)
        return r.status_code == 200
    except Exception:
        return False
