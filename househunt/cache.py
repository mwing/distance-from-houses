import json
import sqlite3
import threading
from pathlib import Path


class Cache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        # The web app's geocode endpoint and the run worker open separate connections to this file.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS kv (ns TEXT, key TEXT, value TEXT, PRIMARY KEY (ns, key))")
        self._lock = threading.Lock()

    def get(self, ns: str, key: str):
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE ns = ? AND key = ?", (ns, key)).fetchone()
        return json.loads(row[0]) if row else None

    def has(self, ns: str, key: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM kv WHERE ns = ? AND key = ?", (ns, key)).fetchone() is not None

    def set(self, ns: str, key: str, value) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?, ?)", (ns, key, json.dumps(value)))
            self._db.commit()


def coord_key(lat: float, lon: float) -> str:
    return f"{lat:.3f},{lon:.3f}"
