"""Aggregator worker: consumes `ad-clicks` in consumer group `aggregator`.

Per batch:  validate -> dedup -> event-time/lateness -> 1-minute aggregation
            -> PostgreSQL transaction (+ fraud rules) -> Redis hot state
            -> commit Kafka offsets.
Offsets are committed ONLY after PostgreSQL committed. On failure the worker
seeks back and retries, so delivery is at-least-once; `click_events.event_id`
PRIMARY KEY + ON CONFLICT DO NOTHING makes counting effectively-once.
Poison messages go to `ad-clicks-dlq` instead of crashing the worker.
"""
import asyncio
import json
import logging
import os
import signal
import socket
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import asyncpg
import redis.asyncio as aioredis
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, ConsumerRebalanceListener, TopicPartition
from prometheus_client import Counter, Gauge, Histogram, start_http_server

import processing as P

log = logging.getLogger("aggregator")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ["REDIS_URL"]
BOOTSTRAP = os.environ["KAFKA_BOOTSTRAP_SERVERS"]
TOPIC = os.environ.get("KAFKA_TOPIC", "ad-clicks")
DLQ_TOPIC = os.environ.get("KAFKA_DLQ_TOPIC", "ad-clicks-dlq")
GROUP = os.environ.get("KAFKA_GROUP_ID", "aggregator")
INSTANCE = os.environ.get("INSTANCE_NAME") or socket.gethostname()
MAX_BATCH = int(os.environ.get("MAX_BATCH", "500"))
HOT_TTL = 25 * 3600
DEDUP_TTL = 24 * 3600

EVENTS_PROCESSED = Counter("events_processed_total", "Events newly counted into aggregates")
DUPLICATES = Counter("duplicates_dropped_total", "Duplicate event_ids dropped", ["layer"])
LATE = Counter("late_events_total", "Events behind the watermark by more than the allowed lateness (still counted)")
DISCARDED = Counter("discarded_events_total", "Events older than 24h, discarded")
DLQ = Counter("dlq_events_total", "Poison messages sent to the DLQ", ["reason"])
ERRORS = Counter("errors_total", "Errors", ["component", "kind"])
FRAUD = Counter("fraud_detections_total", "Suspicious-activity detections", ["rule"])
BATCH_SECONDS = Histogram("batch_seconds", "Batch processing time", buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))
BATCH_SIZE = Histogram("batch_size", "Messages per batch", buckets=(1, 10, 50, 100, 250, 500))
LAG = Gauge("kafka_consumer_lag", "Messages behind the partition high-water mark", ["partition"])
ASSIGNED = Gauge("assigned_partitions", "Partitions currently assigned to this worker")
UNIQUE_VIEWERS = Gauge("unique_viewers_current_minute", "HyperLogLog estimate of unique viewers this minute (all tenants)")

watermarks: dict[int, datetime] = {}
running = True


class Rebalance(ConsumerRebalanceListener):
    async def on_partitions_revoked(self, revoked):
        for tp in revoked:
            watermarks.pop(tp.partition, None)
            LAG.remove(str(tp.partition)) if str(tp.partition) in _lag_labels else None
            _lag_labels.discard(str(tp.partition))
        log.info("partitions revoked: %s", sorted(tp.partition for tp in revoked))

    async def on_partitions_assigned(self, assigned):
        ASSIGNED.set(len(assigned))
        log.info("partitions assigned: %s", sorted(tp.partition for tp in assigned))


_lag_labels: set[str] = set()


async def send_dlq(producer: AIOKafkaProducer, msg, reason: str, error: str) -> None:
    await producer.send_and_wait(
        DLQ_TOPIC, key=msg.key, value=msg.value,
        headers=[("error", error[:500].encode()), ("reason", reason.encode()),
                 ("source", f"{msg.topic}:{msg.partition}:{msg.offset}".encode()), ("worker", INSTANCE.encode())])
    DLQ.labels(reason).inc()


