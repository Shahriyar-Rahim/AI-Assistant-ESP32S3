from __future__ import annotations

import json

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings


class VaultUnavailable(RuntimeError):
    pass


def _cipher(settings: Settings) -> Fernet:
    if not settings.integration_encryption_key:
        raise VaultUnavailable("INTEGRATION_ENCRYPTION_KEY must be configured")
    try:
        return Fernet(settings.integration_encryption_key.encode())
    except (ValueError, TypeError) as exc:
        raise VaultUnavailable("INTEGRATION_ENCRYPTION_KEY must be a valid Fernet key") from exc


def encrypt_credentials(credentials: dict[str, object], settings: Settings) -> str:
    return _cipher(settings).encrypt(json.dumps(credentials, separators=(",", ":")).encode()).decode()


def decrypt_credentials(ciphertext: str, settings: Settings) -> dict[str, object]:
    try:
        value = json.loads(_cipher(settings).decrypt(ciphertext.encode()))
    except InvalidToken as exc:
        raise VaultUnavailable("stored integration credentials could not be decrypted") from exc
    if not isinstance(value, dict):
        raise VaultUnavailable("stored integration credentials are invalid")
    return value
