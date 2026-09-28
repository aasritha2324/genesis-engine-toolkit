"""
Locust load test for the AdVanta ingestion pipeline.

Sends single click events to POST /clicks with a real X-API-Key. Roughly 5%
of events deliberately reuse an event_id from an earlier request so the
duplicate-handling path (accepted vs. deduplicated downstream) is exercised
under load, not just the happy path.

Run (with the Docker stack up on localhost:8080):

  pip install locust
  python deploy/loadtest/locustfile.py --help   # optional: module mode

  # Scenarios (see deploy/loadtest/README.md):
  locust -f deploy/loadtest/locustfile.py \
    --host http://localhost:8080 --users 100 --spawn-rate 20 --run-time 3m
  #   500 users: --users 500 --spawn-rate 50
  #   1000 users: --users 1000 --spawn-rate 100

Environment variables (all optional; a default demo advertiser is created):
  LOADTEST_HOST        default http://localhost:8080
  LOADTEST_API_KEY     existing ingest key; one is created otherwise
  LOADTEST_EMAIL       existing account email; registered otherwise
  LOADTEST_PASSWORD    existing/new account password (default loadtest-pass-1)
"""
import os
import random
import secrets
import time
import uuid

from locust import HttpUser, between, events, task

PASSWORD = os.environ.get("LOADTEST_PASSWORD", "loadtest-pass-1")
EMAIL = os.environ.get("LOADTEST_EMAIL", "loadtest@advanta.local")
DUPLICATE_RATIO = 0.05  # ~5% of events reuse an event_id

_state = {"setup_done": False, "api_key": os.environ.get("LOADTEST_API_KEY"),
          "ad_ids": [], "campaign_id": None, "reusable_ids": []}


def _host_base(user: "IngestUser") -> str:
    return user.host


def _ensure_setup(client):
    """Idempotent: pick or create an advertiser, ad and ingest key. Runs once."""
    if _state["setup_done"]:
        return
    r = client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD},
                    name="setup: login")
    token = r.json().get("token") if r.ok else None
    if not token:
        r = client.post("/auth/register", name="setup: register",
                        json={"email": EMAIL, "password": PASSWORD,
                              "organization": "Loadtest Org"})
        if r.status_code == 409:  # org taken by a parallel run
            r = client.post("/auth/login", json={"email": EMAIL, "password": PASSWORD},
                            name="setup: login after 409")
        token = r.json().get("token")
    assert token, f"setup failed: {r.status_code} {r.text[:200]}"
    auth = {"Authorization": f"Bearer {token}"}

    ads = client.get("/api/ads", headers=auth, name="setup: list ads").json()
    if ads:
        _state["ad_ids"] = [a["id"] for a in ads]
        _state["campaign_id"] = ads[0]["campaign_id"]
    else:
        camps = client.get("/api/campaigns", headers=auth, name="setup: list campaigns").json()
        if camps:
            _state["campaign_id"] = camps[0]["id"]
        else:
            r = client.post("/api/campaigns", headers=auth, name="setup: create campaign",
                            json={"name": "Loadtest Campaign"})
            _state["campaign_id"] = r.json()["id"]
        r = client.post("/api/ads", headers=auth, name="setup: create ad",
                        json={"campaign_id": _state["campaign_id"], "title": "Loadtest Ad"})
        _state["ad_ids"] = [r.json()["id"]]

    if not _state["api_key"]:
        r = client.post("/api/ingest-keys", headers=auth, name="setup: create key",
                        json={"label": "locust"})
        _state["api_key"] = r.json()["key"]
    _state["headers"] = {"X-API-Key": _state["api_key"], "Content-Type": "application/json"}
    _state["read_headers"] = {"Authorization": f"Bearer {token}"}
    _state["setup_done"] = True


@events.test_start.add_listener
def reset_state(environment, **_):
    _state["setup_done"] = False


class IngestUser(HttpUser):
    wait_time = between(0.05, 0.2)
    weight = 10

    def on_start(self):
        _ensure_setup(self.client)

    @task(20)
    def click(self):
        ad_id = random.choice(_state["ad_ids"])
        if _state["reusable_ids"] and random.random() < DUPLICATE_RATIO:
            # ~5% deliberate duplicates: same event_id sent again.
            event_id = random.choice(_state["reusable_ids"])
        else:
            event_id = f"lt_{uuid.uuid4().hex}"
        payload = {
            "event_id": event_id,
            "ad_id": ad_id,
            "viewer_id": f"viewer_{secrets.token_hex(8)}",
            "timestamp": _now_iso(),
            "country": random.choice(["IN", "US", "GB", "DE", "SG"]),
            "device": random.choices(["mobile", "desktop", "tablet", "other"], [0.6, 0.3, 0.08, 0.02])[0],
        }
        with self.client.post("/clicks", json=payload, headers=_state["headers"],
                              name="POST /clicks", catch_response=True) as resp:
            if resp.status_code == 202:
                _state["reusable_ids"].append(event_id)
                if len(_state["reusable_ids"]) > 5000:
                    del _state["reusable_ids"][:2000]
                resp.success()
            elif resp.status_code in (429, 503):
                # Backpressure/dependency hiccup: mark failure but don't spam.
                resp.failure(f"{resp.status_code} {resp.text[:80]}")
            else:
                resp.failure(f"{resp.status_code} {resp.text[:120]}")

    @task(1)
    def dashboard(self):
        # Light read traffic so LB+DB read path is part of the load picture.
        self.client.get("/api/dashboard?window=1m", headers=_read_headers(),
                        name="GET /api/dashboard")


def _read_headers():
    # Dashboard reads use the bearer token minted during setup.
    return _state.get("read_headers", {})


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
