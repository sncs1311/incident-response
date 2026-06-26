# Automated Incident Response System

## Phase 1 — Foundation

Spins up a local observability stack and a Python agent that detects anomalies from logs and metrics.

### Stack
| Component | Role |
|-----------|------|
| Loki + Promtail | Log aggregation |
| Prometheus + cAdvisor + node-exporter | Metrics |
| Grafana | Dashboards |
| SQLite | Incident history |
| Python agent | Detection loop |

### Start everything

```bash
docker compose up --build -d
```

### Verify it's working

```bash
# Loki ready?
curl http://localhost:3100/ready

# Prometheus targets all UP?
open http://localhost:9090/targets

# Grafana (admin / admin)
open http://localhost:3000

# Watch the agent detect incidents
docker logs -f incident-agent
```

### Query incidents directly

```bash
sqlite3 data/incidents.db "SELECT id, service, severity, title, created_at FROM incidents ORDER BY created_at DESC LIMIT 10;"
```

### Generate test errors (trigger the agent)

```bash
# Spam the API to generate errors
for i in $(seq 1 50); do
  curl -s -X POST http://localhost:8000/process -H "Content-Type: application/json" -d '{}' &
done
wait
```

### Project structure

```
incident-response/
├── docker-compose.yml
├── monitoring/
│   ├── loki/
│   │   ├── loki-config.yml
│   │   └── promtail-config.yml
│   └── prometheus/
│       ├── prometheus.yml
│       └── alert_rules.yml
├── services/
│   ├── api-service/         # Flaky Flask API (generates errors)
│   └── worker-service/      # Background worker (fails every N jobs)
└── agent/
    ├── main.py              # Detection loop
    ├── core/
    │   ├── database.py      # SQLite incident store
    │   ├── loki_client.py   # LogQL queries
    │   └── prometheus_client.py  # PromQL queries
    └── Dockerfile
```

## Coming next

- **Phase 2** — Ollama + Llama 3 diagnosis: raw logs → plain-English explanation
- **Phase 3** — Auto-remediation: restart services, clear disk, scale containers
- **Phase 4** — Slack alerts + ticket creation with full context
