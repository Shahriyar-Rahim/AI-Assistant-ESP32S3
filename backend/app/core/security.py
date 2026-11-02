from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time


class AuthenticationError(ValueError):
    pass


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("password must be at least 12 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"pbkdf2_sha256$310000${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        scheme, rounds, salt_text, digest_text = stored_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        expected = _unb64(digest_text)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt_text), int(rounds))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def issue_access_token(subject: str, role: str, secret: str, lifetime_seconds: int) -> str:
    if len(secret) < 32:
        raise ValueError("AUTH_SIGNING_SECRET must contain at least 32 characters")
    payload = _b64(json.dumps({
        "sub": subject,
        "role": role,
        "exp": int(time.time()) + lifetime_seconds,
    }, separators=(",", ":"), sort_keys=True).encode())
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{_b64(signature)}"


def verify_access_token(token: str, secret: str) -> dict[str, str | int]:
    try:
        payload_part, signature_part = token.split(".", 1)
        expected = hmac.new(secret.encode(), payload_part.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(signature_part)):
            raise AuthenticationError("invalid access token")
        payload = json.loads(_unb64(payload_part))
        if not isinstance(payload, dict) or int(payload["exp"]) <= int(time.time()):
            raise AuthenticationError("expired access token")
        if not isinstance(payload.get("sub"), str) or not isinstance(payload.get("role"), str):
            raise AuthenticationError("invalid access token claims")
        return payload
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        if isinstance(exc, AuthenticationError):
            raise
        raise AuthenticationError("invalid access token") from exc


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
