import logging
import sys
import threading
import time
from collections.abc import Callable


class Cancelled(Exception):
    pass


class Progress:
    def __init__(self, listener: Callable[[dict], None] | None = None, cancel: threading.Event | None = None):
        self.listener = listener
        self.cancel = cancel or threading.Event()
        self._lock = threading.Lock()
        self.phase = ""
        self.done = 0
        self.total = 0
        self.cached = 0
        self.message = ""
        self._started = time.monotonic()

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    def start(self, phase: str, total: int = 0, message: str = "") -> None:
        with self._lock:
            self.phase, self.total, self.done, self.cached, self.message = phase, total, 0, 0, message
            self._started = time.monotonic()
        self._emit()

    def advance(self, n: int = 1, cached: int = 0, message: str | None = None) -> None:
        with self._lock:
            self.done += n
            self.cached += cached
            if message is not None:
                self.message = message
        self._emit()

    def note(self, message: str) -> None:
        with self._lock:
            self.message = message
        self._emit()

    def snapshot(self) -> dict:
        with self._lock:
            elapsed = time.monotonic() - self._started
            fetched = self.done - self.cached
            rate = fetched / elapsed if elapsed >= 1 and fetched > 0 else None
            remaining = self.total - self.done if self.total else None
            eta = remaining / rate if rate and remaining else None
            return {
                "phase": self.phase,
                "done": self.done,
                "total": self.total,
                "cached": self.cached,
                "message": self.message,
                "rate": round(rate, 2) if rate else None,
                "eta_seconds": round(eta) if eta else None,
            }

    def _emit(self) -> None:
        if self.listener:
            self.listener(self.snapshot())


def _fmt_eta(seconds: int | None) -> str:
    if seconds is None:
        return ""
    m, s = divmod(int(seconds), 60)
    return f" ETA {m}m{s:02d}s" if m else f" ETA {s}s"


class TerminalProgress:
    def __init__(self, stream=sys.stderr):
        self.stream = stream
        self.enabled = stream.isatty()
        self._last = 0.0
        self._phase = None
        self._line = ""
        self._latest: dict | None = None

    def clear(self) -> None:
        if self.enabled and self._line:
            self.stream.write("\r\033[K")

    def redraw(self) -> None:
        if self.enabled and self._line:
            self.stream.write("\r\033[K" + self._line)
            self.stream.flush()

    def install_log_handler(self) -> None:
        root = logging.getLogger()
        for h in root.handlers:
            if isinstance(h, logging.StreamHandler) and getattr(h, "stream", None) is self.stream:
                h.emit = self._wrap_emit(h.emit)

    def _wrap_emit(self, emit):
        def wrapped(record):
            self.clear()
            emit(record)
            self.redraw()
        return wrapped

    @staticmethod
    def _render(snap: dict) -> str:
        if not snap["total"]:
            return f"{snap['phase']:<16} {snap['message']}"
        width = 24
        filled = int(width * snap["done"] / snap["total"])
        bar = "#" * filled + "-" * (width - filled)
        cached = f", {snap['cached']} cached" if snap["cached"] else ""
        rate = f" {snap['rate']}/s" if snap["rate"] else ""
        return f"{snap['phase']:<16} [{bar}] {snap['done']}/{snap['total']}{cached}{rate}{_fmt_eta(snap['eta_seconds'])}"

    def __call__(self, snap: dict) -> None:
        if not self.enabled:
            return
        if self._phase and snap["phase"] != self._phase:
            if self._latest and self._latest.get("message") or self._latest and self._latest["total"]:
                self.stream.write("\r\033[K" + self._render(self._latest) + "\n")
            else:
                self.clear()
            self._phase, self._line = None, ""
        self._latest = snap
        if snap["phase"] == "done":
            self.clear()
            self._line = ""
            self.stream.flush()
            return
        now = time.monotonic()
        if snap["phase"] == self._phase and now - self._last < 0.2:
            return
        self._phase, self._last = snap["phase"], now
        self._line = self._render(snap)
        self.stream.write("\r\033[K" + self._line)
        self.stream.flush()
