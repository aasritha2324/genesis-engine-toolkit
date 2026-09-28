"""AdVanta ingestion + analytics API (runs as N replicas behind Nginx).

POST /clicks and /clicks/batch validate, authenticate the X-API-Key, resolve
ad -> campaign -> advertiser on the server, publish to Kafka (key = ad_id) and
return 202. Aggregation happens asynchronously in the aggregator workers.
"""
import asyncio
import json
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import asyncpg
import jwt
import redis.asyncio as aioredis
from aiokafka import AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from .config import load_settings
from .schemas import AdIn, CampaignIn, ClickBatch, ClickIn, Credentials, KeyIn, Register
from .security import (
    decode_token, hash_password, hmac_hex, issue_token, new_api_key, sha256_hex, verify_password,
)

S = load_settings()

HTTP_REQUESTS = Counter("http_requests_total", "API requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram(
    "http_request_seconds", "API request latency", ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
CLICKS_INGESTED = Counter("clicks_ingested_total", "Click events accepted and published to Kafka")
CLICKS_REJECTED = Counter("clicks_rejected_total", "Click events rejected by the API", ["reason"])
KAFKA_PUBLISH = Histogram(
    "kafka_publish_seconds", "Time to publish a request's events to Kafka",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 5),
)
ERRORS = Counter("errors_total", "Errors", ["component", "kind"])

WINDOWS = {"1m": 1, "10m": 10, "1h": 60, "1d": 1440}


class State:
    pool: asyncpg.Pool
    redis: aioredis.Redis
    producer: AIOKafkaProducer | None = None
    kafka_ok: bool = False


st = State()


async def _start_producer() -> None:
    """Keep trying to connect to Kafka; ingestion returns 503 until connected."""
    while True:
        producer = AIOKafkaProducer(
            bootstrap_servers=S.kafka_bootstrap,
            acks="all",
            enable_idempotence=True,
            linger_ms=5,
            request_timeout_ms=5000,
        )
        try:
            await producer.start()
            st.producer = producer
            st.kafka_ok = True
            return
        except Exception:
            ERRORS.labels("api", "kafka_connect").inc()
            await producer.stop()
            await asyncio.sleep(2)


async def _heartbeat() -> None:
    while True:
        try:
            await st.redis.set(
                f"heartbeat:api:{S.instance}",
                json.dumps({"instance": S.instance, "at": datetime.now(timezone.utc).isoformat()}),
                ex=15,
            )
        except Exception:
            ERRORS.labels("api", "heartbeat").inc()
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_: FastAPI):
    st.pool = await asyncpg.create_pool(S.database_url, min_size=1, max_size=10)
    st.redis = aioredis.from_url(S.redis_url, decode_responses=True)
    tasks = [asyncio.create_task(_start_producer()), asyncio.create_task(_heartbeat())]
    yield
    for t in tasks:
        t.cancel()
    if st.producer:
        await st.producer.stop()
    await st.pool.close()
    await st.redis.aclose()


app = FastAPI(title="AdVanta API", lifespan=lifespan)
if S.cors_origins:
    app.add_middleware(
        CORSMiddleware, allow_origins=S.cors_origins, allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key"], expose_headers=["X-Served-By"],
    )


@app.middleware("http")
async def observe(request: Request, call_next):
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        ERRORS.labels("api", "unhandled").inc()
        response = JSONResponse({"error": "INTERNAL"}, status_code=500)
    # Use the route template (e.g. /api/ads/{ad_id}) to keep metric cardinality bounded.
    matched = request.scope.get("route")
    route = getattr(matched, "path", "unmatched")
    response.headers["X-Served-By"] = S.instance
    if route != "/metrics":
        HTTP_REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        HTTP_LATENCY.labels(request.method, route).observe(time.perf_counter() - started)
    return response


# ---------------------------------------------------------------- auth

