import threading
import time

import httpx

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
TOOL_UA = "househunt/0.1 (personal home search tool)"


def client(user_agent: str = BROWSER_UA, timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(headers={"User-Agent": user_agent}, timeout=timeout, follow_redirects=True)


class RateLimiter:
    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second if per_second > 0 else 0.0
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            time.sleep(delay)


def request_with_retry(send, attempts: int = 4) -> httpx.Response:
    for i in range(attempts):
        try:
            resp = send()
        except httpx.TransportError:
            if i == attempts - 1:
                raise
        else:
            if resp.status_code not in (429, 500, 502, 503, 504) or i == attempts - 1:
                return resp
        time.sleep(2**i)
    raise RuntimeError("unreachable")
