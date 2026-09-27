import datetime as dt

import pytest
from fastapi.testclient import TestClient

from househunt.models import Listing
from househunt.pipeline import Result
from househunt.web import app as web_app
from househunt.web import jobs as web_jobs
from househunt.web import store as web_store
from househunt.web.jobs import HELSINKI, ServerSettings

SETTINGS = {
    "filters": {"house_types": ["omakotitalo"], "rooms": [4, 5]},
    "destinations": [{"name": "Work", "lat": 60.1756, "lon": 24.8295, "modes": ["car"], "max_minutes": {"car": 30}}],
    "transit": {"api_key": "leaked", "arrive_by": "09:00"},
    "cache_path": "/etc/passwd",
}


def listing(id_, price, lat=60.2, lon=24.8):
    return Listing("oikotie", id_, f"https://asunnot.oikotie.fi/{id_}", f"Tie {id_}", "Espoo", lat, lon, price, 120, 4,
                   "omakotitalo", 1990)


class FakePipeline:
    def __init__(self):
        self.listings = [listing("1", 400000), listing("2", 500000)]
        self.calls = 0

    def __call__(self, cfg, progress, **kwargs):
        self.calls += 1
        self.last_cfg = cfg
        for d in cfg.destinations:
            d.in_uusimaa = True
        progress.start("car", 1)
        progress.advance()
        return [Result(l, {"Work": {"car": 20.0, "transit": None}}, score=20.0) for l in self.listings]


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = FakePipeline()
    monkeypatch.setattr(web_jobs, "run_pipeline", fake)
    settings = ServerSettings(data_dir=tmp_path, digitransit_api_key=None)
    app = web_app.create_app(settings, "s3cret", b"k" * 32, start_jobs=False)
    client = TestClient(app, base_url="https://testserver")
    return client, app, fake


def login(client):
    assert client.post("/api/login", json={"password": "s3cret"}).status_code == 200


