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


@dataclass
class ServerSettings:
    data_dir: Path
    digitransit_api_key: str | None
    digitransit_rps: float = 2.0

    @property
    def cache_path(self) -> Path:
        return self.data_dir / "cache.sqlite"


class AlreadyRunning(Exception):
    def __init__(self, run: dict):
        self.run = run


class JobManager:
    """Runs searches one at a time in a background thread, so the external services see one client."""

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

    def enqueue(self, profile_id: int, trigger: str = "manual") -> dict:
        with self._lock:
            active = self.store.active_run(profile_id)
            if active:
                raise AlreadyRunning(active)
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
        self.store.mark_running(run_id)
        log.info("Run %d for profile %r started (%s)", run_id, profile["name"], run["trigger"])
        try:
            cfg = config_from_dict(profile["settings"], env_api_key=False)
            cfg.cache_path = self.settings.cache_path
            cfg.transit.api_key = self.settings.digitransit_api_key
            cfg.transit.requests_per_second = self.settings.digitransit_rps
            results = run_pipeline(cfg, progress)
            payload = build_payload(results, cfg.destinations, cfg.map)
            self.store.apply_history(profile["id"], payload)
            self.store.save_progress(run_id, progress.snapshot())
            self.store.finish_run(run_id, "done", payload=payload, summary=summary(payload))
            log.info("Run %d done: %s", run_id, summary(payload))
        except Cancelled:
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
        for p in self.store.list_profiles():
            if not p["refresh_daily"]:
                continue
            hour, minute = (int(x) for x in p["refresh_at"].split(":"))
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if now < due:
                continue
            last = self.store.last_run_at(p["id"], "scheduled")
            if last and dt.datetime.fromisoformat(last) >= due:
                continue
            try:
                started.append(self.enqueue(p["id"], "scheduled")["id"])
            except AlreadyRunning:
                continue
        return started
