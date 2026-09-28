"""End-to-end integration tests against a running Docker stack.

Run after `docker compose -f deploy/docker-compose.yml up --build`:

  cd deploy/tests && pip install pytest requests && ADVANTA_BASE_URL=http://localhost:8080 pytest -x -q
"""
import secrets
import time
import uuid

import pytest
import requests

WAIT_DEADLINE = 30.0  # seconds to wait for async aggregation


def _now_iso(offset_s: int = 0) -> str:
    t = time.time() + offset_s
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


@pytest.fixture(scope="module")
def adv_a(http, base_url):
    return _register(http, base_url, "a")


@pytest.fixture(scope="module")
def adv_b(http, base_url):
    return _register(http, base_url, "b")


def _register(http, base_url, tag):
    email = f"it-{tag}-{secrets.token_hex(6)}@advanta.test"
    r = http.post(f"{base_url}/auth/register",
                  json={"email": email, "password": "pass-it-1", "organization": f"Org {tag} {uuid.uuid4().hex[:8]}"})
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    campaign = http.post(f"{base_url}/api/campaigns", headers=headers, json={"name": f"IT Campaign {tag}"}).json()
    ad = http.post(f"{base_url}/api/ads", headers=headers,
                   json={"campaign_id": campaign["id"], "title": f"IT Ad {tag}"}).json()
    key = http.post(f"{base_url}/api/ingest-keys", headers=headers, json={"label": "it"}).json()["key"]
    return {"token": token, "headers": headers, "campaign": campaign["id"], "ad": ad["id"], "key": key}


def _click(adv, event_id=None, offset_s=0, viewer=None, ip=None):
    return {
        "event_id": event_id or f"it_{uuid.uuid4().hex}",
        "ad_id": adv["ad"],
        "viewer_id": viewer or f"viewer-{uuid.uuid4().hex}",
        "timestamp": _now_iso(offset_s),
        "country": "IN",
        "device": "desktop",
        **({"ip": ip} if ip else {}),
    }


def _post_click(http, base_url, adv, payload, expect=202):
    r = http.post(f"{base_url}/clicks", json=payload, headers={"X-API-Key": adv["key"], "Content-Type": "application/json"})
    assert r.status_code == expect, f"{r.status_code} {r.text[:200]}"
    return r


# ------------------------------------------------------------------- auth