async def process_batch(records, pool: asyncpg.Pool, r: aioredis.Redis, producer: AIOKafkaProducer) -> None:
    now = datetime.now(timezone.utc)
    candidates: list[tuple[P.Event, bool]] = []  # (event, is_late)
    seen: set[str] = set()
    dup_by_adv: dict[str, int] = defaultdict(int)

    # 1. validation + in-batch duplicates + lateness
    for msg in records:
        try:
            event = P.validate(json.loads(msg.value))
        except (ValueError, TypeError) as exc:
            await send_dlq(producer, msg, "invalid", str(exc))
            continue
        kind = P.classify(event.event_time, watermarks.get(msg.partition), now)
        if kind == "future":
            await send_dlq(producer, msg, "future_timestamp", event.event_time.isoformat())
            continue
        if kind == "too_old":
            DISCARDED.inc()
            continue
        wm = watermarks.get(msg.partition)
        if wm is None or event.event_time > wm:
            watermarks[msg.partition] = event.event_time
        if event.event_id in seen:
            DUPLICATES.labels("batch").inc()
            dup_by_adv[event.advertiser_id] += 1
            continue
        seen.add(event.event_id)
        candidates.append((event, kind == "late"))

    if not candidates:
        await _record_dups(r, dup_by_adv)
        return

    # 2. fast Redis dedup pre-check (keys are only set after a successful PG commit)
    flags = await r.mget([f"dedup:{e.event_id}" for e, _ in candidates])
    fresh = []
    for (event, late), flag in zip(candidates, flags):
        if flag:
            DUPLICATES.labels("redis").inc()
            dup_by_adv[event.advertiser_id] += 1
        else:
            fresh.append((event, late))

    # 3. PostgreSQL transaction: raw events, 1-minute aggregates, fraud rules
    inserted: list[tuple[P.Event, bool]] = []
    pg_dups: list[P.Event] = []
    detections: list[tuple[str, P.Event, int]] = []
    touched_campaigns: dict[tuple[str, datetime], P.Event] = {}
    if fresh:
        async with pool.acquire() as con, con.transaction():
            for event, late in fresh:
                win = P.window_start(event.event_time)
                ok = await con.fetchval(
                    "INSERT INTO click_events(event_id,ad_id,campaign_id,advertiser_id,viewer_id,event_time,ip_hash,country,device,served_by,processed_by,is_late) "
                    "VALUES($1,$2,$3,$4::uuid,$5,$6,$7,$8,$9,$10,$11,$12) ON CONFLICT (event_id) DO NOTHING RETURNING true",
                    event.event_id, event.ad_id, event.campaign_id, event.advertiser_id, event.viewer_id,
                    event.event_time, event.ip_hash, event.country, event.device, event.served_by, INSTANCE, late)
                if not ok:
                    # Only aggregate if the insert actually happened.
                    pg_dups.append(event)
                    continue
                inserted.append((event, late))
                new_viewer = not await con.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM click_events WHERE ad_id=$1 AND viewer_id=$2 AND event_time>=$3 AND event_time<$4 AND event_id<>$5)",
                    event.ad_id, event.viewer_id, win, win + P.WINDOW, event.event_id)
                await con.execute(
                    "INSERT INTO aggregated_clicks(window_start,advertiser_id,campaign_id,ad_id,country,device,click_count,unique_viewers) "
                    "VALUES($1,$2::uuid,$3,$4,$5,$6,1,$7) ON CONFLICT (window_start,ad_id,country,device) DO UPDATE SET "
                    "click_count=aggregated_clicks.click_count+1, unique_viewers=aggregated_clicks.unique_viewers+EXCLUDED.unique_viewers",
                    win, event.advertiser_id, event.campaign_id, event.ad_id, event.country, event.device, 1 if new_viewer else 0)

                # Fraud counters in Redis sorted sets keyed by event_id: idempotent across retries.
                vkey = f"fraud:viewer:{event.ad_id}:{event.viewer_id}:{int(win.timestamp())}"
                pipe = r.pipeline()
                pipe.zadd(vkey, {event.event_id: event.event_time.timestamp()})
                pipe.expire(vkey, 300)
                pipe.zcard(vkey)
                ikey = None
                if event.ip_hash:
                    ikey = f"fraud:ip:{event.ip_hash}:{int(P.ip_bucket(event.event_time).timestamp())}"
                    pipe.zadd(ikey, {event.event_id: event.event_time.timestamp()})
                    pipe.expire(ikey, 300)
                    pipe.zcard(ikey)
                res = await pipe.execute()
                viewer_count = res[2]
                if viewer_count >= P.VIEWER_REPEAT_THRESHOLD:
                    await _flag(con, event, "viewer_repeat", event.viewer_id, viewer_count, win,
                                {"threshold": P.VIEWER_REPEAT_THRESHOLD, "window": "1m"})
                    if viewer_count == P.VIEWER_REPEAT_THRESHOLD:
                        detections.append(("viewer_repeat", event, viewer_count))
                if ikey is not None:
                    ip_count = res[5]
                    if ip_count >= P.IP_BURST_THRESHOLD:
                        await _flag(con, event, "ip_burst", event.ip_hash, ip_count, P.ip_bucket(event.event_time),
                                    {"threshold": P.IP_BURST_THRESHOLD, "window": "10s"})
                        if ip_count == P.IP_BURST_THRESHOLD:
                            detections.append(("ip_burst", event, ip_count))
                touched_campaigns[(event.campaign_id, win)] = event

            # Campaign spike: current minute vs trailing 10-minute average (durable counts from PG).
            for (campaign_id, win), event in touched_campaigns.items():
                rows = await con.fetch(
                    "SELECT window_start, sum(click_count)::bigint c FROM aggregated_clicks "
                    "WHERE campaign_id=$1 AND window_start>=$2 AND window_start<=$3 GROUP BY 1",
                    campaign_id, win - timedelta(minutes=P.SPIKE_TRAILING_MINUTES), win)
                by_min = {row["window_start"]: row["c"] for row in rows}
                current = by_min.get(win, 0)
                trailing = [by_min.get(win - timedelta(minutes=i), 0) for i in range(1, P.SPIKE_TRAILING_MINUTES + 1)]
                if P.is_campaign_spike(current, trailing):
                    new = await _flag(con, event, "campaign_spike", campaign_id, current, win,
                                      {"trailing_avg": sum(trailing) / P.SPIKE_TRAILING_MINUTES,
                                       "multiplier": P.SPIKE_MULTIPLIER, "min_volume": P.SPIKE_MIN_VOLUME}, ad_id="*")
                    if new:
                        detections.append(("campaign_spike", event, current))
        # transaction committed here; any exception above rolls back and propagates

    for rule, _, _ in detections:
        FRAUD.labels(rule).inc()

    # 4. Redis hot state. SET NX on dedup:{event_id} guards counters, so a retry after a crash
    #    between PG commit and this step still applies each event's counters exactly once.
    for event in pg_dups:
        DUPLICATES.labels("postgres").inc()
        dup_by_adv[event.advertiser_id] += 1
    to_apply = [e for e, _ in inserted] + pg_dups
    for event, late in inserted:
        EVENTS_PROCESSED.inc()
        if late:
            LATE.inc()
    if to_apply:
        claim = r.pipeline()
        for e in to_apply:
            claim.set(f"dedup:{e.event_id}", "1", nx=True, ex=DEDUP_TTL)
        claimed = await claim.execute()
        inserted_ids = {e.event_id for e, _ in inserted}
        pipe = r.pipeline()
        processed_by_adv: dict[str, int] = defaultdict(int)
        for e, got in zip(to_apply, claimed):
            if not got:
                continue
            if e.event_id not in inserted_ids:
                # Crash recovery path: PG already had it but Redis never got its counters.
                pass
            processed_by_adv[e.advertiser_id] += 1
            m = int(P.window_start(e.event_time).timestamp())
            for key in (f"clicks:ad:{e.ad_id}:{m}", f"clicks:campaign:{e.campaign_id}:{m}", f"clicks:adv:{e.advertiser_id}:{m}"):
                pipe.incr(key)
                pipe.expire(key, HOT_TTL)
            for key, member in ((f"unique:{e.ad_id}:{m}", e.viewer_id), (f"unique:adv:{e.advertiser_id}:{m}", e.viewer_id), (f"unique:all:{m}", e.viewer_id)):
                pipe.pfadd(key, member)
                pipe.expire(key, HOT_TTL)
            for key, member in ((f"top:ads:{e.advertiser_id}:{m}", e.ad_id), (f"top:campaigns:{e.advertiser_id}:{m}", e.campaign_id)):
                pipe.zincrby(key, 1, member)
                pipe.expire(key, HOT_TTL)
        for adv, n in processed_by_adv.items():
            pipe.hincrby(f"stats:adv:{adv}", "processed", n)
        await pipe.execute()
    await _record_dups(r, dup_by_adv)
    for rule, event, _ in detections:
        await r.hincrby(f"stats:adv:{event.advertiser_id}", f"fraud_{rule}", 1)