async def current_user(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")
    try:
        claims = decode_token(S.jwt_secret, authorization[7:])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")
    row = await st.pool.fetchrow("SELECT id, email, advertiser_id FROM users WHERE id=$1", claims["sub"])
    if not row:
        raise HTTPException(401, "Unknown user")
    roles = [r["role"] for r in await st.pool.fetch("SELECT role::text FROM user_roles WHERE user_id=$1", row["id"])]
    return {"id": str(row["id"]), "email": row["email"],
            "advertiser_id": row["advertiser_id"], "roles": roles}


def tenant(user: dict = Depends(current_user)):
    if not user["advertiser_id"]:
        raise HTTPException(403, "No advertiser workspace")
    return user


@app.post("/auth/register", status_code=201)
async def register(body: Register):
    email = body.email.lower()
    async with st.pool.acquire() as con, con.transaction():
        if await con.fetchval("SELECT 1 FROM users WHERE email=$1", email):
            raise HTTPException(409, "Email already registered")
        if await con.fetchval("SELECT 1 FROM advertisers WHERE lower(name)=lower($1)", body.organization):
            raise HTTPException(409, "Organization name taken")
        adv = await con.fetchval("INSERT INTO advertisers(name) VALUES($1) RETURNING id", body.organization.strip())
        uid = await con.fetchval(
            "INSERT INTO users(email,password_hash,advertiser_id) VALUES($1,$2,$3) RETURNING id",
            email, hash_password(body.password), adv)
        # Self-registration only ever grants 'advertiser'. Admin is operator-assigned (see README).
        await con.execute("INSERT INTO user_roles(user_id,role) VALUES($1,'advertiser')", uid)
    return {"token": issue_token(S.jwt_secret, str(uid), str(adv), ["advertiser"], S.jwt_ttl_seconds)}


@app.post("/auth/login")
async def login(body: Credentials):
    row = await st.pool.fetchrow("SELECT id, password_hash, advertiser_id FROM users WHERE email=$1", body.email.lower())
    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    roles = [r["role"] for r in await st.pool.fetch("SELECT role::text FROM user_roles WHERE user_id=$1", row["id"])]
    adv = str(row["advertiser_id"]) if row["advertiser_id"] else None
    return {"token": issue_token(S.jwt_secret, str(row["id"]), adv, roles, S.jwt_ttl_seconds)}


@app.get("/auth/me")
async def me(user: dict = Depends(current_user)):
    name = None
    if user["advertiser_id"]:
        name = await st.pool.fetchval("SELECT name FROM advertisers WHERE id=$1", user["advertiser_id"])
    return {**user, "advertiser_id": str(user["advertiser_id"]) if user["advertiser_id"] else None, "advertiser_name": name}


# ------------------------------------------------------ tenant resources

def _slug(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(5)}"


@app.get("/api/campaigns")
async def list_campaigns(user=Depends(tenant)):
    rows = await st.pool.fetch(
        "SELECT id,name,status,created_at FROM campaigns WHERE advertiser_id=$1 ORDER BY created_at DESC", user["advertiser_id"])
    return [dict(r) for r in rows]


@app.post("/api/campaigns", status_code=201)
async def create_campaign(body: CampaignIn, user=Depends(tenant)):
    row = await st.pool.fetchrow(
        "INSERT INTO campaigns(id,advertiser_id,name) VALUES($1,$2,$3) RETURNING id,name,status,created_at",
        _slug("camp"), user["advertiser_id"], body.name.strip())
    return dict(row)


@app.get("/api/campaigns/{campaign_id}")
async def get_campaign(campaign_id: str, user=Depends(tenant)):
    # Tenant check happens in SQL: another advertiser's campaign is indistinguishable from a missing one.
    row = await st.pool.fetchrow(
        "SELECT id,name,status,created_at FROM campaigns WHERE id=$1 AND advertiser_id=$2", campaign_id, user["advertiser_id"])
    if not row:
        raise HTTPException(404, "Campaign not found")
    return dict(row)


@app.get("/api/ads")
async def list_ads(user=Depends(tenant)):
    rows = await st.pool.fetch(
        "SELECT id,campaign_id,title,created_at FROM ads WHERE advertiser_id=$1 ORDER BY created_at DESC", user["advertiser_id"])
    return [dict(r) for r in rows]


@app.post("/api/ads", status_code=201)
async def create_ad(body: AdIn, user=Depends(tenant)):
    owned = await st.pool.fetchval(
        "SELECT 1 FROM campaigns WHERE id=$1 AND advertiser_id=$2", body.campaign_id, user["advertiser_id"])
    if not owned:
        raise HTTPException(404, "Campaign not found")
    row = await st.pool.fetchrow(
        "INSERT INTO ads(id,campaign_id,advertiser_id,title) VALUES($1,$2,$3,$4) RETURNING id,campaign_id,title,created_at",
        _slug("ad"), body.campaign_id, user["advertiser_id"], body.title.strip())
    return dict(row)


@app.get("/api/ads/{ad_id}")
async def get_ad(ad_id: str, user=Depends(tenant)):
    row = await st.pool.fetchrow(
        "SELECT id,campaign_id,title,created_at FROM ads WHERE id=$1 AND advertiser_id=$2", ad_id, user["advertiser_id"])
    if not row:
        raise HTTPException(404, "Ad not found")
    return dict(row)


@app.post("/api/ingest-keys", status_code=201)
async def create_key(body: KeyIn, user=Depends(tenant)):
    key = new_api_key()
    await st.pool.execute(
        "INSERT INTO ingest_keys(advertiser_id,label,key_hash) VALUES($1,$2,$3)",
        user["advertiser_id"], body.label, sha256_hex(key))
    return {"key": key, "label": body.label, "note": "Shown once. Store it securely."}


# ------------------------------------------------------------ analytics

@app.get("/api/dashboard")
async def dashboard(window: str = "1h", campaign_id: str | None = None, user=Depends(tenant)):
    if window not in WINDOWS:
        raise HTTPException(422, "window must be one of 1m,10m,1h,1d")
    adv = user["advertiser_id"]
    since = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=WINDOWS[window] - 1)
    filt, args = "advertiser_id=$1 AND window_start>=$2", [adv, since]
    if campaign_id:
        filt += " AND campaign_id=$3"
        args.append(campaign_id)
    async with st.pool.acquire() as con:
        totals = await con.fetchrow(f"SELECT coalesce(sum(click_count),0)::bigint clicks, coalesce(sum(unique_viewers),0)::bigint viewers FROM aggregated_clicks WHERE {filt}", *args)
        series = await con.fetch(f"SELECT window_start, sum(click_count)::bigint clicks FROM aggregated_clicks WHERE {filt} GROUP BY 1 ORDER BY 1", *args)
        top_ads = await con.fetch(f"SELECT a.ad_id, ad.title, sum(a.click_count)::bigint clicks FROM aggregated_clicks a JOIN ads ad ON ad.id=a.ad_id WHERE a.{filt.replace(' AND ', ' AND a.')} GROUP BY 1,2 ORDER BY 3 DESC LIMIT 5", *args)
        campaigns = await con.fetch(f"SELECT a.campaign_id, c.name, sum(a.click_count)::bigint clicks, sum(a.unique_viewers)::bigint viewers FROM aggregated_clicks a JOIN campaigns c ON c.id=a.campaign_id WHERE a.{filt.replace(' AND ', ' AND a.')} GROUP BY 1,2 ORDER BY 3 DESC", *args)
        countries = await con.fetch(f"SELECT country, sum(click_count)::bigint clicks FROM aggregated_clicks WHERE {filt} GROUP BY 1 ORDER BY 2 DESC LIMIT 10", *args)
        devices = await con.fetch(f"SELECT device, sum(click_count)::bigint clicks FROM aggregated_clicks WHERE {filt} GROUP BY 1 ORDER BY 2 DESC", *args)
        suspicious = await con.fetchval("SELECT count(*) FROM suspicious_clicks WHERE advertiser_id=$1 AND window_start>=$2", adv, since)
    # Hot counters from Redis (written by aggregator workers after each committed batch).
    now_min = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    last_min = now_min - timedelta(minutes=1)
    hot = await st.redis.mget(f"clicks:adv:{adv}:{int(now_min.timestamp())}", f"clicks:adv:{adv}:{int(last_min.timestamp())}")
    stats = await st.redis.hgetall(f"stats:adv:{adv}")
    return {
        "window": window,
        "total_clicks": totals["clicks"],
        "unique_viewers": totals["viewers"],
        "clicks_current_minute": int(hot[0] or 0),
        "clicks_last_minute": int(hot[1] or 0),
        "suspicious": suspicious,
        "series": [{"t": r["window_start"].isoformat(), "clicks": r["clicks"]} for r in series],
        "top_ads": [dict(r) for r in top_ads],
        "campaigns": [dict(r) for r in campaigns],
        "countries": [dict(r) for r in countries],
        "devices": [dict(r) for r in devices],
        "pipeline": {k: int(v) for k, v in stats.items()},
    }