def create(client, **extra):
    body = {"name": "Family", "settings": SETTINGS, **extra}
    r = client.post("/api/profiles", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def run_now(client, app, profile_id):
    r = client.post(f"/api/profiles/{profile_id}/runs")
    assert r.status_code == 202, r.text
    app.state.jobs._execute(app.state.store.get_run(r.json()["id"]))
    return client.get(f"/api/runs/{r.json()['id']}").json()


def test_api_requires_login(env):
    client, _, _ = env
    assert client.get("/api/profiles").status_code == 401
    assert client.get("/api/session").json() == {"auth_required": True, "authenticated": False, "user": None}
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    login(client)
    assert client.get("/api/profiles").status_code == 200
    assert client.get("/api/session").json()["authenticated"] is True


def test_session_cookie_is_httponly_and_secure_over_https(env):
    client, _, _ = env
    r = client.post("/api/login", json={"password": "s3cret"})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie


def test_forged_cookie_rejected(env):
    client, _, _ = env
    client.cookies.set("househunt_session", "9999999999.deadbeef")
    assert client.get("/api/profiles").status_code == 401


def test_profile_validation_and_server_keys_stripped(env):
    client, _, _ = env
    login(client)
    r = client.post("/api/profiles", json={"name": "x", "settings": {"destinations": []}})
    assert r.status_code == 422 and "destination" in r.json()["detail"]
    r = client.post("/api/profiles", json={"name": "", "settings": SETTINGS})
    assert r.status_code == 422
    p = create(client)
    assert "cache_path" not in p["settings"]
    assert "api_key" not in p["settings"]["transit"]


def test_run_lifecycle_results_and_csv(env):
    client, app, fake = env
    login(client)
    p = create(client)
    run = run_now(client, app, p["id"])
    assert run["status"] == "done", run
    assert run["summary"] == "2 listings within limits, 0 over"
    res = client.get(f"/api/profiles/{p['id']}/results").json()
    assert len(res["payload"]["rows"]) == 2
    assert all(r["badges"] == [] for r in res["payload"]["rows"])
    csv = client.get(f"/api/profiles/{p['id']}/results.csv")
    assert csv.status_code == 200 and csv.text.startswith("score,")
    assert "attachment" in csv.headers["content-disposition"]


def test_second_run_flags_new_and_price_drop(env):
    client, app, fake = env
    login(client)
    p = create(client)
    run_now(client, app, p["id"])
    fake.listings = [listing("1", 380000), listing("2", 500000), listing("3", 450000)]
    run_now(client, app, p["id"])
    rows = {r["key"]: r for r in client.get(f"/api/profiles/{p['id']}/results").json()["payload"]["rows"]}
    assert [b["kind"] for b in rows["oikotie:1"]["badges"]] == ["price_down"]
    assert rows["oikotie:1"]["badges"][0]["label"] == "price −20 000 €"
    assert rows["oikotie:2"]["badges"] == []
    assert [b["kind"] for b in rows["oikotie:3"]["badges"]] == ["new"]


def test_one_active_run_per_profile(env):
    client, _, _ = env
    login(client)
    p = create(client)
    first = client.post(f"/api/profiles/{p['id']}/runs")
    second = client.post(f"/api/profiles/{p['id']}/runs")
    assert first.status_code == 202 and second.status_code == 409
    assert second.json()["id"] == first.json()["id"]
    assert client.post(f"/api/runs/{first.json()['id']}/cancel").json() == {"cancelled": True}
    assert client.get(f"/api/runs/{first.json()['id']}").json()["status"] == "cancelled"


def test_failed_run_reports_error(env, monkeypatch):
    client, app, _ = env
    login(client)
    p = create(client)

    def boom(cfg, progress, **kwargs):
        raise RuntimeError("Oikotie changed its API")

    monkeypatch.setattr(web_jobs, "run_pipeline", boom)
    run = run_now(client, app, p["id"])
    assert run["status"] == "failed" and "Oikotie changed" in run["error"]


def test_scheduler_runs_once_per_day_after_refresh_time(env, monkeypatch):
    client, app, _ = env
    login(client)
    p = create(client, refresh_daily=True, refresh_at="06:00")
    jobs = app.state.jobs
    early = dt.datetime(2026, 9, 28, 5, 59, tzinfo=HELSINKI)
    later = dt.datetime(2026, 9, 28, 6, 1, tzinfo=HELSINKI)
    monkeypatch.setattr(web_store, "now_iso", lambda: later.isoformat())
    assert jobs.schedule_due(early) == []
    started = jobs.schedule_due(later)
    assert len(started) == 1
    jobs.cancel(started[0])
    assert jobs.schedule_due(later) == []


def test_static_app_and_security_headers(env):
    client, _, _ = env
    r = client.get("/")
    assert r.status_code == 200 and "househunt" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_geocode_validates_query(env, monkeypatch):
    client, _, _ = env
    login(client)
    assert client.get("/api/geocode?q=ab").status_code == 422


def test_settings_shaped_like_the_web_form_are_accepted(env):
    client, _, _ = env
    login(client)
    form = {
        "name": "From the form",
        "refresh_daily": True,
        "refresh_at": "07:30",
        "settings": {
            "sources": ["oikotie"],
            "max_listings": 200,
            "filters": {
                "house_types": ["paritalo"], "rooms": [3, 5], "plot_ownership": ["own"],
                "price_min": None, "price_max": 450000, "size_min": 80, "size_max": None,
                "municipalities": ["Espoo", "Järvenpää"],
            },
            "destinations": [
                {"name": "Work", "modes": ["transit", "car"], "weight": 2, "address": "Keilaniemi, Espoo",
                 "lat": 60.1756, "lon": 24.8295, "max_minutes": {"transit": 40}},
                {"name": "Gym", "modes": [], "weight": 1, "address": "Tapiola, Espoo"},
            ],
            "transit": {"router": "hsl", "day": "wednesday", "depart_at": "07:45"},
            "map": {"green_factor": 0.5, "red_factor": 1, "default_max_minutes": 45, "fade_km": 3, "idw_power": 2},
        },
    }
    r = client.post("/api/profiles", json=form)
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["refresh_daily"] is True and p["refresh_at"] == "07:30"
    r = client.put(f"/api/profiles/{p['id']}", json={**form, "name": "Renamed"})
    assert r.status_code == 200 and r.json()["name"] == "Renamed"
    bad = {**form, "settings": {**form["settings"], "transit": {"day": "funday"}}}
    assert client.put(f"/api/profiles/{p['id']}", json=bad).status_code == 422


def test_malformed_cookies_are_rejected_not_500(env):
    client, _, _ = env
    for token in ("9999999999.\u00e9", "\u00b2\u00b2.abc", "nodot", "123."):
        raw = f"househunt_session={token}".encode("utf-8")
        assert client.get("/api/profiles", headers=[(b"cookie", raw)]).status_code == 401


def test_changing_admin_password_ends_admin_sessions(tmp_path):
    settings = ServerSettings(data_dir=tmp_path, digitransit_api_key=None)
    old = TestClient(web_app.create_app(settings, "one", b"k" * 32, start_jobs=False), base_url="https://testserver")
    assert old.post("/api/login", json={"password": "one"}).status_code == 200
    cookie = old.cookies.get("househunt_session")
    new = TestClient(web_app.create_app(settings, "two", b"k" * 32, start_jobs=False), base_url="https://testserver")
    new.cookies.set("househunt_session", cookie)
    assert new.get("/api/profiles").status_code == 401
    assert new.post("/api/login", json={"password": "one"}).status_code == 401
    assert new.post("/api/login", json={"password": "two"}).status_code == 200


def test_login_locks_after_repeated_failures(env, monkeypatch):
    client, _, _ = env

    async def no_sleep(_):
        return None

    monkeypatch.setattr(web_app.asyncio, "sleep", no_sleep)
    for _ in range(5):
        assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"password": "s3cret"}).status_code == 429


