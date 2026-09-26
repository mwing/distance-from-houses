import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from ..cache import Cache
from ..config import ROUTERS, WEEKDAYS
from ..geocode import Geocoder
from ..models import HOUSE_TYPES, PLOT_OWNERSHIP, UUSIMAA_MUNICIPALITIES
from ..report import csv_text
from .auth import COOKIE, SESSION_SECONDS, Auth, load_secret
from .jobs import AlreadyRunning, JobManager, ServerSettings
from .store import Store, sanitize_settings

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
OPEN_API_PATHS = {"/api/login", "/api/logout", "/api/session"}

CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://unpkg.com",
    "style-src 'self' 'unsafe-inline' https://unpkg.com",
    "img-src 'self' data: https://tile.openstreetmap.org https://cdn.asunnot.oikotie.fi https://d3ls91xgksobn.cloudfront.net",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
])

DEFAULT_SETTINGS = {
    "sources": ["oikotie", "etuovi"],
    "max_listings": 300,
    "filters": {"house_types": [], "rooms": [], "plot_ownership": [], "municipalities": []},
    "destinations": [],
    "transit": {"router": "hsl", "arrive_by": "09:00", "day": "tuesday"},
    "map": {"green_factor": 0.5, "red_factor": 1.0, "default_max_minutes": 45, "fade_km": 3, "idw_power": 2},
}


def _profile_body(body: dict) -> tuple[str, dict, bool, str]:
    if not isinstance(body, dict):
        raise HTTPException(422, "Expected a JSON object")
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 100:
        raise HTTPException(422, "Name is required (max 100 characters)")
    refresh_at = str(body.get("refresh_at") or "06:00").strip()
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", refresh_at):
        raise HTTPException(422, "refresh_at must be HH:MM")
    try:
        settings = sanitize_settings(body.get("settings") or {})
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return name, settings, bool(body.get("refresh_daily")), refresh_at