@app.get("/api/events/recent")
async def recent_events(user=Depends(tenant)):
    rows = await st.pool.fetch(
        "SELECT event_id, ad_id, campaign_id, country, device, event_time, processed_at, served_by, processed_by, is_late "
        "FROM click_events WHERE advertiser_id=$1 ORDER BY processed_at DESC LIMIT 25", user["advertiser_id"])
    return [dict(r) for r in rows]


@app.get("/api/suspicious")
async def suspicious(user=Depends(tenant)):
    rows = await st.pool.fetch(
        "SELECT rule, ad_id, campaign_id, click_count, window_start, detected_at, details "
        "FROM suspicious_clicks WHERE advertiser_id=$1 ORDER BY detected_at DESC LIMIT 50", user["advertiser_id"])
    return [{**dict(r), "details": json.loads(r["details"]) if isinstance(r["details"], str) else r["details"]} for r in rows]


@app.get("/api/infra")
async def infra(_=Depends(tenant)):
    deps = await _check_deps()
    apis, workers = [], []
    try:
        async for key in st.redis.scan_iter("heartbeat:*", count=100):
            raw = await st.redis.get(key)
            if raw:
                (apis if key.startswith("heartbeat:api:") else workers).append(json.loads(raw))
    except Exception:
        pass
    return {**deps, "served_by": S.instance, "api_replicas": sorted(apis, key=lambda x: x["instance"]),
            "workers": sorted(workers, key=lambda x: x["instance"])}


