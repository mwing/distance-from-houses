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

- [x] Progress bar for transit queries (done / total, rate, ETA), on stderr;
      `tqdm` or a small built-in counter. Count cache hits separately.
- [x] Progress for listing fetch (pages per source) and car table batches
- [x] Log an up-front estimate: uncached pairs, expected duration at the
      configured `requests_per_second`
- [x] Log retries/backoff (429, 5xx) and fallback-router usage at INFO so a slow
      run is distinguishable from a hung one
- [x] Ctrl-C keeps finished results: cache is already written per route; make
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
- Colour scale per destination × mode: green at ≤ `max_minutes × green_factor`
  (default 0.5), gradient to red at `max_minutes × red_factor` (default 1) and above. Destinations
  without `max_minutes` use `map.default_max_minutes` (e.g. 45).
- Houses with no time for the selected mode (no route, outside Uusimaa for
  transit) are grey and left out of the interpolation.
- Markers use the same colour scale, so a house reads the same as the area
  under it.

Tasks:
- [x] Log each house's travel times per destination × mode (DEBUG, shown with `-v`;
      at INFO it drowned the run output with hundreds of lines)
- [x] Config: `map:` section — `red_factor`, `default_max_minutes`,
      `fade_km`, `idw_power`
- [x] Colour function: minutes + limit → green→yellow→red, grey for none;
      unit tests for boundary values (at limit = green, ≥ limit × red_factor = red)
- [x] HTML: IDW canvas layer beneath the house markers
- [x] HTML: selector for destination × mode that recolours layer and markers
- [x] HTML: legend showing the scale with the actual minute values
- [x] Markers coloured with the same scale; popup unchanged
- [x] Over-limit houses kept (flagged `within_limits: false`) so the colour layer
      has red data; hidden from table/markers unless "Show houses over the limit"
- [x] Drop listings with coordinates outside the Uusimaa bbox (seen on both sites
      for one Mäntsälä listing placed near Mikkeli); transit eligibility by bbox,
      not municipality name (Etuovi gives villages like "Nummela")
- [x] End-to-end run and visual check of the map, car only (headless Chrome screenshot)
- [x] Scale: green up to `green_factor` × limit (default 0.5), red at `red_factor` × limit (default 1)
- [x] Combined colour views: worst and weighted average of each destination's
      time as % of its limit (score mode per destination); worst is the default
- [x] "Include" checkboxes for the combined views, remembered in localStorage
- [ ] Visual check with transit times
- [ ] Municipality filter misses village names from Etuovi ("Nummela" for Vihti);
      map villages → municipality or filter by postcode

### Web app
Goal: everything in the browser — settings, starting a run, progress, map and
table — with no YAML or terminal. Runs on the user's own web server.

Why a backend at all: Etuovi and Oikotie send no CORS headers (verified
2026-09-26) and Oikotie's token comes from its HTML, so a browser can't fetch
listings directly. Digitransit, OSRM and Nominatim do allow browser calls, but
routing stays server-side anyway (below).

Design — backend does the work, browser renders:
- **Backend** (Python, reuses `househunt` as-is): FastAPI + uvicorn, SQLite in a
  data dir. Owns listing fetch, geocoding, routing, the route cache, the
  Digitransit key (env var, never sent to the browser) and run jobs.
- **Why not in the browser:** the shared cache is reused across devices and
  users; long transit runs (~6 min for 666 routes) survive a closed tab;
  scheduled refreshes; no TypeScript port of working code.
- **Browser** (static files served by the same app, vanilla JS + Leaflet, no
  build step — same as the current report): settings forms, run button +
  progress, map/table. Colour layer, "Colour by", "Include", sorting and the
  over-limit toggle stay client-side (instant, no round-trip).
- **Profiles:** a named search = filters + destinations + transit + map
  settings, stored in SQLite (the YAML config is replaced; CLI keeps working by
  importing a YAML into a profile or reading it directly).
- **Runs:** `POST` starts a background job (one per profile at a time); progress
  events (phase, done/total, cache hits, ETA) polled or via SSE. Same progress
  hooks feed the CLI progress bar from "Progress & feedback".
- **Listings history:** store every listing per profile with `first_seen`,
  `last_seen`, price history → "new" / "price dropped" / "gone" badges.
