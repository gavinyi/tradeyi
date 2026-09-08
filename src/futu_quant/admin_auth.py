from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time


_attempts: dict[str, tuple[int, float]] = {}


def configured() -> bool:
    return bool(os.getenv("ADMIN_PASSWORD_HASH", "").strip())


def verify(password: str) -> bool:
    expected = os.getenv("ADMIN_PASSWORD_HASH", "").strip().lower()
    if expected.startswith("pbkdf2_sha256$"):
        _, iterations, salt, digest = expected.split("$", 3)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), int(iterations)
        ).hex()
        return hmac.compare_digest(actual, digest)
    actual = hashlib.sha256(password.encode()).hexdigest()
    return bool(expected) and hmac.compare_digest(actual, expected)


def hash_password(password: str, iterations: int = 600_000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations).hex()
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest}"


def login_allowed(client_key: str) -> tuple[bool, int]:
    failures, locked_until = _attempts.get(client_key, (0, 0.0))
    remaining = max(0, int(locked_until - time.time()))
    if remaining <= 0 and locked_until:
        _attempts.pop(client_key, None)
    return remaining == 0, remaining


def record_login(client_key: str, success: bool) -> None:
    if success:
        _attempts.pop(client_key, None)
        return
    failures, locked_until = _attempts.get(client_key, (0, 0.0))
    if locked_until > time.time():
        return
    failures += 1
    maximum = max(2, int(os.getenv("ADMIN_MAX_LOGIN_ATTEMPTS", "5")))
    if failures >= maximum:
        _attempts[client_key] = (0, time.time() + max(30, int(os.getenv("ADMIN_LOCKOUT_SECONDS", "300"))))
    else:
        _attempts[client_key] = (failures, 0.0)
