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
- Map tiles: OpenStreetMap, report served from `127.0.0.1` via
  `python -m househunt serve` (or `run --serve`). OSM needs a Referer, which
  `file://` never sends; the page shows a banner when opened as a file.
  Rejected: CARTO (needs an API key; answers 200 with an "API KEY REQUIRED"
  image, so check tile content, not status), Esri Light Gray (label layer
  empty at street zooms), Esri Street Map (works, but OSM looks better).

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
- [x] `serve` command / `run --serve`: local HTTP server so OSM tiles load
- [x] Unit tests for parsers / filter mapping (recorded fixtures)
- [x] End-to-end run against live APIs (car only)
- [ ] End-to-end run with Digitransit key
- [x] README

### Progress & feedback
Observed: a run sat on "Querying 666 transit routes" with no output. At the
default 2 req/s that is ~6 min, up to ~11 min when the `hsl` router finds nothing
and the `finland` fallback runs too, plus retry backoff on 429/5xx.

- [ ] Progress bar for transit queries (done / total, rate, ETA), on stderr;
      `tqdm` or a small built-in counter. Count cache hits separately.
- [ ] Progress for listing fetch (pages per source) and car table batches
- [ ] Log an up-front estimate: uncached pairs, expected duration at the
      configured `requests_per_second`
- [ ] Log retries/backoff (429, 5xx) and fallback-router usage at INFO so a slow
      run is distinguishable from a hung one
- [ ] Ctrl-C keeps finished results: cache is already written per route; make
      sure an interrupted run exits cleanly and the next run resumes from cache

### Travel-time map
Goal: the map shows each house as a marker at its address, on top of a
colour layer of travel time to the selected destination: green up to the
limit set in config, shading through yellow/orange to red in areas that are
too far.

Design:
- No extra routing: the colour layer is interpolated from the travel times
  already computed for the houses that match the filters. Adequate for now;
  refine later with more data points.
- Interpolation in the browser: inverse-distance weighting (IDW) over the
  houses' minutes per destination × mode, drawn on a canvas layer. Fade out
  beyond ~3 km from the nearest house so empty areas aren't coloured from
  far-off data.
- Colour scale per destination × mode: green at ≤ `max_minutes`, then a gradient
  to red at `max_minutes × red_factor` (default 2) and above. Destinations
  without `max_minutes` use `map.default_max_minutes` (e.g. 45).
- Houses with no time for the selected mode (no route, outside Uusimaa for
  transit) are grey and left out of the interpolation.
- Markers use the same colour scale, so a house reads the same as the area
  under it.

Tasks:
- [ ] Log each house's travel times per destination × mode (INFO line per
      house: address, municipality, minutes) so they're visible outside the report
- [ ] Config: `map:` section — `red_factor`, `default_max_minutes`,
      `fade_km`, `idw_power`
- [ ] Colour function: minutes + limit → green→yellow→red, grey for none;
      unit tests for boundary values (at limit = green, ≥ limit × red_factor = red)
- [ ] HTML: IDW canvas layer beneath the house markers
- [ ] HTML: selector for destination × mode that recolours layer and markers
- [ ] HTML: legend showing the scale with the actual minute values
- [ ] Markers coloured with the same scale; popup unchanged
- [ ] End-to-end run and visual check of the map (car only, then with transit)

### Later / ideas
- [ ] More data points for the colour layer: extra sample points in sparse
      areas (grid cells with no house nearby), routed and cached like houses
- [ ] Rentals (Oikotie `cardType=101`, Vuokraovi)
- [ ] Incremental runs: only route new listings, flag new / price-changed
- [ ] Self-hosted OSRM/OTP if public servers become limiting
