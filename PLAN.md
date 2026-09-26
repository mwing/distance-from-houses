# househunt — plan

Find homes for sale in Uusimaa from Etuovi + Oikotie and rank them by travel
time (public transport + car) to chosen destinations.

Tick a box when the task is done and verified. Next session: start at the first
unchecked box.

## Data sources (verified 2026-09-26)

- **Oikotie** `GET https://asunnot.oikotie.fi/api/search`
  - Needs headers `OTA-token`, `OTA-loaded`, `OTA-cuid`, scraped from
    `<meta name="api-token|loaded|cuid">` on any asunnot.oikotie.fi page. Refresh per run.
  - `cardType=100` sale (`101` rent), `limit` ≤ 100, `offset`,
    `locations=[[2,7,"Uusimaa"]]`, `price[min|max]`, `size[min|max]`,
    `roomCount[]` (exact count 1–7; 5+ = 5,6,7), `buildingType[]`, `lotOwnershipType[]`.
  - buildingType: kerrostalo 1, rivitalo 2, omakotitalo 4, erillistalo 32,
    paritalo 64, luhtitalo 256, puutalo-osake 512. Card `cardSubType` holds the same code.
  - lotOwnershipType: own 1, rent 2.
  - Cards include lat/lon, price string, size, rooms, buildYear.
- **Etuovi** `POST https://www.etuovi.com/api/v2/announcements/search/listpage` (and `/count`), no auth.
  - Sale only (rentals are on Vuokraovi).
  - `locationSearchCriteria.classifiedLocationTerms=[{"type":"REGION","code":"FI_UUSIMAA"}]`,
    `priceMin/Max`, `sizeMin/Max`, `residentialPropertyTypes`, `roomCounts`, `plotHoldingTypes`,
    `pagination{firstResult,maxResults≤100,sortingOrder}`.
  - residentialPropertyTypes: APARTMENT_HOUSE, ROW_HOUSE, DETACHED_HOUSE (omakotitalo),
    SEPARATE_HOUSE (erillistalo), SEMI_DETACHED_HOUSE, BALCONY_ACCESS_BLOCK (luhtitalo),
    WOODEN_HOUSE_APARTMENT.
  - Paging uses `pagination.page` (1-based); `firstResult` alone is ignored.
  - roomCounts: ONE_ROOM, TWO_ROOMS … FIVE_ROOMS (5+). plotHoldingTypes: OWN, RENT.
  - Listing URL: `https://www.etuovi.com/kohde/{friendlyId}`.
- **Transit**: Digitransit Routing v2 GraphQL `planConnection`,
  `https://api.digitransit.fi/routing/v2/{hsl|finland}/gtfs/v1`,
  header `digitransit-subscription-key` (free key from portal-api.digitransit.fi).
  Recommended 0.5–1 s between requests for bulk use.
- **Car**: OSRM public demo `router.project-osrm.org/table/v1/driving`, max 100
  coordinates per request, free-flow times (no traffic).
- **Geocoding**: Nominatim, 1 req/s, custom User-Agent; `ISO3166-2-lvl4 == "FI-18"` ⇒ Uusimaa.

## Decisions

- Python 3.12, `httpx` + `PyYAML`, CLI `python -m househunt`.
- Transit only when both ends are in Uusimaa and a Digitransit key is set;
  otherwise car only.
- Transit time = shortest of the first 3 itineraries arriving by a set time
  (default next Tuesday 09:00).
- SQLite cache for geocodes and routes, coordinates rounded to ~100 m.
- Skip transit lookup when car time already exceeds that destination's transit limit.

## Tasks

### Research
- [x] Verify Oikotie search API, auth headers, filter codes
- [x] Verify Etuovi search API, region / type / rooms / plot filters
- [x] Verify OSRM demo table limits
- [x] Verify Digitransit endpoint + auth
- [x] Verify Nominatim Uusimaa detection

### MVP
- [x] Models: Listing, Filters, Destination, house-type / Uusimaa constants
- [x] Config loader (`config.yaml`) + `config.example.yaml`
- [x] Oikotie fetcher with filters + pagination
- [x] Etuovi fetcher with filters + pagination
- [x] Dedupe listings present on both sites
- [x] SQLite cache
- [x] Nominatim geocoder for destinations
- [x] OSRM car times (batched table)
- [x] Digitransit transit times (rate-limited, cached) — written against OTP schema, not yet run live
- [x] Ranking score (weighted minutes) + max-minute filters
- [x] Output: CSV + static HTML report (sortable table, map)
- [x] CLI `python -m househunt run -c config.yaml`
- [x] Unit tests for parsers / filter mapping (recorded fixtures)
- [x] End-to-end run against live APIs (car only)
- [ ] End-to-end run with Digitransit key
- [x] README

### Travel-time map
Goal: the map shows each house as a marker at its address, on top of a
colour layer of travel time to the selected destination: green up to the
limit set in config, shading through yellow/orange to red in areas that are
too far.

Design:
- Colour layer = regular grid of sample points (default 1 km cells) over the
  bounding box of the listings + destinations, padded ~5 km, clipped to Uusimaa.
- Each cell stores travel minutes per destination × mode (car via OSRM table,
  transit via Digitransit), cached like listing routes.
- Colour scale per destination × mode: green at ≤ `max_minutes`, then a gradient
  to red at `max_minutes × red_factor` (default 2) and above. Destinations
  without `max_minutes` use a `map.default_max_minutes` fallback (e.g. 45).
- Grey cells for "no route" (sea, no transit connection).
- Markers keep the same colour scale as a ring/fill, so a house reads the same
  as the area under it.

Tasks:
- [ ] Config: `map:` section — `cell_km`, `padding_km`, `red_factor`,
      `default_max_minutes`, `enabled`
- [ ] Grid generator: cells over padded bbox, drop cells outside Uusimaa
      (simplified Uusimaa polygon bundled as GeoJSON) and on water
- [ ] Car times for grid cells (batched OSRM table, cached)
- [ ] Transit times for grid cells (rate-limited, cached; log estimated request
      count and duration before starting; `--no-transit-grid` to skip)
- [ ] Colour function: minutes + limit → green→yellow→red, grey for none;
      unit tests for boundary values (at limit = green, ≥ limit × red_factor = red)
- [ ] HTML: grid as semi-transparent Leaflet rectangles (or a canvas layer)
      beneath the house markers
- [ ] HTML: selector for destination × mode that recolours grid and markers
- [ ] HTML: legend showing the scale with the actual minute values
- [ ] Markers coloured with the same scale; popup unchanged
- [ ] Check request volume on a real config (cells × destinations) and tune the
      default `cell_km` so a first run stays within Digitransit's fair use
- [ ] End-to-end run and visual check of the map (car only, then with transit)

### Later / ideas
- [ ] Rentals (Oikotie `cardType=101`, Vuokraovi)
- [ ] Incremental runs: only route new listings, flag new / price-changed
- [ ] Self-hosted OSRM/OTP if public servers become limiting