def create_app(settings: ServerSettings, auth: Auth, start_jobs: bool = True) -> FastAPI:
    store = Store(settings.data_dir / "househunt.sqlite")
    jobs = JobManager(store, settings)
    geocoder = Geocoder(Cache(settings.cache_path))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_jobs:
            jobs.start()
        yield
        jobs.stop()

    app = FastAPI(title="househunt", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.jobs = store, jobs

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path not in OPEN_API_PATHS and not auth.valid(request.cookies.get(COOKIE)):
            response = JSONResponse({"detail": "Not signed in"}, status_code=401)
        else:
            response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # Session

    @app.get("/api/session")
    def session(request: Request):
        return {"auth_required": auth.enabled, "authenticated": auth.valid(request.cookies.get(COOKIE))}

    @app.post("/api/login")
    def login(request: Request, response: Response, body: dict = Body(...)):
        if not auth.enabled:
            return {"ok": True}
        if not auth.check_password(str(body.get("password") or "")):
            time.sleep(1)
            raise HTTPException(401, "Wrong password")
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response.set_cookie(COOKIE, auth.issue(), max_age=SESSION_SECONDS, httponly=True, samesite="lax", secure=secure)
        return {"ok": True}

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie(COOKIE)
        return {"ok": True}

    # Reference data

    @app.get("/api/meta")
    def meta():
        return {
            "house_types": list(HOUSE_TYPES),
            "plot_ownership": list(PLOT_OWNERSHIP),
            "municipalities": sorted(m.capitalize() for m in UUSIMAA_MUNICIPALITIES),
            "weekdays": list(WEEKDAYS),
            "routers": list(ROUTERS),
            "transit_available": bool(settings.digitransit_api_key),
            "defaults": DEFAULT_SETTINGS,
        }

    @app.get("/api/geocode")
    def geocode(q: str = ""):
        q = q.strip()
        if len(q) < 3 or len(q) > 200:
            raise HTTPException(422, "Search text must be 3–200 characters")
        try:
            return geocoder.search(q)
        except Exception as e:
            log.warning("Geocoding %r failed: %s", q, e)
            raise HTTPException(502, "Address search is unavailable right now") from e

    # Profiles

    @app.get("/api/profiles")
    def list_profiles():
        return store.list_profiles()

    @app.post("/api/profiles", status_code=201)
    def create_profile(body: dict = Body(...)):
        return store.create_profile(*_profile_body(body))

    def _get_profile(profile_id: int) -> dict:
        p = store.get_profile(profile_id)
        if not p:
            raise HTTPException(404, "No such search")
        return p

    @app.get("/api/profiles/{profile_id}")
    def get_profile(profile_id: int):
        p = _get_profile(profile_id)
        p["last_run"] = store.last_run(profile_id)
        return p

    @app.put("/api/profiles/{profile_id}")
    def update_profile(profile_id: int, body: dict = Body(...)):
        _get_profile(profile_id)
        return store.update_profile(profile_id, *_profile_body(body))

    @app.delete("/api/profiles/{profile_id}", status_code=204)
    def delete_profile(profile_id: int):
        active = store.active_run(profile_id)
        if active:
            jobs.cancel(active["id"])
        if not store.delete_profile(profile_id):
            raise HTTPException(404, "No such search")
        return Response(status_code=204)

    # Runs

    def _with_live(run: dict) -> dict:
        live = jobs.live_progress(run["id"])
        if live:
            run["progress"] = live
        return run

    @app.post("/api/profiles/{profile_id}/runs", status_code=202)
    def start_run(profile_id: int):
        _get_profile(profile_id)
        try:
            return jobs.enqueue(profile_id, "manual")
        except AlreadyRunning as e:
            return JSONResponse(_with_live(e.run), status_code=409)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: int):
        run = store.get_run(run_id)
        if not run:
            raise HTTPException(404, "No such run")
        return _with_live(run)

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: int):
        if not store.get_run(run_id):
            raise HTTPException(404, "No such run")
        return {"cancelled": jobs.cancel(run_id)}

    @app.get("/api/profiles/{profile_id}/results")
    def results(profile_id: int):
        _get_profile(profile_id)
        latest = store.latest_result(profile_id)
        if not latest:
            raise HTTPException(404, "No results yet")
        run, payload = latest
        return {"run": run, "payload": payload}

    @app.get("/api/profiles/{profile_id}/results.csv")
    def results_csv(profile_id: int):
        p = _get_profile(profile_id)
        latest = store.latest_result(profile_id)
        if not latest:
            raise HTTPException(404, "No results yet")
        filename = re.sub(r"[^A-Za-z0-9_-]+", "_", p["name"]).strip("_") or "listings"
        return PlainTextResponse(
            csv_text(latest[1]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
        )

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app


def _password() -> str | None:
    if os.environ.get("HOUSEHUNT_PASSWORD"):
        return os.environ["HOUSEHUNT_PASSWORD"]
    path = os.environ.get("HOUSEHUNT_PASSWORD_FILE")
    if path:
        return Path(path).read_text(encoding="utf-8").strip() or None
    return None


def main(args) -> int:
    import uvicorn

    data_dir = Path(args.data_dir)
    settings = ServerSettings(
        data_dir=data_dir,
        digitransit_api_key=os.environ.get("DIGITRANSIT_API_KEY"),
        digitransit_rps=float(os.environ.get("HOUSEHUNT_DIGITRANSIT_RPS", "2")),
    )

    if args.import_config:
        raw = yaml.safe_load(Path(args.import_config).read_text(encoding="utf-8")) or {}
        store = Store(data_dir / "househunt.sqlite")
        p = store.create_profile(Path(args.import_config).stem, sanitize_settings(raw), False, "06:00")
        print(f"Created search {p['name']!r} (id {p['id']})")
        return 0

    password = _password()
    if args.no_auth:
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            print("--no-auth is only allowed with --host 127.0.0.1", file=sys.stderr)
            return 2
        password = None
    elif not password:
        print("Set HOUSEHUNT_PASSWORD (or HOUSEHUNT_PASSWORD_FILE), or use --no-auth on 127.0.0.1", file=sys.stderr)
        return 2
    if not settings.digitransit_api_key:
        log.warning("DIGITRANSIT_API_KEY is not set; runs will compute car times only")

    app = create_app(settings, Auth(password, load_secret(data_dir)))
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("HOUSEHUNT_TRUSTED_PROXIES", "127.0.0.1"),
        log_level="info",
    )
    return 0
