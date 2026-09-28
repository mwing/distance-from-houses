import datetime as dt
import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import config_from_dict
from ..pipeline import run as run_pipeline
from ..progress import Cancelled, Progress
from ..report import build_payload, summary
from .store import Store

log = logging.getLogger(__name__)
HELSINKI = ZoneInfo("Europe/Helsinki")
# A run that crashes the process would otherwise be retried after every restart.
MAX_SCHEDULED_RETRIES = 1


@dataclass
class ServerSettings:
    data_dir: Path
    digitransit_api_key: str | None
    digitransit_rps: float = 2.0
    fetch_cache_hours: float = 6.0
    default_daily_runs: int = 5
    default_max_listings: int = 300

    @property
    def cache_path(self) -> Path:
        return self.data_dir / "cache.sqlite"


class AlreadyRunning(Exception):
    def __init__(self, run: dict, message: str = "This search is already running"):
        super().__init__(message)
        self.run = run


class QuotaExceeded(Exception):
    pass


def local_midnight(now: dt.datetime | None = None) -> dt.datetime:
    now = (now or dt.datetime.now(HELSINKI)).astimezone(HELSINKI)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


class JobManager:
    """Single worker by design: the external APIs' rate limits assume one client."""

    def __init__(self, store: Store, settings: ServerSettings):
        self.store = store
        self.settings = settings
        self._queue: queue.Queue[int] = queue.Queue()
        self._progress: dict[int, Progress] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        interrupted = self.store.mark_interrupted()
        if interrupted:
            log.warning("Marked %d unfinished runs as interrupted", interrupted)
        for target, name in ((self._worker, "househunt-worker"), (self._scheduler, "househunt-scheduler")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for p in self._progress.values():
                p.cancel.set()

    def daily_limit(self, user: dict) -> int | None:
        if user["role"] == "admin":
            return None
        return user["daily_run_limit"] if user["daily_run_limit"] is not None else self.settings.default_daily_runs

    def listing_cap(self, user: dict | None) -> int | None:
        if not user or user["role"] == "admin":
            return None
        return user["max_listings"] if user["max_listings"] is not None else self.settings.default_max_listings

    def enqueue(self, profile_id: int, trigger: str = "manual", user: dict | None = None) -> dict:
        with self._lock:
            active = self.store.active_run(profile_id)
            if active:
                raise AlreadyRunning(active)
            if user and trigger == "manual":
                other = self.store.user_active_run(user["id"])
                if other:
                    raise AlreadyRunning(other, "Another of your searches is running; wait for it to finish")
                limit = self.daily_limit(user)
                if limit is not None and self.store.manual_runs_since(user["id"], local_midnight().isoformat()) >= limit:
                    raise QuotaExceeded(f"Daily limit of {limit} runs reached; scheduled refreshes still happen")
            run = self.store.create_run(profile_id, trigger)
        self._queue.put(run["id"])
        return run

    def cancel(self, run_id: int) -> bool:
        if self.store.cancel_queued(run_id):
            return True
        with self._lock:
            p = self._progress.get(run_id)
        if p:
            p.cancel.set()
            return True
        return False

    def live_progress(self, run_id: int) -> dict | None:
        with self._lock:
            p = self._progress.get(run_id)
        return p.snapshot() if p else None

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                run_id = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            run = self.store.get_run(run_id)
            if not run or run["status"] != "queued":
                continue
            self._execute(run)

    def _execute(self, run: dict) -> None:
        run_id = run["id"]
        profile = self.store.get_profile(run["profile_id"])
        if not profile:
            self.store.finish_run(run_id, "failed", error="Profile was deleted")
            return
        last_saved = [0.0]

        def listener(snap: dict) -> None:
            now = time.monotonic()
            if now - last_saved[0] >= 2:
                last_saved[0] = now
                self.store.save_progress(run_id, snap)

        progress = Progress(listener)
        with self._lock:
            self._progress[run_id] = progress
        if not self.store.mark_running(run_id):
            with self._lock:
                self._progress.pop(run_id, None)
            return
        log.info("Run %d for profile %r started (%s)", run_id, profile["name"], run["trigger"])
        try:
            cfg = config_from_dict(profile["settings"], env_api_key=False)
            cfg.cache_path = self.settings.cache_path
            cfg.transit.api_key = self.settings.digitransit_api_key
            cfg.transit.requests_per_second = self.settings.digitransit_rps
            owner = self.store.get_user(profile["user_id"]) if profile["user_id"] else None
            cap = self.listing_cap(owner)
            if cap is not None:
                cfg.max_listings = min(cfg.max_listings, cap)
            results = run_pipeline(cfg, progress, fetch_cache_hours=self.settings.fetch_cache_hours)
            payload = build_payload(results, cfg.destinations, cfg.map)
            self.store.apply_history(profile["id"], payload)
            self.store.save_progress(run_id, progress.snapshot())
            self.store.finish_run(run_id, "done", payload=payload, summary=summary(payload))
            log.info("Run %d done: %s", run_id, summary(payload))
        except Cancelled:
            if self._stop.is_set():
                self.store.finish_run(run_id, "interrupted", error="Server stopped during the run")
                log.info("Run %d interrupted by shutdown", run_id)
            else:
                self.store.finish_run(run_id, "cancelled", error="Cancelled")
                log.info("Run %d cancelled", run_id)
        except Exception as e:
            log.exception("Run %d failed", run_id)
            self.store.finish_run(run_id, "failed", error=str(e) or e.__class__.__name__)
        finally:
            with self._lock:
                self._progress.pop(run_id, None)

    def _scheduler(self) -> None:
        while not self._stop.wait(60):
            try:
                self.schedule_due()
            except Exception:
                log.exception("Scheduler check failed")

    def schedule_due(self, now: dt.datetime | None = None) -> list[int]:
        now = (now or dt.datetime.now(HELSINKI)).astimezone(HELSINKI)
        started = []
        for p in self.store.all_profiles():
            if not p["refresh_daily"]:
                continue
            hour, minute = (int(x) for x in p["refresh_at"].split(":"))
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if now < due:
                continue
            last = self.store.last_scheduled_at(p["id"])
            # Once per local day, so moving refresh_at later after a run can't buy another one.
            if last and dt.datetime.fromisoformat(last) >= local_midnight(now):
                continue
            if self.store.user_active_run(p["user_id"]):
                continue
            if self.store.interrupted_scheduled_since(p["id"], due.isoformat()) > MAX_SCHEDULED_RETRIES:
                continue
            try:
                started.append(self.enqueue(p["id"], "scheduled")["id"])
            except AlreadyRunning:
                continue
        return started
