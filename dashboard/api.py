"""
dashboard/api.py

Thin FastAPI backend that exposes:
  GET  /api/services     — service health from Prometheus
  GET  /api/metrics      — CPU, memory, error rate
  GET  /api/incidents    — incident history from SQLite
  GET  /api/logs         — recent error logs from Loki
  GET  /api/alerts       — firing alerts from Prometheus
  POST /api/simulate     — trigger a self-healing or non-healing error
"""

import json
import os
import sqlite3
import time
from typing import Optional

import docker
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="Incident Response Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
LOKI_URL       = os.environ.get("LOKI_URL", "http://loki:3100")
DB_PATH        = os.environ.get("DB_PATH", "/app/data/incidents.db")


# ── Helpers ────────────────────────────────────────────────────────────────

async def prom_query(query: str) -> list:
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query})
        data = r.json()
        return data.get("data", {}).get("result", [])


async def loki_query(query: str, limit: int = 50, minutes_back: int = 5) -> list:
    start = int((time.time() - minutes_back * 60) * 1e9)
    end   = int(time.time() * 1e9)
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{LOKI_URL}/loki/api/v1/query_range",
            params={"query": query, "limit": limit, "start": start, "end": end, "direction": "backward"}
        )
        return r.json().get("data", {}).get("result", [])


def get_db():
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Routes ─────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/services")
async def get_services():
    """Service UP/DOWN status."""
    try:
        results = await prom_query('up{job=~"api-service|cadvisor"}')
        services = []
        for r in results:
            services.append({
                "name": r["metric"].get("job", r["metric"].get("instance", "unknown")),
                "status": "up" if r["value"][1] == "1" else "down",
                "value": r["value"][1],
            })

        # Also check via Docker
        try:
            client = docker.from_env()
            for container in client.containers.list(all=True):
                name = container.name
                if name in ["api-service", "worker-service"]:
                    already = any(s["name"] == name for s in services)
                    if not already:
                        services.append({
                            "name": name,
                            "status": "up" if container.status == "running" else "down",
                            "value": "1" if container.status == "running" else "0",
                        })
        except Exception:
            pass

        return {"services": services}
    except Exception as e:
        return {"services": [], "error": str(e)}


@app.get("/api/metrics")
async def get_metrics():
    """CPU, memory, error rate, request rate."""
    try:
        cpu_results = await prom_query(
            'rate(container_cpu_usage_seconds_total{name=~"api-service|worker-service"}[2m]) * 100'
        )
        mem_results = await prom_query(
            'container_memory_usage_bytes{name=~"api-service|worker-service"}'
        )
        err_results = await prom_query('rate(api_errors_total[1m])')
        req_results = await prom_query('rate(api_requests_total[1m])')

        return {
            "cpu": [{"name": r["metric"].get("name", "unknown"), "value": float(r["value"][1])} for r in cpu_results],
            "memory": [{"name": r["metric"].get("name", "unknown"), "value": int(float(r["value"][1]))} for r in mem_results],
            "errors": [{"type": r["metric"].get("error_type", "unknown"), "rate": float(r["value"][1])} for r in err_results],
            "requests": [{"status": r["metric"].get("status", "?"), "rate": float(r["value"][1])} for r in req_results],
        }
    except Exception as e:
        return {"cpu": [], "memory": [], "errors": [], "requests": [], "error": str(e)}


@app.get("/api/alerts")
async def get_alerts():
    """Firing Prometheus alerts."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{PROMETHEUS_URL}/api/v1/alerts")
            alerts = r.json().get("data", {}).get("alerts", [])
            firing = [a for a in alerts if a.get("state") == "firing"]
            return {"alerts": firing, "count": len(firing)}
    except Exception as e:
        return {"alerts": [], "count": 0, "error": str(e)}


@app.get("/api/logs")
async def get_logs(level: str = "ERROR", limit: int = 30):
    """Recent error logs from Loki."""
    try:
        query = f'{{job="docker", level="{level}"}}'
        streams = await loki_query(query, limit=limit, minutes_back=60)
        logs = []
        for stream in streams:
            labels = stream.get("stream", {})
            for ts, line in stream.get("values", []):
                try:
                    parsed = json.loads(line)
                    message = parsed.get("message", parsed.get("msg", line))
                    svc = parsed.get("service", labels.get("service", "unknown"))
                    lvl = parsed.get("level", labels.get("level", level))
                except Exception:
                    message = line
                    svc = labels.get("service", "unknown")
                    lvl = labels.get("level", level)

                logs.append({
                    "timestamp": int(ts) // 1_000_000,  # ns to ms
                    "service": svc,
                    "level": lvl,
                    "message": message,
                })

        logs.sort(key=lambda x: x["timestamp"], reverse=True)
        return {"logs": logs[:limit]}
    except Exception as e:
        return {"logs": [], "error": str(e)}


@app.get("/api/incidents")
async def get_incidents(limit: int = 20):
    """Incident history from SQLite."""
    conn = get_db()
    if not conn:
        return {"incidents": [], "stats": {"total": 0, "remediated": 0, "escalated": 0}}

    try:
        rows = conn.execute(
            "SELECT * FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()

        incidents = []
        for row in rows:
            row = dict(row)
            # Get actions for this incident
            actions = conn.execute(
                "SELECT * FROM actions WHERE incident_id = ? ORDER BY created_at DESC",
                (row["id"],)
            ).fetchall()
            row["actions"] = [dict(a) for a in actions]
            incidents.append(row)

        stats = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'remediated' THEN 1 ELSE 0 END) as remediated,
                SUM(CASE WHEN status = 'escalated' THEN 1 ELSE 0 END) as escalated,
                SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) as open
            FROM incidents
        """).fetchone()

        conn.close()
        return {
            "incidents": incidents,
            "stats": dict(stats) if stats else {"total": 0, "remediated": 0, "escalated": 0},
        }
    except Exception as e:
        conn.close()
        return {"incidents": [], "stats": {}, "error": str(e)}


class SimulateRequest(BaseModel):
    type: str  # "self_healing" or "non_healing"


@app.post("/api/simulate")
async def simulate_incident(req: SimulateRequest):
    """Trigger a simulated incident."""
    if req.type == "self_healing":
        # Stop worker-service — agent will detect and restart it
        try:
            client = docker.from_env()
            container = client.containers.get("worker-service")
            container.stop(timeout=5)
            return {
                "ok": True,
                "message": "worker-service stopped. The agent will detect this as a ServiceDown alert and restart it automatically within 60 seconds.",
                "type": "self_healing",
            }
        except docker.errors.NotFound:
            raise HTTPException(status_code=404, detail="worker-service container not found")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    elif req.type == "non_healing":
        # Flood the API with errors — no playbook will match, agent will escalate
        try:
            errors_sent = 0
            async with httpx.AsyncClient(timeout=30) as client:
                for i in range(20):
                    try:
                        await client.post(
                            "http://api-service:8000/process",
                            content="malformed-payload",
                            headers={"Content-Type": "application/json"},
                        )
                        errors_sent += 1
                    except Exception:
                        errors_sent += 1

            return {
                "ok": True,
                "message": f"Sent {errors_sent} error requests to api-service. The agent will detect high error rate and escalate — no auto-fix for this pattern.",
                "type": "non_healing",
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    else:
        raise HTTPException(status_code=400, detail="type must be 'self_healing' or 'non_healing'")


# ── Serve frontend ─────────────────────────────────────────────────────────

app.mount("/", StaticFiles(directory="/app/static", html=True), name="static")