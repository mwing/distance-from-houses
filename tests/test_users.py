import datetime as dt
import sqlite3

import pytest
from fastapi.testclient import TestClient

from househunt.cache import Cache
from househunt.fetchcache import FetchCache
from househunt.models import Filters
from househunt.web import app as web_app
from househunt.web import jobs as web_jobs
from househunt.web.jobs import HELSINKI, ServerSettings
from househunt.web.store import Store

from test_web import SETTINGS, FakePipeline, listing

ADMIN_PW = "admin-secret"


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = FakePipeline()
    monkeypatch.setattr(web_jobs, "run_pipeline", fake)
    settings = ServerSettings(data_dir=tmp_path, digitransit_api_key=None, default_daily_runs=2, default_max_listings=50)
    app = web_app.create_app(settings, ADMIN_PW, b"k" * 32, start_jobs=False)

    def client():
        return TestClient(app, base_url="https://testserver")

    admin = client()
    assert admin.post("/api/login", json={"password": ADMIN_PW}).status_code == 200
    return app, admin, client, fake


def invite_user(admin, client, label="Mum"):
    r = admin.post("/api/admin/invites", json={"label": label})
    assert r.status_code == 201, r.text
    token = r.json()["path"].rsplit("/", 1)[1]
    c = client()
    assert c.get(f"/api/invites/{token}").json()["label"] == label
    r = c.post(f"/api/invites/{token}/redeem")
    assert r.status_code == 200, r.text
    return c, r.json()["password"], token


def create(c, name="Search"):
    r = c.post("/api/profiles", json={"name": name, "settings": SETTINGS})
    assert r.status_code == 201, r.text
    return r.json()


def test_invite_creates_account_signed_in_and_is_single_use(env):
    app, admin, client, _ = env
    user, password, token = invite_user(admin, client)
    assert user.get("/api/session").json()["user"]["label"] == "Mum"
    assert user.get("/api/session").json()["user"]["role"] == "user"
    assert client().post(f"/api/invites/{token}/redeem").status_code == 410
    assert client().get(f"/api/invites/{token}").status_code == 410
    fresh = client()
    assert fresh.post("/api/login", json={"password": password}).status_code == 200
    assert fresh.get("/api/session").json()["user"]["label"] == "Mum"
    assert len(password.replace("-", "")) == 20


def test_expired_invite_rejected(env):
    app, admin, client, _ = env
    r = admin.post("/api/admin/invites", json={"label": "Late"})
    token = r.json()["path"].rsplit("/", 1)[1]
    app.state.store._exec("UPDATE invites SET expires_at = '2000-01-01T00:00:00+00:00'")
    assert client().post(f"/api/invites/{token}/redeem").status_code == 410


def test_searches_are_strictly_separated(env):
    app, admin, client, _ = env
    a, _, _ = invite_user(admin, client, "A")
    b, _, _ = invite_user(admin, client, "B")
    pa = create(a, "A's search")
    run = a.post(f"/api/profiles/{pa['id']}/runs").json()
    app.state.jobs._execute(app.state.store.get_run(run["id"]))

    assert [p["name"] for p in b.get("/api/profiles").json()] == []
    assert [p["name"] for p in admin.get("/api/profiles").json()] == []
    for method, path in [
        ("GET", f"/api/profiles/{pa['id']}"),
        ("PUT", f"/api/profiles/{pa['id']}"),
        ("DELETE", f"/api/profiles/{pa['id']}"),
        ("POST", f"/api/profiles/{pa['id']}/runs"),
        ("GET", f"/api/profiles/{pa['id']}/results"),
        ("GET", f"/api/profiles/{pa['id']}/results.csv"),
        ("GET", f"/api/runs/{run['id']}"),
        ("POST", f"/api/runs/{run['id']}/cancel"),
    ]:
        for other in (b, admin):
            kwargs = {"json": {"name": "x", "settings": SETTINGS}} if method == "PUT" else {}
            r = other.request(method, path, **kwargs)
            assert r.status_code == 404, (method, path, r.status_code)
    assert a.get(f"/api/profiles/{pa['id']}/results").status_code == 200


def test_admin_endpoints_need_admin(env):
    _, admin, client, _ = env
    user, _, _ = invite_user(admin, client)
    for path in ("/api/admin/users", "/api/admin/invites", "/api/admin/queue"):
        assert user.get(path).status_code == 403
        assert admin.get(path).status_code == 200
    assert user.post("/api/admin/invites", json={"label": "x"}).status_code == 403