async def _record_dups(r: aioredis.Redis, dup_by_adv: dict[str, int]) -> None:
    if dup_by_adv:
        pipe = r.pipeline()
        for adv, n in dup_by_adv.items():
            pipe.hincrby(f"stats:adv:{adv}", "duplicates", n)
        await pipe.execute()


async def _flag(con, event: P.Event, rule: str, subject: str, count: int, win: datetime, details: dict, ad_id: str | None = None) -> bool:
    """Upsert a suspicious-activity row. Events stay counted; fraud only flags."""
    return await con.fetchval(
        "INSERT INTO suspicious_clicks(advertiser_id,campaign_id,ad_id,rule,subject,click_count,window_start,details) "
        "VALUES($1::uuid,$2,$3,$4,$5,$6,$7,$8::jsonb) ON CONFLICT (rule,subject,ad_id,window_start) "
        "DO UPDATE SET click_count=GREATEST(suspicious_clicks.click_count, EXCLUDED.click_count) RETURNING (xmax = 0)",
        event.advertiser_id, event.campaign_id, ad_id or event.ad_id, rule, subject, count, win, json.dumps(details))


async def heartbeat(consumer: AIOKafkaConsumer, r: aioredis.Redis) -> None:
    while running:
        try:
            parts = sorted(tp.partition for tp in consumer.assignment())
            ASSIGNED.set(len(parts))
            total_lag = 0
            for tp in consumer.assignment():
                hw = consumer.highwater(tp)
                if hw is None:
                    continue
                pos = await consumer.position(tp)
                lag = max(0, hw - pos)
                total_lag += lag
                LAG.labels(str(tp.partition)).set(lag)
                _lag_labels.add(str(tp.partition))
            m = int(P.window_start(datetime.now(timezone.utc)).timestamp())
            UNIQUE_VIEWERS.set(await r.pfcount(f"unique:all:{m}"))
            await r.set(f"heartbeat:worker:{INSTANCE}", json.dumps({
                "instance": INSTANCE, "partitions": parts, "lag": total_lag,
                "at": datetime.now(timezone.utc).isoformat()}), ex=15)
        except Exception as exc:
            ERRORS.labels("aggregator", "heartbeat").inc()
            log.warning("heartbeat failed: %s", exc)
        await asyncio.sleep(5)


