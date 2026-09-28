import asyncio
import datetime as dt
import logging
import os
import re
import sqlite3
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from ..cache import Cache
from ..config import ROUTERS, WEEKDAYS, config_from_dict
from ..geocode import Geocoder
from ..models import HOUSE_TYPES, PLOT_OWNERSHIP, UUSIMAA_MUNICIPALITIES
from ..report import csv_text, reevaluate, summary
from .auth import (COOKIE, INVITE_DAYS, SESSION_DAYS, SESSION_SECONDS, Hasher, LoginThrottle, expiry,
                   generate_password, load_secret, new_token)
from .jobs import AlreadyRunning, JobManager, QuotaExceeded, ServerSettings, local_midnight
from .store import Store, sanitize_settings

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
OPEN_API_PATHS = {"/api/login", "/api/logout", "/api/session"}
OPEN_API_PREFIXES = ("/api/invites/",)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SESSION_TOUCH_SECONDS = 300
MAX_SEARCHES = 20
MAX_REFRESHING_SEARCHES = 3

CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://unpkg.com/leaflet@1.9.4/dist/",
    "style-src 'self' 'unsafe-inline' https://unpkg.com/leaflet@1.9.4/dist/",
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
    "map": {"green_factor": 0.5, "red_factor": 1.0, "default_max_minutes": 60, "fade_km": 3, "idw_power": 2},
    "car": {"rush_hour_factor": 1.2},
    "nearby_services": True,
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


def _label(value) -> str:
    label = str(value or "").strip()
    if not label or len(label) > 60:
        raise HTTPException(422, "Label is required (max 60 characters)")
    return label


def _optional_int(value, name: str, minimum: int, maximum: int) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise HTTPException(422, f"{name} must be a whole number from {minimum} to {maximum}, or empty for the default")
    return value