def test_disable_and_new_password_end_sessions(env):
    app, admin, client, _ = env
    user, password, _ = invite_user(admin, client)
    uid = next(u["id"] for u in admin.get("/api/admin/users").json()["users"] if u["label"] == "Mum")

    new_pw = admin.post(f"/api/admin/users/{uid}/password").json()["password"]
    assert user.get("/api/profiles").status_code == 401
    assert client().post("/api/login", json={"password": password}).status_code == 401
    again = client()
    assert again.post("/api/login", json={"password": new_pw}).status_code == 200

    assert admin.patch(f"/api/admin/users/{uid}", json={"disabled": True}).status_code == 200
    assert again.get("/api/profiles").status_code == 401
    assert client().post("/api/login", json={"password": new_pw}).status_code == 401


def test_admin_account_is_protected(env):
    _, admin, _, _ = env
    admin_id = next(u["id"] for u in admin.get("/api/admin/users").json()["users"] if u["role"] == "admin")
    assert admin.patch(f"/api/admin/users/{admin_id}", json={"disabled": True}).status_code == 422
    assert admin.post(f"/api/admin/users/{admin_id}/password").status_code == 422
    assert admin.delete(f"/api/admin/users/{admin_id}").status_code == 422
    assert admin.patch(f"/api/admin/users/{admin_id}", json={"label": "Me"}).json()["label"] == "Me"


def test_logout_everywhere(env):
    _, admin, client, _ = env
    user, password, _ = invite_user(admin, client)
    second = client()
    second.post("/api/login", json={"password": password})
    assert user.post("/api/logout-everywhere").status_code == 200
    assert second.get("/api/profiles").status_code == 401


def test_quota_one_active_run_and_daily_limit(env):
    app, admin, client, fake = env
    user, _, _ = invite_user(admin, client)
    p1, p2 = create(user, "one"), create(user, "two")
    first = user.post(f"/api/profiles/{p1['id']}/runs")
    assert first.status_code == 202
    r = user.post(f"/api/profiles/{p2['id']}/runs")
    assert r.status_code == 409 and "Another of your searches" in r.json()["detail"]
    app.state.jobs._execute(app.state.store.get_run(first.json()["id"]))
    second = user.post(f"/api/profiles/{p2['id']}/runs")
    assert second.status_code == 202
    app.state.jobs._execute(app.state.store.get_run(second.json()["id"]))
    r = user.post(f"/api/profiles/{p1['id']}/runs")
    assert r.status_code == 429 and "Daily limit of 2" in r.json()["detail"]
    assert user.get("/api/session").json()["user"]["runs_today"] == 2


def test_listing_cap_applies_to_users_not_admin(env):
    app, admin, client, fake = env
    user, _, _ = invite_user(admin, client)
    p = create(user)
    run = user.post(f"/api/profiles/{p['id']}/runs").json()
    app.state.jobs._execute(app.state.store.get_run(run["id"]))
    assert fake.last_cfg.max_listings == 50
    pa = create(admin)
    run = admin.post(f"/api/profiles/{pa['id']}/runs").json()
    app.state.jobs._execute(app.state.store.get_run(run["id"]))
    assert fake.last_cfg.max_listings == 500


def test_scheduler_skips_disabled_users(env):
    app, admin, client, _ = env
    user, _, _ = invite_user(admin, client)
    r = user.post("/api/profiles", json={"name": "daily", "settings": SETTINGS, "refresh_daily": True, "refresh_at": "06:00"})
    uid = next(u["id"] for u in admin.get("/api/admin/users").json()["users"] if u["label"] == "Mum")
    admin.patch(f"/api/admin/users/{uid}", json={"disabled": True})
    later = dt.datetime(2026, 9, 28, 6, 1, tzinfo=HELSINKI)
    assert app.state.jobs.schedule_due(later) == []