async def main() -> None:
    global running
    start_http_server(9100)
    pool = await _retry(lambda: asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4), "postgres")
    r = aioredis.from_url(REDIS_URL, decode_responses=True)
    producer = AIOKafkaProducer(bootstrap_servers=BOOTSTRAP, acks="all", enable_idempotence=True)
    await _retry(producer.start, "kafka producer")
    consumer = AIOKafkaConsumer(
        bootstrap_servers=BOOTSTRAP, group_id=GROUP, enable_auto_commit=False,
        auto_offset_reset="earliest", max_poll_records=MAX_BATCH, client_id=INSTANCE,
        session_timeout_ms=10000, heartbeat_interval_ms=3000)
    consumer.subscribe([TOPIC], listener=Rebalance())
    await _retry(consumer.start, "kafka consumer")
    hb = asyncio.create_task(heartbeat(consumer, r))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: globals().__setitem__("running", False))
    log.info("worker %s consuming %s as group %s", INSTANCE, TOPIC, GROUP)

    backoff = 1.0
    try:
        while running:
            batches = await consumer.getmany(timeout_ms=1000, max_records=MAX_BATCH)
            if not batches:
                continue
            records = [m for msgs in batches.values() for m in msgs]
            started = time.perf_counter()
            try:
                await process_batch(records, pool, r, producer)
            except Exception as exc:
                # Do NOT commit: rewind so Kafka redelivers the batch after a backoff.
                ERRORS.labels("aggregator", type(exc).__name__).inc()
                log.error("batch failed, will retry in %.1fs: %s", backoff, exc)
                for tp, msgs in batches.items():
                    consumer.seek(tp, msgs[0].offset)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            await consumer.commit({tp: msgs[-1].offset + 1 for tp, msgs in batches.items()})
            BATCH_SECONDS.observe(time.perf_counter() - started)
            BATCH_SIZE.observe(len(records))
            backoff = 1.0
    finally:
        hb.cancel()
        await consumer.stop()  # leaves the group so partitions rebalance immediately
        await producer.stop()
        await pool.close()
        await r.aclose()


async def _retry(fn, what: str):
    delay = 1
    while True:
        try:
            return await fn()
        except Exception as exc:
            log.warning("%s not ready (%s); retrying in %ss", what, exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)


if __name__ == "__main__":
    asyncio.run(main())
