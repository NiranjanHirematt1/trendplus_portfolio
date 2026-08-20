import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt

from app.core.config import settings


def _secret() -> bytes:
    return (settings.ADMIN_SECRET or settings.DATABASE_URL or "trendplus-dev-secret").encode("utf-8")


def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt. Never store the raw password."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, stored: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
    except Exception:
        return False


def create_token(subject: str, token_type: str = "access", expires_minutes: int = 60 * 24 * 7, extra: dict[str, Any] | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "typ": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expires_minutes)).timestamp()),
    }
    if extra:
        payload.update(extra)
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    body = base64.urlsafe_b64encode(raw).rstrip(b"=")
    sig = hmac.new(_secret(), body, hashlib.sha256).digest()
    return body.decode() + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


def generate_totp_secret() -> str:
    """New base32 TOTP shared secret."""
    import pyotp
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, username: str, issuer: str = "TrendPlus Admin") -> str:
    """otpauth:// URI for QR-code enrollment in an authenticator app."""
    import pyotp
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def verify_totp(secret: str, code: str) -> bool:
    """Validate a 6-digit TOTP code (±1 step for clock skew)."""
    if not secret or not code:
        return False
    import pyotp
    try:
        return pyotp.TOTP(secret).verify(str(code).strip(), valid_window=1)
    except Exception:
        return False


def decode_token(token: str, expected_type: str | None = None) -> dict[str, Any] | None:
    try:
        body, sig = token.split(".", 1)
        expected = base64.urlsafe_b64encode(hmac.new(_secret(), body.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
        if not hmac.compare_digest(sig, expected):
            return None
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if expected_type and payload.get("typ") != expected_type:
            return None
        if int(payload.get("exp", 0)) < int(datetime.now(timezone.utc).timestamp()):
            return None
        return payload
    except Exception:
        return None
