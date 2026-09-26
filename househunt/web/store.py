import copy
import datetime as dt
import json
import sqlite3
import threading
from pathlib import Path

from ..config import config_from_dict

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  settings TEXT NOT NULL,
  refresh_daily INTEGER NOT NULL DEFAULT 0,
  refresh_at TEXT NOT NULL DEFAULT '06:00',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  trigger TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  progress TEXT,
  error TEXT,
  summary TEXT,
  result TEXT
);
CREATE INDEX IF NOT EXISTS runs_profile ON runs(profile_id, id);
CREATE TABLE IF NOT EXISTS listings (
  profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  price_history TEXT NOT NULL,
  PRIMARY KEY (profile_id, key)
);
"""

# Machine-wide settings the web app owns; a profile must not override them.
SERVER_KEYS = {"cache_path", "output_dir"}
SERVER_TRANSIT_KEYS = {"api_key", "requests_per_second"}
ACTIVE = ("queued", "running")
KEEP_RESULTS = 5
NEW_DAYS = 3
PRICE_CHANGE_DAYS = 14


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def sanitize_settings(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("settings must be an object")
    s = copy.deepcopy(raw)
    for k in SERVER_KEYS:
        s.pop(k, None)
    if isinstance(s.get("transit"), dict):
        for k in SERVER_TRANSIT_KEYS:
            s["transit"].pop(k, None)
    config_from_dict(s, env_api_key=False)
    return s


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)
            self._db.commit()

    def _exec(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    def _all(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def _one(self, sql: str, params=()) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, params).fetchone()

    # Profiles

    @staticmethod
    def _profile(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "settings": json.loads(row["settings"]),
            "refresh_daily": bool(row["refresh_daily"]),
            "refresh_at": row["refresh_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_profiles(self) -> list[dict]:
        profiles = [self._profile(r) for r in self._all("SELECT * FROM profiles ORDER BY name COLLATE NOCASE")]
        for p in profiles:
            p["last_run"] = self.last_run(p["id"])
        return profiles

    def get_profile(self, profile_id: int) -> dict | None:
        row = self._one("SELECT * FROM profiles WHERE id = ?", (profile_id,))
        return self._profile(row) if row else None

    def create_profile(self, name: str, settings: dict, refresh_daily: bool, refresh_at: str) -> dict:
        ts = now_iso()
        cur = self._exec(
            "INSERT INTO profiles (name, settings, refresh_daily, refresh_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (name, json.dumps(settings, ensure_ascii=False), int(refresh_daily), refresh_at, ts, ts),
        )
        return self.get_profile(cur.lastrowid)

    def update_profile(self, profile_id: int, name: str, settings: dict, refresh_daily: bool, refresh_at: str) -> dict | None:
        self._exec(
            "UPDATE profiles SET name = ?, settings = ?, refresh_daily = ?, refresh_at = ?, updated_at = ? WHERE id = ?",
            (name, json.dumps(settings, ensure_ascii=False), int(refresh_daily), refresh_at, now_iso(), profile_id),
        )
        return self.get_profile(profile_id)

    def delete_profile(self, profile_id: int) -> bool:
        return self._exec("DELETE FROM profiles WHERE id = ?", (profile_id,)).rowcount > 0

    # Runs

    @staticmethod
    def _run(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "profile_id": row["profile_id"],
            "trigger": row["trigger"],
            "status": row["status"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "progress": json.loads(row["progress"]) if row["progress"] else None,
            "error": row["error"],
            "summary": row["summary"],
        }

    def create_run(self, profile_id: int, trigger: str) -> dict:
        cur = self._exec(
            "INSERT INTO runs (profile_id, trigger, status, created_at) VALUES (?, ?, 'queued', ?)",
            (profile_id, trigger, now_iso()),
        )
        return self.get_run(cur.lastrowid)

    def get_run(self, run_id: int) -> dict | None:
        row = self._one("SELECT * FROM runs WHERE id = ?", (run_id,))
        return self._run(row) if row else None

    def last_run(self, profile_id: int) -> dict | None:
        row = self._one("SELECT * FROM runs WHERE profile_id = ? ORDER BY id DESC LIMIT 1", (profile_id,))
        return self._run(row) if row else None

    def active_run(self, profile_id: int) -> dict | None:
        row = self._one(
            "SELECT * FROM runs WHERE profile_id = ? AND status IN ('queued', 'running') ORDER BY id DESC LIMIT 1",
            (profile_id,),
        )
        return self._run(row) if row else None

    def mark_running(self, run_id: int) -> None:
        self._exec("UPDATE runs SET status = 'running', started_at = ? WHERE id = ?", (now_iso(), run_id))

    def save_progress(self, run_id: int, progress: dict) -> None:
        self._exec("UPDATE runs SET progress = ? WHERE id = ?", (json.dumps(progress), run_id))

    def finish_run(self, run_id: int, status: str, error: str | None = None, payload: dict | None = None,
                   summary: str | None = None) -> None:
        self._exec(
            "UPDATE runs SET status = ?, finished_at = ?, error = ?, summary = ?, result = ? WHERE id = ?",
            (status, now_iso(), error, summary, json.dumps(payload, ensure_ascii=False) if payload else None, run_id),
        )
        if payload:
            row = self._one("SELECT profile_id FROM runs WHERE id = ?", (run_id,))
            self._exec(
                """UPDATE runs SET result = NULL WHERE profile_id = ? AND result IS NOT NULL AND id NOT IN
                   (SELECT id FROM runs WHERE profile_id = ? AND result IS NOT NULL ORDER BY id DESC LIMIT ?)""",
                (row["profile_id"], row["profile_id"], KEEP_RESULTS),
            )

    def cancel_queued(self, run_id: int) -> bool:
        cur = self._exec(
            "UPDATE runs SET status = 'cancelled', finished_at = ? WHERE id = ? AND status = 'queued'", (now_iso(), run_id)
        )
        return cur.rowcount > 0

    def mark_interrupted(self) -> int:
        cur = self._exec(
            "UPDATE runs SET status = 'interrupted', finished_at = ?, error = 'Server restarted during the run' "
            "WHERE status IN ('queued', 'running')",
            (now_iso(),),
        )
        return cur.rowcount

    def latest_result(self, profile_id: int) -> tuple[dict, dict] | None:
        row = self._one(
            "SELECT * FROM runs WHERE profile_id = ? AND status = 'done' AND result IS NOT NULL ORDER BY id DESC LIMIT 1",
            (profile_id,),
        )
        return (self._run(row), json.loads(row["result"])) if row else None

    def last_run_at(self, profile_id: int, trigger: str | None = None) -> str | None:
        if trigger:
            row = self._one("SELECT MAX(created_at) AS t FROM runs WHERE profile_id = ? AND trigger = ?", (profile_id, trigger))
        else:
            row = self._one("SELECT MAX(created_at) AS t FROM runs WHERE profile_id = ?", (profile_id,))
        return row["t"] if row else None

    # Listing history

    def apply_history(self, profile_id: int, payload: dict, now: dt.datetime | None = None) -> None:
        """Record first/last seen and price changes, and add badges and first_seen to the payload rows."""
        now = now or dt.datetime.now(dt.timezone.utc)
        ts = now.isoformat(timespec="microseconds")
        with self._lock:
            existing = {
                r["key"]: (r["first_seen"], json.loads(r["price_history"]))
                for r in self._db.execute("SELECT * FROM listings WHERE profile_id = ?", (profile_id,))
            }
            first_batch = min((fs for fs, _ in existing.values()), default=None)
            for row in payload["rows"]:
                key, price = row["key"], row["price_eur"]
                if key in existing:
                    first_seen, history = existing[key]
                    if price is not None and (not history or history[-1][1] != price):
                        history.append([ts, price])
                    self._db.execute(
                        "UPDATE listings SET last_seen = ?, price_history = ? WHERE profile_id = ? AND key = ?",
                        (ts, json.dumps(history), profile_id, key),
                    )
                else:
                    first_seen, history = ts, ([[ts, price]] if price is not None else [])
                    self._db.execute(
                        "INSERT INTO listings (profile_id, key, first_seen, last_seen, price_history) VALUES (?, ?, ?, ?, ?)",
                        (profile_id, key, ts, ts, json.dumps(history)),
                    )
                row["first_seen"] = first_seen[:10]
                row["badges"] = _badges(first_seen, history, first_batch, now)
            self._db.commit()
        if "first_seen" not in payload["columns"]:
            payload["columns"].append("first_seen")


def _badges(first_seen: str, history: list, first_batch: str | None, now: dt.datetime) -> list[dict]:
    badges = []
    seen = dt.datetime.fromisoformat(first_seen)
    if first_batch is not None and first_seen > first_batch and now - seen <= dt.timedelta(days=NEW_DAYS):
        badges.append({"kind": "new", "label": "new"})
    if len(history) >= 2:
        (_, before), (changed_at, after) = history[-2], history[-1]
        if now - dt.datetime.fromisoformat(changed_at) <= dt.timedelta(days=PRICE_CHANGE_DAYS):
            diff = after - before
            kind = "price_down" if diff < 0 else "price_up"
            sign = "−" if diff < 0 else "+"
            badges.append({"kind": kind, "label": f"price {sign}{abs(diff):,.0f} €".replace(",", " ")})
    return badges
