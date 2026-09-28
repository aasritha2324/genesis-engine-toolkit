# Clickstream API

Create an advertiser account, a campaign and an ad in the app. Under Settings, generate an ingestion key. Store it securely: the plaintext is shown once.

`POST /api/public/clicks` with `X-API-Key: <key>` and `Content-Type: application/json` accepts one click or `{ "events": [click, ...] }` (up to 500). Example:

```json
{"event_id":"evt_01234567-89ab-4cde-8f01-23456789abcd","ad_id":"ad_from_dashboard","viewer_id":"viewer_100","timestamp":"2026-09-28T10:30:25Z","ip":"192.168.1.10","country":"IN","device":"mobile"}
```

Returns 202 with accepted and duplicate counts. IDs must be stable across retries. The API hashes viewer IDs and IPs before storing them. Events older than 24 hours or more than five minutes in the future are rejected. This hosted implementation writes each event transactionally to the database; it does not use Kafka or Redis, and does not promise the throughput targets of the separate infrastructure design.

`GET /api/public/health` returns service availability only, not a dependency health probe.
