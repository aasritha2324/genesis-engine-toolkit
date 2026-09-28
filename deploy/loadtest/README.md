# AdVanta load tests (Locust)

Profiles from the demo plan: **100 → 500 → 1000 users**, 3-minute runs,
30-second ramp-up, ~5% deliberate duplicate `event_id`s.

## Prerequisites

The Docker stack is running and healthy:

```bash
docker compose -f deploy/docker-compose.yml up --build -d --scale api=3 --scale aggregator=3
curl -s http://localhost:8080/ready | jq
```

Install Locust locally (it is not part of the stack):

```bash
pip install locust
```

## Runs

```bash
# Warm-up: 100 users, ramp 30s, 3 minutes
locust -f deploy/loadtest/locustfile.py --host http://localhost:8080 \
  --users 100 --spawn-rate 20 --run-time 3m --csv loadtest/results_100

# Peak: 500 users
locust -f deploy/loadtest/locustfile.py --host http://localhost:8080 \
  --users 500 --spawn-rate 50 --run-time 3m --csv loadtest/results_500

# Stress: 1000 users
locust -f deploy/loadtest/locustfile.py --host http://localhost:8080 \
  --users 1000 --spawn-rate 100 --run-time 3m --csv loadtest/results_1000
```

While each run is going, watch:

```bash
open http://localhost:3000        # Grafana (admin / $GRAFANA_ADMIN_PASSWORD)
open http://localhost:9090        # Prometheus
```

## What to observe

| Signal | Where | Healthy during load |
| --- | --- | --- |
| Ingest p95 latency | Grafana → API p95 (`http_request_seconds`) | < 100 ms |
| 202 rate ≈ request rate | `clicks_ingested_total` rate | flat, no drop |
| Kafka consumer lag | `kafka_consumergroup_lag` | rises briefly, drains after ramp |
| Batch time | `batch_seconds` p95 | < 1 s at 500 users |
| Duplicate drops | `duplicates_dropped_total{layer}` | grows on the ~5% dupes |
| No DLQ traffic | `dlq_events_total` | 0 |
| Error budget | `errors_total` | 0 |

## Reusing an existing account / key

```bash
LOADTEST_EMAIL=me@example.com LOADTEST_PASSWORD=... \
LOADTEST_API_KEY=ck_... locust -f deploy/loadtest/locustfile.py --host http://localhost:8080 \
  --users 500 --spawn-rate 50 --run-time 3m
```

If no key is supplied, one is created for the advertiser at test start
(dashboard → "shown once" key); you can revoke it afterwards in the UI.