# ------------------------------------------------------------ ingestion

async def _resolve_key(api_key: str | None) -> str:
    if not api_key or len(api_key) > 200:
        raise HTTPException(401, "Missing or invalid X-API-Key")
    adv = await st.pool.fetchval(
        "SELECT advertiser_id FROM ingest_keys WHERE key_hash=$1 AND revoked_at IS NULL", sha256_hex(api_key))
    if not adv:
        CLICKS_REJECTED.labels("bad_key").inc()
        raise HTTPException(401, "Missing or invalid X-API-Key")
    return str(adv)


async def _resolve_ads(ad_ids: set[str], advertiser_id: str) -> dict[str, dict]:
    # ad -> campaign -> advertiser is resolved server-side; client-supplied owners are never trusted.
    rows = await st.pool.fetch(
        "SELECT id, campaign_id, advertiser_id FROM ads WHERE id = ANY($1::text[]) AND advertiser_id=$2",
        list(ad_ids), advertiser_id)
    return {r["id"]: {"campaign_id": r["campaign_id"], "advertiser_id": str(r["advertiser_id"])} for r in rows}


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")


async def _publish(request: Request, events: list[ClickIn], api_key: str | None) -> JSONResponse:
    advertiser_id = await _resolve_key(api_key)
    ads = await _resolve_ads({e.ad_id for e in events}, advertiser_id)
    unknown = sorted({e.ad_id for e in events if e.ad_id not in ads})
    if unknown:
        CLICKS_REJECTED.labels("unknown_ad").inc(len(events))
        raise HTTPException(422, {"error": "UNKNOWN_AD", "ad_ids": unknown[:20]})
    mismatch = [e.event_id for e in events if e.campaign_id and e.campaign_id != ads[e.ad_id]["campaign_id"]]
    if mismatch:
        CLICKS_REJECTED.labels("campaign_mismatch").inc(len(events))
        raise HTTPException(422, {"error": "CAMPAIGN_MISMATCH", "event_ids": mismatch[:20]})
    if not st.producer or not st.kafka_ok:
        ERRORS.labels("api", "kafka_unavailable").inc()
        raise HTTPException(503, {"error": "KAFKA_UNAVAILABLE", "message": "Events were NOT accepted; retry later."})

    fallback_ip = _client_ip(request)
    received = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    try:
        futures = []
        for e in events:
            owner = ads[e.ad_id]
            ip = e.ip or fallback_ip
            payload = {
                "event_id": e.event_id,
                "ad_id": e.ad_id,
                "campaign_id": owner["campaign_id"],
                "advertiser_id": owner["advertiser_id"],
                "viewer_id": hmac_hex(S.ip_hmac_secret, "viewer:" + e.viewer_id),
                "timestamp": e.timestamp.isoformat(),
                "ip_hash": hmac_hex(S.ip_hmac_secret, "ip:" + ip) if ip else None,  # raw IP never leaves the API
                "country": e.country,
                "device": e.device,
                "served_by": S.instance,
                "received_at": received,
            }
            futures.append(await st.producer.send(
                S.kafka_topic, key=e.ad_id.encode(), value=json.dumps(payload).encode()))
        metas = await asyncio.wait_for(asyncio.gather(*futures), timeout=10)
    except Exception:
        ERRORS.labels("api", "kafka_publish").inc()
        raise HTTPException(503, {"error": "KAFKA_PUBLISH_FAILED", "message": "Events were NOT accepted; retry later."})
    KAFKA_PUBLISH.observe(time.perf_counter() - started)
    CLICKS_INGESTED.inc(len(events))
    return JSONResponse(
        {"accepted": len(events), "queued": True, "served_by": S.instance,
         "partitions": sorted({m.partition for m in metas})},
        status_code=202)


