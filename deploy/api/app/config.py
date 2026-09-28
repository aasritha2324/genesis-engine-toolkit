import os
import socket


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


class Settings:
    def __init__(self) -> None:
        self.database_url = _require("DATABASE_URL")
        self.redis_url = _require("REDIS_URL")
        self.kafka_bootstrap = _require("KAFKA_BOOTSTRAP_SERVERS")
        self.kafka_topic = os.environ.get("KAFKA_TOPIC", "ad-clicks")
        self.jwt_secret = _require("JWT_SECRET")
        self.ip_hmac_secret = _require("CLICK_IP_HMAC_SECRET")
        self.jwt_ttl_seconds = int(os.environ.get("JWT_TTL_SECONDS", "43200"))
        self.cors_origins = [o for o in os.environ.get("CORS_ORIGINS", "").split(",") if o]
        self.instance = os.environ.get("INSTANCE_NAME") or socket.gethostname()
        self.max_batch = 500
        self.max_body_bytes = 300_000


def load_settings() -> Settings:
    return Settings()