- **Scheduled refresh:** daily per profile; only new listings get routed.
- **Access:** behind the user's reverse proxy with TLS; app-level auth (single
  shared password → session cookie, or proxy basic auth). The fetch endpoints
  must never act as an open proxy to Etuovi/Oikotie.
- **Politeness:** one global rate limiter per external service in the backend
  (Nominatim 1/s, OSRM ~1/s, Digitransit configurable) so concurrent runs
  can't exceed limits.

API sketch:
- `GET/POST /api/profiles`, `GET/PUT/DELETE /api/profiles/{id}`
- `GET /api/geocode?q=` — destination search box (Nominatim, cached)
- `POST /api/profiles/{id}/runs`, `GET /api/runs/{id}` (status + progress),
  `GET /api/runs/{id}/events` (SSE)
- `GET /api/profiles/{id}/results` — the JSON the report's `DATA` holds today
  (rows, dests, columns, map settings) + listing history badges
- `GET /api/profiles/{id}/results.csv`
- Progress is polled (`GET /api/runs/{id}` every 1.5 s); the SSE endpoint was
  dropped as unnecessary for one-at-a-time runs.

Open questions for the user (answer before the deploy tasks):
- [ ] Server: OS, Docker available? Existing reverse proxy (nginx/Caddy/Traefik)?
- [ ] Single user, or family/shared with separate logins?
- [ ] Public domain or LAN/VPN only?

Tasks:
- [x] Split `pipeline.run` into steps with a progress callback (fetch pages,
      car batches, transit pairs) and cancellation
- [x] Settings model shared by YAML loader and API (`config_from_dict`, not pydantic), with the same
      validation as `config.py`
- [x] SQLite schema: profiles, runs, listings (per profile, history), keep the
      existing kv route cache; data dir from env
- [x] FastAPI app: profiles CRUD, geocode, runs (background thread worker),
      results JSON/CSV; serve static frontend
- [x] Global per-service rate limiters shared by all jobs
- [x] Auth: password → signed session cookie; all `/api` routes protected
- [x] Frontend: profile list + settings form (house types, rooms, plot, price,
      size, municipalities; destinations with geocode search, modes, weight,
      max minutes; transit arrive/depart time and day; map factors)
- [x] Frontend: run button, progress bar with ETA, cancel
- [x] Frontend: results view — move the report's map/table JS into a module
      that renders from `/results` (static report export keeps using it)
- [x] Listing history: first/last seen, price changes, badges in table and popup
- [x] Scheduled daily refresh per profile (in-process scheduler)
- [x] CLI: `househunt web` to start the server; `run` keeps working
- [x] Tests: API (profiles, run lifecycle with stubbed sources/routers), auth
- [x] Dockerfile + compose example (volume for data dir, env for key/password)
- [x] Local checks: live run through the API (car only), headless-Chrome
      screenshots of all views (phone check at 500 px: headless Chrome on macOS
      won't lay out narrower), container smoke test (auth 401/200, static)
- [x] Review (manual, CodeRabbit CLI not installed) and fixes: login throttle + async delay,
      malformed-cookie 500, password change ends sessions, session key file created 0600
      and regenerated if short, cross-site POST guard (Sec-Fetch-Site), CSP pinned to the
      Leaflet path, CSV formula neutralising, `<` escaped in the report JSON, duplicate
      listing keys, cancel-before-start race, all-sources-failed fails the run, NaN and
      negative-weight validation, scheduler retries interrupted runs, WAL on the route cache,
      healthcheck port. Kept the direct X-Forwarded-Proto check for the Secure flag (works
      even when HOUSEHUNT_TRUSTED_PROXIES doesn't match the proxy).
- [x] `run-local.sh`: the server's Docker setup on localhost (generated password, import, reset)
- [ ] Deploy to the user's server behind their reverse proxy; smoke test on phone
- [ ] Nominatim policy forbids autocomplete: address search stays behind the Find
      button; keep it that way if the form is reworked

### Later / ideas
- [ ] More data points for the colour layer: extra sample points in sparse
      areas (grid cells with no house nearby), routed and cached like houses
- [ ] Rentals (Oikotie `cardType=101`, Vuokraovi)
- [ ] Self-hosted OSRM/OTP if public servers become limiting
