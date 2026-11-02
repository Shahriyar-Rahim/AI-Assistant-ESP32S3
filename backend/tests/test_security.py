import pytest

from app.core.security import (
    AuthenticationError,
    hash_password,
    issue_access_token,
    verify_access_token,
    verify_password,
)


def test_password_hash_and_verification() -> None:
    encoded = hash_password("a-long-test-password")
    assert encoded != "a-long-test-password"
    assert verify_password("a-long-test-password", encoded)
    assert not verify_password("incorrect-password", encoded)
    with pytest.raises(ValueError):
        hash_password("short")


def test_signed_access_token_rejects_tampering() -> None:
    token = issue_access_token("user-1", "admin", "signing-secret-at-least-thirty-two-characters", 60)
    assert verify_access_token(token, "signing-secret-at-least-thirty-two-characters")["sub"] == "user-1"
    with pytest.raises(AuthenticationError):
        verify_access_token(token + "x", "signing-secret-at-least-thirty-two-characters")
