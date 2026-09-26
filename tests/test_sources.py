import datetime as dt
import json
from pathlib import Path

import pytest

from househunt.config import TransitSettings, _parse_filters
from househunt.models import Filters
from househunt.routing import target_datetime
from househunt.sources import dedupe, matches, parse_number
from househunt.sources import etuovi, oikotie

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_parse_number():
    assert parse_number("445 000 €") == 445000
    assert parse_number("139/162 m²") == 139
    assert parse_number("58,5 m²") == 58.5
    assert parse_number(None) is None


def test_oikotie_parse_card():
    listings = [oikotie.parse_card(c) for c in load("oikotie_search.json")["cards"]]
    first = listings[0]
    assert first.municipality == "Tuusula"
    assert first.price == 445000
    assert first.size == 139
    assert first.house_type == "omakotitalo"
    assert first.rooms == 4
    assert first.in_uusimaa
    assert {l.house_type for l in listings} <= {"omakotitalo", "kerrostalo"}


def test_etuovi_parse_announcement():
    listings = [etuovi.parse_announcement(a) for a in load("etuovi_listpage.json")["announcements"]]
    first = listings[0]
    assert first.municipality == "Tuusula"
    assert first.url == "https://www.etuovi.com/kohde/80531098"
    assert first.house_type == "omakotitalo"
    assert first.rooms == 4
    assert first.image.startswith("https://") and "{imageParameters}" not in first.image


def test_same_home_on_both_sites_is_merged():
    o = oikotie.parse_card(load("oikotie_search.json")["cards"][0])
    e = etuovi.parse_announcement(load("etuovi_listpage.json")["announcements"][0])
    merged = dedupe([o, e])
    assert len(merged) == 1
    assert merged[0].other_urls == [e.url]


def test_oikotie_params():
    f = Filters(house_types=["omakotitalo", "paritalo"], rooms=[3, 5], plot_ownership=["own"], price_max=500000)
    params = oikotie.build_params(f, offset=100)
    assert ("buildingType[]", "4") in params and ("buildingType[]", "64") in params
    assert ("roomCount[]", "3") in params and ("roomCount[]", "6") in params
    assert ("lotOwnershipType[]", "1") in params
    assert ("price[max]", "500000") in params
    assert ("offset", "100") in params


def test_etuovi_body():
    f = Filters(house_types=["erillistalo"], rooms=[5], plot_ownership=["rent"], size_min=80)
    body = etuovi.build_body(f, offset=0)
    assert body["residentialPropertyTypes"] == ["SEPARATE_HOUSE"]
    assert body["roomCounts"] == ["FIVE_ROOMS"]
    assert body["plotHoldingTypes"] == ["RENT"]
    assert body["sizeMin"] == 80


def test_matches_rooms_five_plus_and_municipality():
    l = oikotie.parse_card(load("oikotie_search.json")["cards"][2])
    assert l.rooms == 6
    assert matches(l, Filters(rooms=[5]))
    assert not matches(l, Filters(rooms=[4]))
    assert matches(l, Filters(municipalities=["espoo"]))
    assert not matches(l, Filters(municipalities=["vantaa"]))


def test_filters_reject_unknown_values():
    with pytest.raises(ValueError):
        _parse_filters({"house_types": ["mökki"]})
    with pytest.raises(ValueError):
        _parse_filters({"plot_ownership": ["lease"]})


def test_target_datetime_next_weekday():
    kind, when = target_datetime(TransitSettings(arrive_by="09:00", day="tuesday"), today=dt.date(2026, 9, 29))
    assert kind == "latestArrival"
    assert when == "2026-10-06T09:00:00+03:00"
    kind, when = target_datetime(TransitSettings(arrive_by=None, depart_at="7:30", day="monday"), today=dt.date(2026, 9, 26))
    assert kind == "earliestDeparture"
    assert when == "2026-09-28T07:30:00+03:00"


def test_all_sources_failing_raises(monkeypatch):
    from househunt import pipeline
    from househunt.config import config_from_dict

    def broken(*a, **k):
        raise RuntimeError("blocked")

    monkeypatch.setattr(pipeline, "FETCHERS", {"oikotie": broken, "etuovi": broken})
    cfg = config_from_dict({"destinations": [{"name": "W", "lat": 60.2, "lon": 24.9}]}, env_api_key=False)
    with pytest.raises(RuntimeError, match="Every listing source failed"):
        pipeline.fetch_listings(cfg)
