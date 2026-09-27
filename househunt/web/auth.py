import datetime as dt
import hashlib
import hmac
import os
import secrets
import threading
import time
from pathlib import Path

COOKIE = "househunt_session"
SESSION_DAYS = 30
SESSION_SECONDS = SESSION_DAYS * 24 * 3600
INVITE_DAYS = 7
KEY_BYTES = 32
MAX_FAILURES = 5
FAILURE_WINDOW = 60
LOCKOUT_SECONDS = 60
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
PASSWORD_GROUPS = 5


def load_secret(data_dir: Path) -> bytes:
    path = data_dir / "session.key"
    if path.exists():
        key = path.read_bytes()
        if len(key) >= KEY_BYTES:
            return key
        path.unlink()
    data_dir.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(KEY_BYTES)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


class Hasher:
    def __init__(self, secret: bytes):
        self._secret = secret

    def password(self, password: str) -> str:
        # Keyed so a leaked database can't be checked against guesses offline; plain HMAC
        # is enough because user passwords are generated with ~100 bits of entropy.
        return hmac.new(self._secret, b"password:" + password.strip().encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()


def generate_password() -> str:
    groups = ("".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(4)) for _ in range(PASSWORD_GROUPS))
    return "-".join(groups)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def expiry(days: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).isoformat(timespec="seconds")


class LoginThrottle:
    """Process-wide, not per client: any password is a valid guess against every account."""

    def __init__(self):
        self._failures: list[float] = []
        self._locked_until = 0.0
        self._lock = threading.Lock()

    def locked(self) -> bool:
        with self._lock:
            return time.monotonic() < self._locked_until

    def failed(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._failures = [t for t in self._failures if now - t < FAILURE_WINDOW] + [now]
            if len(self._failures) >= MAX_FAILURES:
                self._locked_until = now + LOCKOUT_SECONDS
                self._failures = []
