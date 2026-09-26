import hashlib
import hmac
import os
import secrets
import threading
import time
from pathlib import Path

COOKIE = "househunt_session"
SESSION_SECONDS = 30 * 24 * 3600
KEY_BYTES = 32
MAX_FAILURES = 5
FAILURE_WINDOW = 60
LOCKOUT_SECONDS = 60


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


class LoginThrottle:
    """Process-wide, not per client: there is one shared password to guess."""

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


class Auth:
    def __init__(self, password: str | None, secret: bytes):
        self.enabled = password is not None
        self._password_digest = hashlib.sha256(password.encode()).digest() if password else b""
        self._secret = secret
        self.throttle = LoginThrottle()

    def check_password(self, password: str) -> bool:
        return self.enabled and hmac.compare_digest(hashlib.sha256(password.encode()).digest(), self._password_digest)

    def _sign(self, expires: int) -> str:
        # The password digest is part of the message so changing the password ends every session.
        msg = f"session:{expires}".encode() + self._password_digest
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def issue(self) -> str:
        expires = int(time.time()) + SESSION_SECONDS
        return f"{expires}.{self._sign(expires)}"

    def valid(self, token: str | None) -> bool:
        if not self.enabled:
            return True
        if not token or "." not in token:
            return False
        expires_s, sig = token.split(".", 1)
        if not (expires_s.isascii() and expires_s.isdigit() and sig.isascii()):
            return False
        if int(expires_s) < time.time():
            return False
        return hmac.compare_digest(sig, self._sign(int(expires_s)))
