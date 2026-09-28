import json
import sqlite3
import threading
from pathlib import Path


class Cache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        # The geocode endpoint and the run worker write this file on separate connections; without WAL they block each other.
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

    def delete_namespaces(self, prefix: str, keep: set[str]) -> None:
        with self._lock:
            # A range, not LIKE: LIKE is case-insensitive and can't use the primary-key index.
            upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
            rows = self._db.execute("SELECT DISTINCT ns FROM kv WHERE ns >= ? AND ns < ?", (prefix, upper)).fetchall()
            for (ns,) in rows:
                if ns not in keep:
                    self._db.execute("DELETE FROM kv WHERE ns = ?", (ns,))
            self._db.commit()

    def set(self, ns: str, key: str, value) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?, ?)", (ns, key, json.dumps(value)))
            self._db.commit()


def coord_key(lat: float, lon: float) -> str:
    return f"{lat:.3f},{lon:.3f}"
