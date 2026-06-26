"""
agent/core/prometheus_client.py

Thin wrapper around Prometheus's HTTP query API.
Uses PromQL (Prometheus Query Language) to fetch metrics.

Key concepts:
  Instant query  → value RIGHT NOW  (e.g. current CPU %)
  Range query    → values over time (e.g. CPU % over last 10 min)
  Alert query    → which alert rules are currently firing
"""

import os
import requests
from typing import Optional

PROM_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")


def instant_query(promql: str) -> list[dict]:
    """Run an instant PromQL query. Returns list of {metric: {labels}, value: float}."""
    resp = requests.get(
        f"{PROM_URL}/api/v1/query",
        params={"query": promql},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    results = []
    for r in data.get("data", {}).get("result", []):
        results.append({
            "metric": r["metric"],
            "value": float(r["value"][1]),
        })
    return results


def get_container_cpu(container_name: str) -> Optional[float]:
    """CPU usage % for a container over the last 2 minutes."""
    promql = (
        f'rate(container_cpu_usage_seconds_total{{name="{container_name}"}}[2m]) * 100'
    )
    results = instant_query(promql)
    return results[0]["value"] if results else None


def get_container_memory_mb(container_name: str) -> Optional[float]:
    """Memory usage in MB for a container."""
    promql = f'container_memory_usage_bytes{{name="{container_name}"}} / 1024 / 1024'
    results = instant_query(promql)
    return round(results[0]["value"], 1) if results else None


def get_disk_usage_pct() -> Optional[float]:
    """Root disk usage percentage on the host."""
    promql = (
        '(node_filesystem_size_bytes{mountpoint="/"} - node_filesystem_free_bytes{mountpoint="/"}) '
        '/ node_filesystem_size_bytes{mountpoint="/"} * 100'
    )
    results = instant_query(promql)
    return round(results[0]["value"], 1) if results else None


def get_firing_alerts() -> list[dict]:
    """
    Return all currently firing Prometheus alerts.
    Each dict: {name, severity, labels, annotations, state}
    """
    resp = requests.get(f"{PROM_URL}/api/v1/alerts", timeout=10)
    resp.raise_for_status()
    alerts = resp.json().get("data", {}).get("alerts", [])

    firing = []
    for a in alerts:
        if a.get("state") == "firing":
            firing.append({
                "name": a["labels"].get("alertname"),
                "severity": a["labels"].get("severity", "unknown"),
                "labels": a["labels"],
                "annotations": a.get("annotations", {}),
                "state": a["state"],
                "auto_remediate": a["labels"].get("auto_remediate") == "true",
                "remediation": a["labels"].get("remediation"),
            })
    return firing


def is_prometheus_healthy() -> bool:
    try:
        r = requests.get(f"{PROM_URL}/-/healthy", timeout=5)
        return r.status_code == 200
    except Exception:
        return False
