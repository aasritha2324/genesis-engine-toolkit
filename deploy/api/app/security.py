"""Password hashing, JWTs, API-key and IP hashing. Pure functions (unit tested)."""
import hashlib
import hmac
import secrets
import time
import uuid

import bcrypt
import jwt


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def issue_token(secret: str, user_id: str, advertiser_id: str | None, roles: list[str], ttl: int) -> str:
    now = int(time.time())
    payload = {
        "sub": user_id,
        "adv": advertiser_id,
        "roles": roles,
        "iat": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_token(secret: str, token: str) -> dict:
    # Raises jwt.PyJWTError on bad signature, expiry or malformed tokens.
    return jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["sub", "exp"]})


def new_api_key() -> str:
    return "ck_" + secrets.token_urlsafe(32)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hmac_hex(secret: str, value: str) -> str:
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()
