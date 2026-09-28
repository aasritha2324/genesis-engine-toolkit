# AdVanta — local distributed stack

A self-contained Docker deployment of the click-fraud analytics platform:
ingestion API, Kafka pipeline, aggregator workers, PostgreSQL, Redis,
Nginx load balancer, Prometheus and Grafana. Mirrors the hosted preview's
schema and behaviour (deduplication, event-time lateness, fraud rules,
tenant isolation) with the API's own JWT auth.

```
                       ┌──────────────┐
  advertiser / ───────▶│    nginx     │ :8080  (LB, retries, X-Served-By)
  load test  │         └──────┬───────┘  ┌────────────┐
             │                │ split    │  frontend  │  static dashboard
             │                ▼          └────────────┘
             │        ┌───────────────┐  (scaled N: --scale api=3)
             │        │  api replicas │──▶ Kafka (ad-clicks, 6 parts)
             │        │  /clicks 202  │──▶ Postgres (users, campaigns, ads, keys)
             │        └───────────────┘
             │                │ async
             │                ▼
             │        ┌───────────────────┐ (scaled N: --scale aggregator=3)
             │        │ aggregator workers│ validate → dedup → lateness →
             │        │ (consumer group)  │ 1-min aggregation → fraud rules
             │        └───────┬───────────┘
             │        poison →▼            └─▶ Redis hot counters, heartbeats
             │        ┌──────┐   ┌──────────────────────┐
             └─X──────│ DLQ  │   │ Postgres = truth     │
                      └──────┘   │ (click_events,       │
                                 │  aggregated_clicks,  │
                                 │  suspicious_clicks)  │
                                 └──────────────────────┘

  Prometheus ──scrapes── api :8000/metrics · aggregator :9100/metrics ·
                        kafka-exporter :9308 · cadvisor :8080
  Grafana    ──reads── Prometheus  → dashboards/advanta-overview.json
```

## Quick start

```bash
cp deploy/.env.example deploy/.env      # then set CHANGE_ME values
cd deploy && docker compose up --build -d --scale api=3 --scale aggregator=3

curl -s http://localhost:8080/ready | jq          # all three deps healthy
open http://localhost:8080                        # dashboard UI (register → login)
open http://localhost:3000                        # Grafana (admin / GRAFANA_ADMIN_PASSWORD)
```

First run creates the schema via `db/migrate.sh` (additive, idempotent) and the
Kafka topics (`ad-clicks` with 6 partitions + `ad-clicks-dlq`).

## Ports

| Service | Host port | Notes |
| --- | --- | --- |
| nginx | 8080 | everything user-facing; API behind LB |
| Grafana | 3000 | provisioned AdVanta dashboard |
| Prometheus | 9090 | raw metrics & targets |

API and aggregator `/metrics` are scraped by Prometheus directly (DNS service
discovery) and deliberately NOT reachable through nginx.

## Scaling

- `--scale api=N` — nginx re-resolves `api` DNS every 5 s; new replicas join the
  LB automatically. State (Kafka producer, Postgres pool) is per-replica.
- `--scale aggregator=N` — consumer-group rebalancing splits the 6 `ad-clicks`
  partitions; N > 6 workers sit idle. Offsets are committed only after the
  Postgres transaction, so crash → seek-and-retry keeps counting effectively-once.
- Kafka is a single KRaft broker (demo-sized); retention 48 h.

## Ingestion contract

`POST /clicks` (or `/clicks/batch` with `{"events":[…]}`, ≤500) with `X-API-Key`.
The API resolves ad → campaign → advertiser server-side, HMACs viewer/IP, and
publishes to Kafka; 202 means accepted-and-queued. On Kafka failure it returns
503 with **"Events were NOT accepted; retry later"** — clients may safely retry.
Responses carry `X-Served-By` (replica name) for the demo.

## Demo flow (7–10 minutes)

1. **Show the UI**: register two advertisers. Both see empty dashboards —
   tenants are isolated from zero (integration test `test_tenant_isolation`).
2. **Mint a key** in advertiser A's UI; point the built-in simulator at it.
3. **Watch the dashboard**: per-minute series, countries, devices, top ads.
   Redis hot counters make "clicks this minute" move before Postgres aggregates.
4. **Duplicate immunity**: re-send the same `event_id` — counts don't move;
   `duplicates_dropped_total` climbs instead.
5. **Fraud rules**: hammer one viewer ≥50×/min → `viewer_repeat`; ≥100 clicks
   from one IP in 10 s → `ip_burst`; a burst to 5× the trailing average with
   ≥200 clicks → `campaign_spike`. Flags appear under Suspicious; events are
   still counted (flag, don't drop).
6. **Failure demo**: `docker compose stop kafka` → API returns 503 (no lost
   events, no 200s), dashboard shows degraded infra. Restart kafka → ingestion
   resumes automatically.
7. **Scale demo**: `--scale api=3` mid-run; `X-Served-By` rotates across
   replicas; `--scale aggregator=2` splits partitions, lag drains.

## Monitoring

Grafana dashboard **AdVanta — Pipeline & Infrastructure** covers:
business (ingest rate, unique viewers, rejected reasons), API (RPS by status,
latency p50/p95/p99, Kafka publish time), workers (consumer lag per partition,
batch size/time, duplicates by layer, late/discarded/DLQ, fraud by rule), and
infrastructure (broker count, topic partitions, container CPU/memory via
cAdvisor).

## Testing

```bash
# Pure-rule unit tests (no stack needed)
cd deploy/tests && pip install pytest && pytest test_processing.py -q

# Integration tests (stack must be running)
pip install requests && ADVANTA_BASE_URL=http://localhost:8080 pytest -q
```

Covers: auth, ingestion (single/batch/malformed), dedup across layers,
event-time lateness (late / too_old / future→DLQ), fraud rules, tenant
isolation, LB distribution across replicas.

## Load testing

See `loadtest/README.md` — 100/500/1000-user Locust profiles with ~5%
deliberate duplicate `event_id`s.

## Hosted preview vs this stack

| | Hosted preview (this repo's app) | Docker stack |
| --- | --- | --- |
| Auth | Lovable Cloud (Supabase) sessions | Local JWT (bcrypt) |
| Pipeline | Transactional DB function, per-event | Kafka + worker batches |
| Fraud | DB rules via `private.*` functions | Same thresholds in workers |
| Storage | Managed cloud database | Postgres + Redis in compose |
