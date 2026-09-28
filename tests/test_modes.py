import pytest

from househunt import pipeline, services
from househunt.config import config_from_dict
from househunt.models import Filters, Listing
from househunt.sources import etuovi, matches, oikotie


def listing(id_, lat=60.2, lon=24.8, year=1990):
    return Listing("oikotie", id_, f"https://x/{id_}", f"Tie {id_}", "Espoo", lat, lon, 300000, 100, 4, "rivitalo", year)


def test_build_year_filters_reach_both_sites_and_client_check():
    f = Filters(build_year_min=1990, build_year_max=2010)
    params = oikotie.build_params(f, 0)
    assert ("constructionYear[min]", "1990") in params and ("constructionYear[max]", "2010") in params
    body = etuovi.build_body(f, 0)
    assert body["yearMin"] == 1990 and body["yearMax"] == 2010
    assert matches(listing("1", year=2000), f)
    assert not matches(listing("1", year=1985), f)
    assert not matches(listing("1", year=2015), f)
    assert matches(listing("1", year=None), f)


def test_config_validation_for_new_settings():
    base = {"destinations": [{"name": "W", "lat": 60.2, "lon": 24.9, "modes": ["bike", "walk"], "max_minutes": {"bike": 20}}]}
    cfg = config_from_dict(base, env_api_key=False)
    assert cfg.destinations[0].modes == ["bike", "walk"] and cfg.car.rush_hour_factor == 1.2 and cfg.nearby_services
    with pytest.raises(ValueError):
        config_from_dict({**base, "car": {"rush_hour_factor": 0.5}}, env_api_key=False)
    with pytest.raises(ValueError):
        config_from_dict({**base, "filters": {"build_year_min": 1500}}, env_api_key=False)
    with pytest.raises(ValueError):
        config_from_dict({"destinations": [{"name": "W", "lat": 60, "lon": 24, "modes": ["boat"]}]}, env_api_key=False)


class FakeTable:
    speeds = {"car": 20.0, "bike": 40.0, "walk": 120.0}

    def __init__(self, mode, cache):
        self.mode = mode

    def minutes(self, origins, dests, progress=None):
        return {(o, d): self.speeds[self.mode] for o in origins for d in dests}


class FakeTransit:
    queried = []

    def __init__(self, settings, cache):
        pass

    def minutes(self, pairs, progress=None):
        FakeTransit.queried.extend(pairs)
        return {p: 30.0 for p in pairs}


class FakeServices:
    def __init__(self, cache):
        pass

    def points(self):
        return {"daycare": [[60.201, 24.8, "Päiväkoti Esimerkki"]], "school": [], "grocery": [[60.3, 24.8, "Market"]]}


@pytest.fixture
def fake_run(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "fetch_listings", lambda cfg, progress, fc: [listing("1"), listing("2", lat=60.25)])
    monkeypatch.setattr(pipeline, "TableRouter", FakeTable)
    monkeypatch.setattr(pipeline, "TransitRouter", FakeTransit)
    monkeypatch.setattr(pipeline, "Services", FakeServices)
    monkeypatch.setattr(pipeline.Geocoder, "resolve", lambda self, d: (setattr(d, "in_uusimaa", True), d)[1])
    FakeTransit.queried = []

    def go(raw):
        cfg = config_from_dict({**raw, "cache_path": str(tmp_path / "c.sqlite")}, env_api_key=False)
        cfg.transit.api_key = "k"
        return pipeline.run(cfg)
    return go


def test_rush_hour_factor_bike_walk_and_services(fake_run):
    results = fake_run({
        "car": {"rush_hour_factor": 1.5},
        "destinations": [
            {"name": "Work", "lat": 60.17, "lon": 24.83, "modes": ["transit", "car"]},
            {"name": "School", "lat": 60.18, "lon": 24.80, "modes": ["bike", "walk"]},
        ],
    })
    t = results[0].times
    assert t["Work"]["car"] == 30.0 and t["Work"]["transit"] == 30.0
    assert t["School"]["bike"] == 40.0 and t["School"]["walk"] == 120.0 and "bike" not in t["Work"]
    near = {r.listing.id: r.nearby for r in results}
    assert near["1"]["daycare"]["name"] == "Päiväkoti Esimerkki" and near["1"]["daycare"]["m"] == 110
    assert near["1"]["school"] is None
    assert results[0].score == 30.0 + 40.0


def test_transit_shortcut_uses_free_flow_car_time(fake_run):
    results = fake_run({
        "car": {"rush_hour_factor": 1.5},
        "destinations": [{"name": "Work", "lat": 60.17, "lon": 24.83, "max_minutes": {"transit": 25}}],
    })
    assert len(FakeTransit.queried) == 2
    assert all(r.times["Work"]["car"] == 30.0 for r in results)


def test_services_disabled(fake_run):
    results = fake_run({"nearby_services": False, "destinations": [{"name": "W", "lat": 60.17, "lon": 24.83}]})
    assert all(r.nearby == {} for r in results)


def test_overpass_parse_and_nearest():
    elements = [
        {"type": "node", "lat": 60.2, "lon": 24.9, "tags": {"amenity": "kindergarten", "name": "A"}},
        {"type": "way", "center": {"lat": 60.21, "lon": 24.9}, "tags": {"amenity": "school", "name": "B"}},
        {"type": "node", "lat": 60.3, "lon": 24.9, "tags": {"shop": "supermarket"}},
        {"type": "node", "lat": 60.3, "lon": 24.9, "tags": {"amenity": "fuel"}},
        {"type": "relation", "tags": {"amenity": "school"}},
    ]
    points = services._parse(elements)
    assert [p[2] for p in points["daycare"]] == ["A"]
    assert [p[2] for p in points["school"]] == ["B"]
    assert len(points["grocery"]) == 1
    assert services.nearest(60.2, 24.9, points["school"])["m"] == 1110
    assert services.nearest(60.2, 24.9, []) is None