def _is_secure(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def create_app(settings: ServerSettings, password: str | None, secret: bytes, start_jobs: bool = True,
               secret_is_new: bool = False) -> FastAPI:
    store = Store(settings.data_dir / "househunt.sqlite")
    if secret_is_new and any(u["role"] != "admin" for u in store.list_users()):
        raise RuntimeError(
            "session.key was missing or damaged, but user accounts exist. Their passwords are keyed with it, so "
            "restore session.key from backup, or delete the users and invite them again."
        )
    jobs = JobManager(store, settings)
    geocoder = Geocoder(Cache(settings.cache_path))
    hasher = Hasher(secret)
    throttle = LoginThrottle()
    auth_enabled = password is not None
    if auth_enabled:
        admin = store.ensure_admin(hasher.password(password))
    else:
        admin = store.get_user(store.admin_id()) if store.admin_id() else store.ensure_admin(hasher.password(generate_password()))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.prune_sessions()
        if start_jobs:
            jobs.start()
        yield
        jobs.stop()

    app = FastAPI(title="househunt", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.jobs, app.state.hasher = store, jobs, hasher

    def resolve_user(request: Request) -> dict | None:
        if not auth_enabled:
            return store.get_user(admin["id"])
        token = request.cookies.get(COOKIE)
        if not token:
            return None
        token_hash = hasher.token(token)
        user = store.session_user(token_hash)
        if user:
            seen = dt.datetime.fromisoformat(user.pop("session_seen"))
            if (dt.datetime.now(dt.timezone.utc) - seen).total_seconds() > SESSION_TOUCH_SECONDS:
                store.touch_session(token_hash)
                store.touch_user(user["id"])
        return user

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        is_api = path.startswith("/api/")
        # SameSite=Lax still lets sibling subdomains behind the same proxy POST with the cookie.
        cross_site = request.headers.get("sec-fetch-site") not in (None, "same-origin", "none")
        request.state.user = await run_in_threadpool(resolve_user, request) if is_api else None
        open_path = path in OPEN_API_PATHS or path.startswith(OPEN_API_PREFIXES)
        if is_api and request.method not in SAFE_METHODS and cross_site:
            response = JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
        elif is_api and not open_path and request.state.user is None:
            response = JSONResponse({"detail": "Not signed in"}, status_code=401)
        else:
            response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if is_api:
            response.headers["Cache-Control"] = "no-store"
        return response

    def me(request: Request) -> dict:
        return request.state.user

    def require_admin(request: Request) -> dict:
        user = request.state.user
        if not user or user["role"] != "admin":
            raise HTTPException(403, "Admins only")
        return user

    def start_session(request: Request, response: Response, user: dict) -> None:
        token = new_token()
        store.create_session(hasher.token(token), user["id"], expiry(SESSION_DAYS))
        store.touch_user(user["id"])
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="lax",
                            secure=_is_secure(request))

    def public_user(user: dict) -> dict:
        limit = jobs.daily_limit(user)
        return {
            "label": user["label"],
            "role": user["role"],
            "daily_run_limit": limit,
            "runs_today": store.manual_runs_since(user["id"], local_midnight().isoformat()),
            "max_listings": jobs.listing_cap(user),
        }

    @app.get("/api/session")
    def session(request: Request):
        user = request.state.user
        return {
            "auth_required": auth_enabled,
            "authenticated": user is not None,
            "user": public_user(user) if user else None,
        }

    @app.post("/api/login")
    async def login(request: Request, response: Response, body: dict = Body(...)):
        if not auth_enabled:
            return {"ok": True}
        if throttle.locked():
            raise HTTPException(429, "Too many failed attempts, try again in a minute")
        user = await run_in_threadpool(store.user_by_secret, hasher.password(str(body.get("password") or "")))
        if not user:
            throttle.failed()
            await asyncio.sleep(1)
            raise HTTPException(401, "Wrong password")
        await run_in_threadpool(start_session, request, response, user)
        return {"ok": True}

    @app.post("/api/logout")
    def logout(request: Request, response: Response):
        token = request.cookies.get(COOKIE)
        if token:
            store.delete_session(hasher.token(token))
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.post("/api/logout-everywhere")
    def logout_everywhere(response: Response, user: dict = Depends(me)):
        store.delete_sessions(user["id"])
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/api/invites/{token}")
    def check_invite(token: str):
        invite = store.open_invite(hasher.token(token))
        if not invite:
            raise HTTPException(410, "This invite link has been used or has expired")
        return {"label": invite["label"], "expires_at": invite["expires_at"]}

    @app.post("/api/invites/{token}/redeem")
    def redeem_invite(token: str, request: Request, response: Response):
        for _ in range(3):
            password = generate_password()
            try:
                user = store.redeem_invite(hasher.token(token), hasher.password(password), None, None)
            except sqlite3.IntegrityError:
                continue
            break
        else:
            raise HTTPException(500, "Could not create the account, try again")
        if not user:
            raise HTTPException(410, "This invite link has been used or has expired")
        start_session(request, response, user)
        return {"password": password, "user": public_user(user)}

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

    @app.get("/api/profiles")
    def list_profiles(user: dict = Depends(me)):
        return store.list_profiles(user["id"])

    def check_search_limits(user: dict, refresh_daily: bool, profile_id: int | None = None) -> None:
        if user["role"] == "admin":
            return
        if profile_id is None and store.count_profiles(user["id"]) >= MAX_SEARCHES:
            raise HTTPException(429, f"At most {MAX_SEARCHES} searches per account")
        if refresh_daily:
            current = store.get_profile(profile_id, user["id"]) if profile_id else None
            already = bool(current and current["refresh_daily"])
            if not already and store.count_profiles(user["id"], refresh_only=True) >= MAX_REFRESHING_SEARCHES:
                raise HTTPException(429, f"At most {MAX_REFRESHING_SEARCHES} searches can refresh daily")

    @app.post("/api/profiles", status_code=201)
    def create_profile(body: dict = Body(...), user: dict = Depends(me)):
        fields = _profile_body(body)
        check_search_limits(user, fields[2])
        return store.create_profile(user["id"], *fields)

    def own_profile(profile_id: int, user: dict) -> dict:
        p = store.get_profile(profile_id, user["id"])
        if not p:
            raise HTTPException(404, "No such search")
        return p

    @app.get("/api/profiles/{profile_id}")
    def get_profile(profile_id: int, user: dict = Depends(me)):
        p = own_profile(profile_id, user)
        p["last_run"] = store.last_run(profile_id)
        return p

    @app.put("/api/profiles/{profile_id}")
    def update_profile(profile_id: int, body: dict = Body(...), user: dict = Depends(me)):
        own_profile(profile_id, user)
        fields = _profile_body(body)
        check_search_limits(user, fields[2], profile_id)
        return store.update_profile(profile_id, user["id"], *fields)

    @app.delete("/api/profiles/{profile_id}", status_code=204)
    def delete_profile(profile_id: int, user: dict = Depends(me)):
        own_profile(profile_id, user)
        active = store.active_run(profile_id)
        if active:
            jobs.cancel(active["id"])
        store.delete_profile(profile_id, user["id"])
        return Response(status_code=204)

    def with_live(run: dict) -> dict:
        live = jobs.live_progress(run["id"])
        if live:
            run["progress"] = live
        return run

    @app.post("/api/profiles/{profile_id}/runs", status_code=202)
    def start_run(profile_id: int, user: dict = Depends(me)):
        own_profile(profile_id, user)
        try:
            return jobs.enqueue(profile_id, "manual", user)
        except AlreadyRunning as e:
            if e.run["profile_id"] == profile_id:
                return JSONResponse(with_live(e.run), status_code=409)
            raise HTTPException(409, str(e)) from e
        except QuotaExceeded as e:
            raise HTTPException(429, str(e)) from e

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: int, user: dict = Depends(me)):
        run = store.get_run(run_id, user["id"])
        if not run:
            raise HTTPException(404, "No such run")
        return with_live(run)

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: int, user: dict = Depends(me)):
        if not store.get_run(run_id, user["id"]):
            raise HTTPException(404, "No such run")
        return {"cancelled": jobs.cancel(run_id)}

    def current_result(profile: dict) -> tuple[dict, dict]:
        latest = store.latest_result(profile["id"])
        if not latest:
            raise HTTPException(404, "No results yet")
        run, payload = latest
        payload = reevaluate(payload, config_from_dict(profile["settings"], env_api_key=False))
        run["summary"] = summary(payload)
        return run, payload

    @app.get("/api/profiles/{profile_id}/results")
    def results(profile_id: int, user: dict = Depends(me)):
        run, payload = current_result(own_profile(profile_id, user))
        return {"run": run, "payload": payload}

    @app.get("/api/profiles/{profile_id}/results.csv")
    def results_csv(profile_id: int, user: dict = Depends(me)):
        p = own_profile(profile_id, user)
        _, payload = current_result(p)
        filename = re.sub(r"[^A-Za-z0-9_-]+", "_", p["name"]).strip("_") or "listings"
        return PlainTextResponse(
            csv_text(payload),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
        )

    @app.get("/api/admin/users")
    def admin_users(_: dict = Depends(require_admin)):
        users = store.list_users()
        for u in users:
            u["effective_daily_run_limit"] = jobs.daily_limit(u)
            u["effective_max_listings"] = jobs.listing_cap(u)
        return {"users": users, "defaults": {"daily_run_limit": settings.default_daily_runs,
                                             "max_listings": settings.default_max_listings}}

    def managed_user(user_id: int) -> dict:
        target = store.get_user(user_id)
        if not target:
            raise HTTPException(404, "No such user")
        return target

    @app.patch("/api/admin/users/{user_id}")
    def admin_update_user(user_id: int, body: dict = Body(...), _: dict = Depends(require_admin)):
        target = managed_user(user_id)
        fields = {}
        if "label" in body:
            fields["label"] = _label(body["label"])
        if target["role"] != "admin":
            if "disabled" in body:
                if not isinstance(body["disabled"], bool):
                    raise HTTPException(422, "disabled must be true or false")
                fields["disabled"] = int(body["disabled"])
            if "daily_run_limit" in body:
                fields["daily_run_limit"] = _optional_int(body["daily_run_limit"], "Daily runs", 0, 100)
            if "max_listings" in body:
                fields["max_listings"] = _optional_int(body["max_listings"], "Max listings", 1, 5000)
        elif set(body) - {"label"}:
            raise HTTPException(422, "The admin account can only be renamed; its password comes from HOUSEHUNT_PASSWORD")
        if fields.get("disabled"):
            active = store.user_active_run(user_id)
            if active:
                jobs.cancel(active["id"])
        return store.update_user(user_id, **fields)

    @app.post("/api/admin/users/{user_id}/password")
    def admin_new_password(user_id: int, _: dict = Depends(require_admin)):
        target = managed_user(user_id)
        if target["role"] == "admin":
            raise HTTPException(422, "Change the admin password with HOUSEHUNT_PASSWORD")
        password = generate_password()
        store.update_user(user_id, secret_hash=hasher.password(password))
        return {"password": password}

    @app.delete("/api/admin/users/{user_id}", status_code=204)
    def admin_delete_user(user_id: int, _: dict = Depends(require_admin)):
        target = managed_user(user_id)
        if target["role"] == "admin":
            raise HTTPException(422, "The admin account can't be deleted")
        active = store.user_active_run(user_id)
        if active:
            jobs.cancel(active["id"])
        store.delete_user(user_id)
        return Response(status_code=204)

    @app.get("/api/admin/invites")
    def admin_invites(_: dict = Depends(require_admin)):
        return store.pending_invites()

    @app.post("/api/admin/invites", status_code=201)
    def admin_create_invite(body: dict = Body(...), _: dict = Depends(require_admin)):
        token = new_token()
        invite = store.create_invite(hasher.token(token), _label(body.get("label")), expiry(INVITE_DAYS))
        return {**invite, "path": f"/#/invite/{token}"}

    @app.delete("/api/admin/invites/{invite_id}", status_code=204)
    def admin_delete_invite(invite_id: int, _: dict = Depends(require_admin)):
        if not store.delete_invite(invite_id):
            raise HTTPException(404, "No such pending invite")
        return Response(status_code=204)

    @app.get("/api/admin/queue")
    def admin_queue(_: dict = Depends(require_admin)):
        return [
            {k: with_live(r)[k] for k in ("id", "status", "trigger", "created_at", "started_at", "progress", "user_label")}
            for r in store.queue()
        ]

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
        fetch_cache_hours=float(os.environ.get("HOUSEHUNT_FETCH_CACHE_HOURS", "6")),
        default_daily_runs=int(os.environ.get("HOUSEHUNT_DEFAULT_DAILY_RUNS", "5")),
        default_max_listings=int(os.environ.get("HOUSEHUNT_DEFAULT_MAX_LISTINGS", "300")),
    )

    if args.import_config:
        raw = yaml.safe_load(Path(args.import_config).read_text(encoding="utf-8")) or {}
        store = Store(data_dir / "househunt.sqlite")
        p = store.create_profile(store.admin_id(), Path(args.import_config).stem, sanitize_settings(raw), False, "06:00")
        print(f"Created search {p['name']!r} (id {p['id']}) for the admin account")
        return 0

    if not 0 < settings.fetch_cache_hours <= 24:
        print("HOUSEHUNT_FETCH_CACHE_HOURS must be between 0 and 24", file=sys.stderr)
        return 2
    key_path = data_dir / "session.key"
    secret_is_new = not (key_path.exists() and key_path.stat().st_size >= 32)

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

    try:
        app = create_app(settings, password, load_secret(data_dir), secret_is_new=secret_is_new)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 2
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("HOUSEHUNT_TRUSTED_PROXIES", "127.0.0.1"),
        log_level="info",
    )
    return 0