def test_cross_site_posts_refused(env):
    client, _, _ = env
    login(client)
    p = create(client)
    r = client.post(f"/api/profiles/{p['id']}/runs", headers={"Sec-Fetch-Site": "same-site"})
    assert r.status_code == 403
    r = client.post(f"/api/profiles/{p['id']}/runs", headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 202


def test_duplicate_listing_without_price_does_not_fail_run(env):
    client, app, fake = env
    login(client)
    p = create(client)
    fake.listings = [listing("9", None), listing("9", None)]
    assert run_now(client, app, p["id"])["status"] == "done"


def test_cancel_just_before_start_is_honoured(env):
    client, app, fake = env
    login(client)
    p = create(client)
    run = client.post(f"/api/profiles/{p['id']}/runs").json()
    queued = app.state.store.get_run(run["id"])
    assert client.post(f"/api/runs/{run['id']}/cancel").json() == {"cancelled": True}
    app.state.jobs._execute(queued)
    assert client.get(f"/api/runs/{run['id']}").json()["status"] == "cancelled"
    assert fake.calls == 0


def test_nan_settings_rejected(env):
    client, _, _ = env
    login(client)
    body = {"name": "x", "settings": {**SETTINGS, "map": {"fade_km": float("nan")}}}
    r = client.post("/api/profiles", content=__import__("json").dumps(body), headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    bad = {**SETTINGS, "destinations": [{**SETTINGS["destinations"][0], "weight": -1}]}
    assert client.post("/api/profiles", json={"name": "x", "settings": bad}).status_code == 422


def test_csv_neutralises_formulas():
    from househunt.report import csv_text
    payload = {"rows": [{"address": "=HYPERLINK(\"x\")", "price_eur": -5, "times": {}, "badges": []}]}
    text = csv_text(payload)
    assert "'=HYPERLINK" in text and ",-5" in text


def test_shutdown_marks_run_interrupted_and_scheduler_retries_once(env, monkeypatch):
    client, app, _ = env
    login(client)
    p = create(client, refresh_daily=True, refresh_at="06:00")
    jobs, store = app.state.jobs, app.state.store
    later = dt.datetime(2026, 9, 28, 6, 1, tzinfo=HELSINKI)
    monkeypatch.setattr(web_store, "now_iso", lambda: later.isoformat())

    def stopped(cfg, progress, **kwargs):
        jobs.stop()
        progress.check()

    monkeypatch.setattr(web_jobs, "run_pipeline", stopped)
    for expected_retry in (True, True, False):
        jobs._stop.clear()
        started = jobs.schedule_due(later)
        assert bool(started) is expected_retry
        if started:
            jobs._execute(store.get_run(started[0]))
            assert store.get_run(started[0])["status"] == "interrupted"
    jobs._stop.clear()
    assert jobs.schedule_due(later) == []
