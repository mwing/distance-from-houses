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
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
  secret_hash TEXT NOT NULL UNIQUE,
  disabled INTEGER NOT NULL DEFAULT 0,
  daily_run_limit INTEGER,
  max_listings INTEGER,
  created_at TEXT NOT NULL,
  last_seen_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash TEXT NOT NULL UNIQUE,
  label TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT,
  used_by INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS run_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  trigger TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS run_log_user ON run_log(user_id, created_at);
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
            self._migrate()
            self._db.commit()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(profiles)")}
        if "user_id" not in cols:
            self._db.execute("ALTER TABLE profiles ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE")
        self._db.execute("CREATE INDEX IF NOT EXISTS profiles_user ON profiles(user_id)")

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

    @staticmethod
    def _profile(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "name": row["name"],
            "settings": json.loads(row["settings"]),
            "refresh_daily": bool(row["refresh_daily"]),
            "refresh_at": row["refresh_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_profiles(self, user_id: int) -> list[dict]:
        rows = self._all("SELECT * FROM profiles WHERE user_id = ? ORDER BY name COLLATE NOCASE", (user_id,))
        profiles = [self._profile(r) for r in rows]
        for p in profiles:
            p["last_run"] = self.last_run(p["id"])
        return profiles

    def all_profiles(self) -> list[dict]:
        rows = self._all("SELECT p.* FROM profiles p JOIN users u ON u.id = p.user_id WHERE u.disabled = 0")
        return [self._profile(r) for r in rows]

    def get_profile(self, profile_id: int, user_id: int | None = None) -> dict | None:
        """With user_id, returns None for another user's profile; without, for internal callers only."""
        if user_id is None:
            row = self._one("SELECT * FROM profiles WHERE id = ?", (profile_id,))
        else:
            row = self._one("SELECT * FROM profiles WHERE id = ? AND user_id = ?", (profile_id, user_id))
        return self._profile(row) if row else None

    def create_profile(self, user_id: int | None, name: str, settings: dict, refresh_daily: bool, refresh_at: str) -> dict:
        ts = now_iso()
        cur = self._exec(
            "INSERT INTO profiles (user_id, name, settings, refresh_daily, refresh_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, name, json.dumps(settings, ensure_ascii=False), int(refresh_daily), refresh_at, ts, ts),
        )
        return self.get_profile(cur.lastrowid)

    def update_profile(self, profile_id: int, user_id: int, name: str, settings: dict, refresh_daily: bool,
                       refresh_at: str) -> dict | None:
        self._exec(
            "UPDATE profiles SET name = ?, settings = ?, refresh_daily = ?, refresh_at = ?, updated_at = ? "
            "WHERE id = ? AND user_id = ?",
            (name, json.dumps(settings, ensure_ascii=False), int(refresh_daily), refresh_at, now_iso(), profile_id, user_id),
        )
        return self.get_profile(profile_id, user_id)

    def delete_profile(self, profile_id: int, user_id: int) -> bool:
        return self._exec("DELETE FROM profiles WHERE id = ? AND user_id = ?", (profile_id, user_id)).rowcount > 0

    # Users are identified by their password alone, so secret_hash is the lookup key.

    @staticmethod
    def _user(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "label": row["label"],
            "role": row["role"],
            "disabled": bool(row["disabled"]),
            "daily_run_limit": row["daily_run_limit"],
            "max_listings": row["max_listings"],
            "created_at": row["created_at"],
            "last_seen_at": row["last_seen_at"],
        }

    def ensure_admin(self, secret_hash: str) -> dict:
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM users WHERE role = 'admin' ORDER BY id LIMIT 1").fetchone()
            if row is None:
                cur = self._db.execute(
                    "INSERT INTO users (label, role, secret_hash, created_at) VALUES ('Admin', 'admin', ?, ?)",
                    (secret_hash, now_iso()),
                )
                admin_id = cur.lastrowid
            else:
                admin_id = row["id"]
                if row["secret_hash"] != secret_hash:
                    self._db.execute("UPDATE users SET secret_hash = ? WHERE id = ?", (secret_hash, admin_id))
                    self._db.execute("DELETE FROM sessions WHERE user_id = ?", (admin_id,))
            self._db.execute("UPDATE profiles SET user_id = ? WHERE user_id IS NULL", (admin_id,))
            if self._db.execute("SELECT COUNT(*) FROM run_log").fetchone()[0] == 0:
                self._db.execute(
                    "INSERT INTO run_log (user_id, trigger, created_at) SELECT p.user_id, r.trigger, r.created_at "
                    "FROM runs r JOIN profiles p ON p.id = r.profile_id WHERE p.user_id IS NOT NULL"
                )
        return self.get_user(admin_id)

    def admin_id(self) -> int | None:
        row = self._one("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")
        return row["id"] if row else None

    def get_user(self, user_id: int) -> dict | None:
        row = self._one("SELECT * FROM users WHERE id = ?", (user_id,))
        return self._user(row) if row else None

    def user_by_secret(self, secret_hash: str) -> dict | None:
        row = self._one("SELECT * FROM users WHERE secret_hash = ? AND disabled = 0", (secret_hash,))
        return self._user(row) if row else None

    def create_user(self, label: str, secret_hash: str, daily_run_limit: int | None, max_listings: int | None) -> dict:
        cur = self._exec(
            "INSERT INTO users (label, role, secret_hash, daily_run_limit, max_listings, created_at) "
            "VALUES (?, 'user', ?, ?, ?, ?)",
            (label, secret_hash, daily_run_limit, max_listings, now_iso()),
        )
        return self.get_user(cur.lastrowid)

    def list_users(self) -> list[dict]:
        users = [self._user(r) for r in self._all("SELECT * FROM users ORDER BY role, label COLLATE NOCASE")]
        for u in users:
            u["searches"] = self._one("SELECT COUNT(*) AS n FROM profiles WHERE user_id = ?", (u["id"],))["n"]
        return users

    def update_user(self, user_id: int, **fields) -> dict | None:
        allowed = {"label", "disabled", "daily_run_limit", "max_listings", "secret_hash"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if sets:
            cols = ", ".join(f"{k} = ?" for k in sets)
            self._exec(f"UPDATE users SET {cols} WHERE id = ?", (*sets.values(), user_id))
        if sets.get("disabled") or "secret_hash" in sets:
            self.delete_sessions(user_id)
        return self.get_user(user_id)

    def delete_user(self, user_id: int) -> bool:
        return self._exec("DELETE FROM users WHERE id = ? AND role != 'admin'", (user_id,)).rowcount > 0

    def touch_user(self, user_id: int) -> None:
        self._exec("UPDATE users SET last_seen_at = ? WHERE id = ?", (now_iso(), user_id))

    def create_session(self, token_hash: str, user_id: int, expires_at: str) -> None:
        ts = now_iso()
        self._exec(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
            (token_hash, user_id, ts, expires_at, ts),
        )

    def session_user(self, token_hash: str) -> dict | None:
        row = self._one(
            "SELECT u.*, s.last_seen_at AS session_seen FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.expires_at > ? AND u.disabled = 0",
            (token_hash, now_iso()),
        )
        if not row:
            return None
        user = self._user(row)
        user["session_seen"] = row["session_seen"]
        return user

    def touch_session(self, token_hash: str) -> None:
        self._exec("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (now_iso(), token_hash))

    def delete_session(self, token_hash: str) -> None:
        self._exec("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def delete_sessions(self, user_id: int) -> None:
        self._exec("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def prune_sessions(self) -> None:
        self._exec("DELETE FROM sessions WHERE expires_at <= ?", (now_iso(),))

    def create_invite(self, token_hash: str, label: str, expires_at: str) -> dict:
        cur = self._exec(
            "INSERT INTO invites (token_hash, label, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token_hash, label, now_iso(), expires_at),
        )
        return self._invite(self._one("SELECT * FROM invites WHERE id = ?", (cur.lastrowid,)))

    @staticmethod
    def _invite(row: sqlite3.Row) -> dict:
        return {k: row[k] for k in ("id", "label", "created_at", "expires_at", "used_at", "used_by")}

    def pending_invites(self) -> list[dict]:
        rows = self._all("SELECT * FROM invites WHERE used_at IS NULL AND expires_at > ? ORDER BY id DESC", (now_iso(),))
        return [self._invite(r) for r in rows]

    def open_invite(self, token_hash: str) -> dict | None:
        row = self._one(
            "SELECT * FROM invites WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?", (token_hash, now_iso())
        )
        return self._invite(row) if row else None

    def redeem_invite(self, token_hash: str, secret_hash: str, daily_run_limit: int | None,
                      max_listings: int | None) -> dict | None:
        """Creates the user and consumes the invite atomically."""
        with self._lock, self._db:
            ts = now_iso()
            row = self._db.execute(
                "SELECT * FROM invites WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?", (token_hash, ts)
            ).fetchone()
            if not row:
                return None
            cur = self._db.execute(
                "INSERT INTO users (label, role, secret_hash, daily_run_limit, max_listings, created_at) "
                "VALUES (?, 'user', ?, ?, ?, ?)",
                (row["label"], secret_hash, daily_run_limit, max_listings, ts),
            )
            self._db.execute("UPDATE invites SET used_at = ?, used_by = ? WHERE id = ?", (ts, cur.lastrowid, row["id"]))
            user_id = cur.lastrowid
        return self.get_user(user_id)

    def delete_invite(self, invite_id: int) -> bool:
        return self._exec("DELETE FROM invites WHERE id = ? AND used_at IS NULL", (invite_id,)).rowcount > 0

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
        ts = now_iso()
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO runs (profile_id, trigger, status, created_at) VALUES (?, ?, 'queued', ?)",
                (profile_id, trigger, ts),
            )
            # Kept apart from runs, which cascade with their profile: deleting a search must not reset quotas.
            self._db.execute(
                "INSERT INTO run_log (user_id, trigger, created_at) SELECT user_id, ?, ? FROM profiles "
                "WHERE id = ? AND user_id IS NOT NULL",
                (trigger, ts, profile_id),
            )
            run_id = cur.lastrowid
        return self.get_run(run_id)

    def get_run(self, run_id: int, user_id: int | None = None) -> dict | None:
        if user_id is None:
            row = self._one("SELECT * FROM runs WHERE id = ?", (run_id,))
        else:
            row = self._one(
                "SELECT r.* FROM runs r JOIN profiles p ON p.id = r.profile_id WHERE r.id = ? AND p.user_id = ?",
                (run_id, user_id),
            )
        return self._run(row) if row else None

    def user_active_run(self, user_id: int) -> dict | None:
        row = self._one(
            "SELECT r.* FROM runs r JOIN profiles p ON p.id = r.profile_id "
            "WHERE p.user_id = ? AND r.status IN ('queued', 'running') ORDER BY r.id DESC LIMIT 1",
            (user_id,),
        )
        return self._run(row) if row else None

    def manual_runs_since(self, user_id: int, since_iso: str) -> int:
        rows = self._all("SELECT created_at FROM run_log WHERE user_id = ? AND trigger = 'manual'", (user_id,))
        since = dt.datetime.fromisoformat(since_iso)
        return sum(1 for r in rows if dt.datetime.fromisoformat(r["created_at"]) >= since)

    def queue(self) -> list[dict]:
        rows = self._all(
            "SELECT r.*, u.label AS user_label FROM runs r JOIN profiles p ON p.id = r.profile_id "
            "JOIN users u ON u.id = p.user_id WHERE r.status IN ('queued', 'running') ORDER BY r.id"
        )
        return [{**self._run(r), "user_label": r["user_label"]} for r in rows]

    def last_run(self, profile_id: int) -> dict | None:
        row = self._one("SELECT * FROM runs WHERE profile_id = ? ORDER BY id DESC LIMIT 1", (profile_id,))
        return self._run(row) if row else None

    def active_run(self, profile_id: int) -> dict | None:
        row = self._one(
            "SELECT * FROM runs WHERE profile_id = ? AND status IN ('queued', 'running') ORDER BY id DESC LIMIT 1",
            (profile_id,),
        )
        return self._run(row) if row else None

    def mark_running(self, run_id: int) -> bool:
        cur = self._exec(
            "UPDATE runs SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'", (now_iso(), run_id)
        )
        return cur.rowcount > 0

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

    def count_profiles(self, user_id: int, refresh_only: bool = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM profiles WHERE user_id = ?" + (" AND refresh_daily = 1" if refresh_only else "")
        return self._one(sql, (user_id,))["n"]

    def last_scheduled_at(self, profile_id: int) -> str | None:
        # Interrupted runs don't count, so a restart during a scheduled run retries it the same day.
        row = self._one(
            "SELECT MAX(created_at) AS t FROM runs WHERE profile_id = ? AND trigger = 'scheduled' AND status != 'interrupted'",
            (profile_id,),
        )
        return row["t"] if row else None

    def interrupted_scheduled_since(self, profile_id: int, since_iso: str) -> int:
        rows = self._all(
            "SELECT created_at FROM runs WHERE profile_id = ? AND trigger = 'scheduled' AND status = 'interrupted'",
            (profile_id,),
        )
        since = dt.datetime.fromisoformat(since_iso)
        return sum(1 for r in rows if dt.datetime.fromisoformat(r["created_at"]) >= since)

    def apply_history(self, profile_id: int, payload: dict, now: dt.datetime | None = None) -> None:
        """Mutates payload: adds first_seen and badges to each row and a first_seen column."""
        now = now or dt.datetime.now(dt.timezone.utc)
        ts = now.isoformat(timespec="microseconds")
        with self._lock, self._db:
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
                    existing[key] = (first_seen, history)
                row["first_seen"] = first_seen[:10]
                row["badges"] = _badges(first_seen, history, first_batch, now)
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
