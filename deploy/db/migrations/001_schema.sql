-- AdVanta local distributed stack: durable source of truth.
-- Mirrors the hosted schema (advertisers, campaigns, ads, click_events,
-- aggregated_clicks, suspicious_clicks, ingest_keys) and adds local users
-- because the Docker stack uses its own JWT auth instead of hosted auth.
-- Additive only: never drops data.

CREATE TABLE IF NOT EXISTS advertisers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email text NOT NULL UNIQUE,
  password_hash text NOT NULL,
  advertiser_id uuid REFERENCES advertisers(id),
  created_at timestamptz NOT NULL DEFAULT now()
);

DO $$ BEGIN
  CREATE TYPE app_role AS ENUM ('admin', 'advertiser');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Roles live in their own table, never on users.
CREATE TABLE IF NOT EXISTS user_roles (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role app_role NOT NULL,
  UNIQUE (user_id, role)
);

CREATE TABLE IF NOT EXISTS campaigns (
  id text PRIMARY KEY,
  advertiser_id uuid NOT NULL REFERENCES advertisers(id),
  name text NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','ended')),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ads (
  id text PRIMARY KEY,
  campaign_id text NOT NULL REFERENCES campaigns(id),
  advertiser_id uuid NOT NULL REFERENCES advertisers(id),
  title text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS click_events (
  event_id text PRIMARY KEY,
  ad_id text NOT NULL REFERENCES ads(id),
  campaign_id text NOT NULL REFERENCES campaigns(id),
  advertiser_id uuid NOT NULL REFERENCES advertisers(id),
  viewer_id text NOT NULL,
  event_time timestamptz NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz NOT NULL DEFAULT now(),
  ip_hash text,
  country char(2) NOT NULL,
  device text NOT NULL CHECK (device IN ('mobile','desktop','tablet','other')),
  served_by text,
  processed_by text,
  is_late boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS click_events_viewer_idx ON click_events (ad_id, viewer_id, event_time);
CREATE INDEX IF NOT EXISTS click_events_recent_idx ON click_events (advertiser_id, processed_at DESC);

CREATE TABLE IF NOT EXISTS aggregated_clicks (
  window_start timestamptz NOT NULL,
  advertiser_id uuid NOT NULL REFERENCES advertisers(id),
  campaign_id text NOT NULL REFERENCES campaigns(id),
  ad_id text NOT NULL REFERENCES ads(id),
  country char(2) NOT NULL,
  device text NOT NULL,
  click_count bigint NOT NULL DEFAULT 0,
  unique_viewers bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (window_start, ad_id, country, device)
);
CREATE INDEX IF NOT EXISTS aggregated_adv_idx ON aggregated_clicks (advertiser_id, window_start);
CREATE INDEX IF NOT EXISTS aggregated_campaign_idx ON aggregated_clicks (campaign_id, window_start);

CREATE TABLE IF NOT EXISTS suspicious_clicks (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  advertiser_id uuid NOT NULL REFERENCES advertisers(id),
  campaign_id text NOT NULL,
  ad_id text NOT NULL,
  rule text NOT NULL,
  subject text NOT NULL,
  click_count int NOT NULL,
  window_start timestamptz NOT NULL,
  detected_at timestamptz NOT NULL DEFAULT now(),
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (rule, subject, ad_id, window_start)
);
CREATE INDEX IF NOT EXISTS suspicious_adv_idx ON suspicious_clicks (advertiser_id, detected_at DESC);

CREATE TABLE IF NOT EXISTS ingest_keys (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  advertiser_id uuid NOT NULL REFERENCES advertisers(id),
  label text NOT NULL,
  key_hash text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  revoked_at timestamptz
);
