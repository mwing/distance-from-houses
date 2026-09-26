import hashlib
import hmac
import secrets
import time
from pathlib import Path

COOKIE = "househunt_session"
SESSION_SECONDS = 30 * 24 * 3600


def load_secret(data_dir: Path) -> bytes:
    path = data_dir / "session.key"
    if path.exists():
        return path.read_bytes()
    data_dir.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    path.write_bytes(key)
    path.chmod(0o600)
    return key


class Auth:
    def __init__(self, password: str | None, secret: bytes):
        self.enabled = password is not None
        self._password_digest = hashlib.sha256(password.encode()).digest() if password else b""
        self._secret = secret

    def check_password(self, password: str) -> bool:
        return self.enabled and hmac.compare_digest(hashlib.sha256(password.encode()).digest(), self._password_digest)

    def _sign(self, expires: int) -> str:
        return hmac.new(self._secret, f"session:{expires}".encode(), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        expires = int(time.time()) + SESSION_SECONDS
        return f"{expires}.{self._sign(expires)}"

    def valid(self, token: str | None) -> bool:
        if not self.enabled:
            return True
        if not token or "." not in token:
            return False
        expires_s, sig = token.split(".", 1)
        if not expires_s.isdigit() or int(expires_s) < time.time():
            return False
        return hmac.compare_digest(sig, self._sign(int(expires_s)))