def test_existing_profiles_move_to_admin(tmp_path):
    db = sqlite3.connect(tmp_path / "househunt.sqlite")
    db.execute("""CREATE TABLE profiles (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, settings TEXT NOT NULL,
                  refresh_daily INTEGER NOT NULL DEFAULT 0, refresh_at TEXT NOT NULL DEFAULT '06:00',
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    db.execute("INSERT INTO profiles (name, settings, created_at, updated_at) VALUES ('old', '{}', 'x', 'x')")
    db.commit()
    db.close()
    store = Store(tmp_path / "househunt.sqlite")
    admin = store.ensure_admin("hash")
    assert [p["name"] for p in store.list_profiles(admin["id"])] == ["old"]


def test_fetch_cache_reuses_within_ttl(tmp_path):
    fc = FetchCache(Cache(tmp_path / "c.sqlite"), ttl_hours=6)
    f = Filters(house_types=["rivitalo", "paritalo"], rooms=[4])
    fc.put("oikotie", f, 100, [listing("1", 1000)])
    same_filters_other_order = Filters(house_types=["paritalo", "rivitalo"], rooms=[4])
    hit = fc.get("oikotie", same_filters_other_order, 100)
    assert hit and hit[0].id == "1"
    assert fc.get("etuovi", f, 100) is None
    assert fc.get("oikotie", f, 200) is None
    assert fc.get("oikotie", Filters(house_types=["rivitalo"]), 100) is None
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=7)).isoformat()
    ns = fc._ns(dt.datetime.now(dt.timezone.utc).date())
    key = fc._key("oikotie", f, 100)
    fc.cache.set(ns, key, {**fc.cache.get(ns, key), "fetched_at": old})
    assert fc.get("oikotie", f, 100) is None


def test_deleting_a_search_does_not_reset_daily_runs(env):
    app, admin, client, _ = env
    user, _, _ = invite_user(admin, client)
    for _ in range(2):
        p = create(user)
        run = user.post(f"/api/profiles/{p['id']}/runs").json()
        app.state.jobs._execute(app.state.store.get_run(run["id"]))
        assert user.delete(f"/api/profiles/{p['id']}").status_code == 204
    p = create(user)
    assert user.post(f"/api/profiles/{p['id']}/runs").status_code == 429


def test_scheduled_runs_once_per_day_and_one_per_user(env):
    app, admin, client, _ = env
    user, _, _ = invite_user(admin, client)
    body = {"settings": SETTINGS, "refresh_daily": True, "refresh_at": "06:00"}
    p1 = user.post("/api/profiles", json={"name": "a", **body}).json()
    user.post("/api/profiles", json={"name": "b", **body})
    jobs, store = app.state.jobs, app.state.store
    t = dt.datetime(2026, 9, 28, 6, 1, tzinfo=HELSINKI)
    first = jobs.schedule_due(t)
    assert len(first) == 1
    jobs._execute(store.get_run(first[0]))
    second = jobs.schedule_due(t)
    assert len(second) == 1 and second != first
    jobs._execute(store.get_run(second[0]))
    user.put(f"/api/profiles/{p1['id']}", json={"name": "a", **body, "refresh_at": "07:00"})
    assert jobs.schedule_due(t.replace(hour=7, minute=5)) == []


def test_search_limits_for_users(env, monkeypatch):
    app, admin, client, _ = env
    user, _, _ = invite_user(admin, client)
    monkeypatch.setattr(web_app, "MAX_SEARCHES", 2)
    body = {"settings": SETTINGS, "refresh_daily": True}
    assert user.post("/api/profiles", json={"name": "1", **body}).status_code == 201
    assert user.post("/api/profiles", json={"name": "2", **body}).status_code == 201
    assert user.post("/api/profiles", json={"name": "3", "settings": SETTINGS}).status_code == 429
    too_many = {**SETTINGS, "destinations": [{**SETTINGS["destinations"][0], "name": f"d{i}"} for i in range(11)]}
    assert admin.post("/api/profiles", json={"name": "x", "settings": too_many}).status_code == 422


def test_admin_input_validation(env):
    _, admin, client, _ = env
    invite_user(admin, client)
    uid = next(u["id"] for u in admin.get("/api/admin/users").json()["users"] if u["label"] == "Mum")
    assert admin.patch(f"/api/admin/users/{uid}", json={"disabled": "false"}).status_code == 422
    assert admin.patch(f"/api/admin/users/{uid}", json={"max_listings": 0}).status_code == 422
    assert admin.patch(f"/api/admin/users/{uid}", json={"daily_run_limit": 0}).status_code == 200


def test_disabling_cancels_queued_run(env):
    app, admin, client, _ = env
    user, _, _ = invite_user(admin, client)
    p = create(user)
    run = user.post(f"/api/profiles/{p['id']}/runs").json()
    uid = next(u["id"] for u in admin.get("/api/admin/users").json()["users"] if u["label"] == "Mum")
    admin.patch(f"/api/admin/users/{uid}", json={"disabled": True})
    assert app.state.store.get_run(run["id"])["status"] == "cancelled"


def test_new_session_key_with_existing_users_refuses_to_start(tmp_path):
    settings = ServerSettings(data_dir=tmp_path, digitransit_api_key=None)
    app = web_app.create_app(settings, ADMIN_PW, b"k" * 32, start_jobs=False)
    app.state.store.create_user("Mum", "hash", None, None)
    with pytest.raises(RuntimeError, match="session.key"):
        web_app.create_app(settings, ADMIN_PW, b"j" * 32, start_jobs=False, secret_is_new=True)


def test_fetch_cache_rejects_long_ttl_and_old_format(tmp_path):
    with pytest.raises(ValueError):
        FetchCache(Cache(tmp_path / "c.sqlite"), ttl_hours=48)
    fc = FetchCache(Cache(tmp_path / "c.sqlite"), ttl_hours=6)
    f = Filters()
    fc.put("oikotie", f, 10, [listing("1", 1)])
    ns, key = fc._ns(dt.datetime.now(dt.timezone.utc).date()), fc._key("oikotie", f, 10)
    entry = fc.cache.get(ns, key)
    entry["listings"][0]["removed_field"] = 1
    fc.cache.set(ns, key, entry)
    assert fc.get("oikotie", f, 10) is None
