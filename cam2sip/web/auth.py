"""Admin password hashing and signed session cookies (stdlib only)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

COOKIE = "cam2sip_session"
SESSION_TTL = 7 * 24 * 3600


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2 ** 14, r=8, p=1, dklen=32)
    return hmac.compare_digest(digest.hex(), digest_hex)


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_session(secret: str, pw_hash: str) -> str:
    exp = int(time.time()) + SESSION_TTL
    # binding the password hash means a password change logs out old sessions
    return f"{exp}.{_sign(secret, f'{exp}:{pw_hash}')}"


def check_session(token: str | None, secret: str, pw_hash: str) -> bool:
    if not token or "." not in token or not pw_hash:
        return False
    exp_s, sig = token.split(".", 1)
    if not exp_s.isdigit() or int(exp_s) < time.time():
        return False
    return hmac.compare_digest(sig, _sign(secret, f"{exp_s}:{pw_hash}"))
