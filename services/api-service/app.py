"""
services/api-service/app.py

A deliberately flaky Flask API.
It randomly raises errors, logs them in a structured way, and
exposes /metrics for Prometheus — exactly the kind of service
the incident response system is designed to monitor.
"""

import json
import logging
import os
import random
import time
from datetime import datetime

from flask import Flask, jsonify, request
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

# ── Structured JSON logging ─────────────────────────────────────────────────
# This is critical: logs must be structured so Promtail and our LLM can parse them.
# A single-line JSON object per log entry >> unstructured text.

class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "service": "api-service",
            "message": record.getMessage(),
            "logger": record.name,
        }
        # Include exception info if present
        if record.exc_info:
            log_entry["error"] = self.formatException(record.exc_info)
        if hasattr(record, "extra"):
            log_entry.update(record.extra)
        return json.dumps(log_entry)


logging.basicConfig(level=logging.INFO)
for handler in logging.root.handlers:
    handler.setFormatter(JSONFormatter())

logger = logging.getLogger("api-service")

# ── App setup ──────────────────────────────────────────────────────────────

app = Flask(__name__)
CRASH_RATE = float(os.environ.get("CRASH_RATE", "0.1"))  # 10% failure by default

# ── Prometheus metrics ─────────────────────────────────────────────────────
# These are the metrics Prometheus will scrape from /metrics

request_count = Counter(
    "api_requests_total",
    "Total API requests",
    ["method", "endpoint", "status"]
)
request_latency = Histogram(
    "api_request_duration_seconds",
    "API request duration",
    ["endpoint"]
)
error_count = Counter(
    "api_errors_total",
    "Total API errors",
    ["error_type"]
)

# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "api-service"})


@app.route("/metrics")
def metrics():
    """Prometheus scrapes this endpoint every 15s."""
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


@app.route("/process", methods=["POST"])
def process():
    """
    Simulates a data processing endpoint.
    Randomly fails based on CRASH_RATE to generate realistic error logs.
    """
    start = time.time()
    endpoint = "/process"

    # Simulate random failure types
    if random.random() < CRASH_RATE:
        failure_type = random.choice([
            "database_timeout",
            "memory_exceeded",
            "upstream_service_unavailable",
            "invalid_payload_schema",
        ])

        error_count.labels(error_type=failure_type).inc()
        request_count.labels(method="POST", endpoint=endpoint, status="500").inc()

        logger.error(
            "Request processing failed",
            extra={
                "error_type": failure_type,
                "request_id": request.headers.get("X-Request-ID", "unknown"),
                "duration_ms": round((time.time() - start) * 1000, 2),
            }
        )

        return jsonify({"error": failure_type}), 500

    # Simulate successful processing with some latency
    time.sleep(random.uniform(0.05, 0.3))
    request_count.labels(method="POST", endpoint=endpoint, status="200").inc()
    request_latency.labels(endpoint=endpoint).observe(time.time() - start)

    logger.info(
        "Request processed successfully",
        extra={
            "request_id": request.headers.get("X-Request-ID", "unknown"),
            "duration_ms": round((time.time() - start) * 1000, 2),
        }
    )

    return jsonify({"status": "processed"})


if __name__ == "__main__":
    logger.info("API service starting", extra={"port": 8000, "crash_rate": CRASH_RATE})
    app.run(host="0.0.0.0", port=8000)