def test_health_and_ready(http, base_url):
    assert http.get(f"{base_url}/health").status_code == 200
    r = http.get(f"{base_url}/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["postgres"] == "healthy" and body["redis"] == "healthy" and body["kafka"] == "healthy"


def test_register_login_me(http, base_url):
    email = f"auth-{secrets.token_hex(6)}@advanta.test"
    r = http.post(f"{base_url}/auth/register", json={"email": email, "password": "pw-1", "organization": f"AuthOrg {uuid.uuid4().hex[:6]}"})
    assert r.status_code == 201
    token = r.json()["token"]
    r = http.post(f"{base_url}/auth/login", json={"email": email, "password": "pw-1"})
    assert r.status_code == 200 and r.json()["token"]
    me = http.get(f"{base_url}/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["email"] == email and me["roles"] == ["advertiser"]
    assert me["advertiser_id"]  # registration creates a workspace


def test_bad_credentials_rejected(http, base_url):
    r = http.post(f"{base_url}/auth/login", json={"email": "nobody@advanta.test", "password": "wrong"})
    assert r.status_code == 401
    r = http.get(f"{base_url}/api/campaigns")
    assert r.status_code == 401


# -------------------------------------------------------------- ingestion

def test_ingest_and_dashboard(adv_a, http, base_url):
    _post_click(http, base_url, adv_a)
    _wait_aggregated(http, base_url, adv_a, 1)
    dash = _dashboard(http, base_url, adv_a)
    assert dash["total_clicks"] >= 1
    assert dash["clicks_current_minute"] >= 1  # Redis hot counter
    assert any(e["ad_id"] == adv_a["ad"] for e in dash["top_ads"])
    assert dash["devices"][0]["device"] == "desktop"


def test_batch_and_campaign_mismatch(adv_a, http, base_url):
    ok = [_click(adv_a) for _ in range(3)]
    r = http.post(f"{base_url}/clicks/batch", json={"events": ok}, headers={"X-API-Key": adv_a["key"]})
    assert r.status_code == 202 and r.json()["accepted"] == 3

    bad = _click(adv_a)
    bad["campaign_id"] = "camp_not_yours"
    r = http.post(f"{base_url}/clicks/batch", json={"events": [bad]}, headers={"X-API-Key": adv_a["key"]})
    assert r.status_code == 422 and r.json()["error"] == "CAMPAIGN_MISMATCH"


def test_unknown_ad_rejected(adv_a, http, base_url):
    payload = _click(adv_a)
    payload["ad_id"] = "ad_does_not_exist"
    r = http.post(f"{base_url}/clicks", json=payload, headers={"X-API-Key": adv_a["key"]})
    assert r.status_code == 422 and r.json()["error"] == "UNKNOWN_AD"


def test_bad_key_rejected(adv_a, http, base_url):
    payload = _click(adv_a)
    r = http.post(f"{base_url}/clicks", json=payload, headers={"X-API-Key": "ck_invalid"})
    assert r.status_code == 401
    r = http.post(f"{base_url}/clicks", json=payload)
    assert r.status_code == 401


def test_invalid_payloads(adv_a, http, base_url):
    headers = {"X-API-Key": adv_a["key"], "Content-Type": "application/json"}
    r = http.post(f"{base_url}/clicks", data="not json", headers=headers)
    assert r.status_code == 400
    r = http.post(f"{base_url}/clicks", json=_click(adv_a) | {"device": "fridge"}, headers=headers)
    assert r.status_code == 422


def test_load_balanced_replicas(adv_a, http, base_url):
    served = set()
    for _ in range(12):
        r = _post_click(http, base_url, adv_a)
        served.add(r.headers["X-Served-By"])
        assert r.headers.get("X-Upstream-Addr")  # nginx LB header present
    assert len(served) >= 2, "traffic should hit more than one API replica"


# ------------------------------------------------------------ dedup

def test_exact_duplicates_counted_once(adv_a, http, base_url):
    dash_before = _dashboard(http, base_url, adv_a)
    event_id = f"dup_{uuid.uuid4().hex}"
    for _ in range(4):
        _post_click(http, base_url, adv_a, _click(adv_a, event_id=event_id))
    _wait_aggregated(http, base_url, adv_a, dash_before["total_clicks"] + 1)
    dash = _dashboard(http, base_url, adv_a)
    assert dash["total_clicks"] == dash_before["total_clicks"] + 1, "4 identical event_ids must count once"
    assert dash["pipeline"].get("duplicates", 0) >= 3


# ------------------------------------------------- event time / lateness

def test_late_event_is_marked_late(adv_a, http, base_url):
    # 5 minutes behind: beyond the 2-minute allowed lateness, still aggregated.
    _post_click(http, base_url, adv_a, _click(adv_a, offset_s=-300))
    deadline = time.time() + WAIT_DEADLINE
    while time.time() < deadline:
        events = http.get(f"{base_url}/api/events/recent", headers=adv_a["headers"]).json()
        late = [e for e in events if e["is_late"]]
        if late:
            assert late[0]["event_time"].startswith(_now_iso(-300)[:14])
            return
        time.sleep(2)
    pytest.fail("late event never appeared with is_late=true")


def test_future_event_is_poisoned_to_dlq(adv_a, http, base_url):
    # >5 minutes ahead: rejected by the worker into the DLQ, not aggregated.
    _post_click(http, base_url, adv_a, _click(adv_a, offset_s=+600))
    time.sleep(5)  # let the worker classify; it must NOT show up in recent events
    events = http.get(f"{base_url}/api/events/recent", headers=adv_a["headers"]).json()
    assert not any(e["event_id"].startswith("fut") for e in events)


def test_too_old_event_discarded(adv_a, http, base_url):
    dash_before = _dashboard(http, base_url, adv_a)
    _post_click(http, base_url, adv_a, _click(adv_a, offset_s=-48 * 3600))
    time.sleep(6)
    dash = _dashboard(http, base_url, adv_a)
    assert dash["total_clicks"] == dash_before["total_clicks"], "24h+ old events are discarded, never counted"


# ---------------------------------------------------------- fraud rules

def test_viewer_repeat_rule(adv_a, http, base_url):
    # VIEWER_REPEAT_THRESHOLD = 50 clicks by one viewer on one ad within a minute.
    viewer = f"fraud-viewer-{uuid.uuid4().hex[:8]}"
    for _ in range(50):
        _post_click(http, base_url, adv_a, _click(adv_a, viewer=viewer))
    deadline = time.time() + WAIT_DEADLINE
    while time.time() < deadline:
        rows = http.get(f"{base_url}/api/suspicious", headers=adv_a["headers"]).json()
        hit = [r for r in rows if r["rule"] == "viewer_repeat" and viewer in r["subject"]]
        if hit:
            assert hit[0]["click_count"] >= 50
            return
        time.sleep(2)
    pytest.fail("viewer_repeat detection missing after 50 rapid clicks")


# ------------------------------------------------------- tenant isolation

def test_tenant_isolation(adv_a, adv_b, http, base_url):
    # A cannot read B's campaigns/ads even by guessing ids.
    assert http.get(f"{base_url}/api/campaigns/{adv_b['campaign']}", headers=adv_a["headers"]).status_code == 404
    assert http.get(f"{base_url}/api/ads/{adv_b['ad']}", headers=adv_a["headers"]).status_code == 404

    # A's dashboard never contains B's click volumes.
    _post_click(http, base_url, adv_b)
    _wait_aggregated(http, base_url, adv_b, 1)
    dash_a = _dashboard(http, base_url, adv_a)
    assert not any(c["campaign_id"] == adv_b["campaign"] for c in dash_a["campaigns"])
    assert not any(a["ad_id"] == adv_b["ad"] for a in dash_a["top_ads"])

    # A cannot ingest into B's ad with A's key.
    payload = _click(adv_a)
    payload["ad_id"] = adv_b["ad"]
    assert http.post(f"{base_url}/clicks", json=payload, headers={"X-API-Key": adv_a["key"]}).status_code == 422


def test_admin_role_not_self_granted(http, base_url):
    me = None
    email = f"nogrant-{secrets.token_hex(6)}@advanta.test"
    token = http.post(f"{base_url}/auth/register",
                      json={"email": email, "password": "pw-1", "organization": f"NoGrant {uuid.uuid4().hex[:6]}"}).json()["token"]
    me = http.get(f"{base_url}/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    assert "admin" not in me["roles"]


# ------------------------------------------------------------------ helpers

def _dashboard(http, base_url, adv):
    r = http.get(f"{base_url}/api/dashboard?window=1h", headers=adv["headers"])
    assert r.status_code == 200, r.text[:200]
    return r.json()


def _wait_aggregated(http, base_url, adv, at_least: int) -> dict:
    deadline = time.time() + WAIT_DEADLINE
    dash = _dashboard(http, base_url, adv)
    while dash["total_clicks"] < at_least and time.time() < deadline:
        time.sleep(1)
        dash = _dashboard(http, base_url, adv)
    assert dash["total_clicks"] >= at_least, f"aggregation lag: {dash['total_clicks']} < {at_least}"
    return dash