async def _read_body(request: Request):
    raw = await request.body()
    if len(raw) > S.max_body_bytes:
        raise HTTPException(413, "Body too large")
    try:
        return json.loads(raw or b"null")
    except ValueError:
        raise HTTPException(400, "Invalid JSON")


@app.post("/clicks", status_code=202)
async def ingest_one(request: Request, x_api_key: str | None = Header(default=None)):
    data = await _read_body(request)
    if isinstance(data, dict) and "events" in data:
        return await ingest_batch(request, x_api_key, data)
    try:
        event = ClickIn.model_validate(data)
    except Exception as exc:
        CLICKS_REJECTED.labels("invalid").inc()
        raise HTTPException(422, {"error": "INVALID_EVENT", "detail": str(exc)[:500]})
    return await _publish(request, [event], x_api_key)


@app.post("/clicks/batch", status_code=202)
async def ingest_batch(request: Request, x_api_key: str | None = Header(default=None), data=None):
    if data is None:
        data = await _read_body(request)
    try:
        batch = ClickBatch.model_validate(data)
    except Exception as exc:
        CLICKS_REJECTED.labels("invalid").inc()
        raise HTTPException(422, {"error": "INVALID_BATCH", "detail": str(exc)[:500]})
    return await _publish(request, batch.events, x_api_key)


# --------------------------------------------------------------- health

async def _check_deps() -> dict:
    out = {}
    try:
        await asyncio.wait_for(st.pool.fetchval("SELECT 1"), 2)
        out["postgres"] = "healthy"
    except Exception:
        out["postgres"] = "unhealthy"
    try:
        await asyncio.wait_for(st.redis.ping(), 2)
        out["redis"] = "healthy"
    except Exception:
        out["redis"] = "unhealthy"
    try:
        admin = AIOKafkaAdminClient(bootstrap_servers=S.kafka_bootstrap, request_timeout_ms=2000)
        await asyncio.wait_for(admin.start(), 3)
        try:
            meta = await asyncio.wait_for(admin.describe_topics([S.kafka_topic]), 3)
            out["kafka"] = "healthy"
            out["kafka_partitions"] = len(meta[0].get("partitions", [])) if meta else 0
        finally:
            await admin.close()
    except Exception:
        out["kafka"] = "unhealthy"
    out["status"] = "healthy" if all(out.get(k) == "healthy" for k in ("postgres", "redis", "kafka")) else "degraded"
    return out


@app.get("/health")
async def health():
    return {"status": "alive", "instance": S.instance}


@app.get("/ready")
async def ready():
    deps = await _check_deps()
    deps["producer"] = "connected" if st.kafka_ok else "disconnected"
    ok = deps["status"] == "healthy" and st.kafka_ok
    return JSONResponse({**deps, "instance": S.instance}, status_code=200 if ok else 503)


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
