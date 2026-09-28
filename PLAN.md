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
- Address search only on the Find button: Nominatim's usage policy forbids autocomplete.
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
- [x] End-to-end run with Digitransit key (2026-09-28, via the web app: 98/129 homes got transit times, 7 needed the `finland` router)
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
- [ ] Visual check of the map with transit times
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
- [x] Server: OS, Docker available? Existing reverse proxy (nginx/Caddy/Traefik)? → deployed by the user
- [x] Single user, or family/shared with separate logins? → invite-only shared (see Multiple users)
- [x] Public domain or LAN/VPN only? → deployed by the user

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
- [x] Deploy to the user's server behind their reverse proxy (live at https://asuntohaku.lith.fi, 2026-09-28)

### Multiple users (roadmap)
Goal: a few invited people (family, friends) each with their own searches, on
the same server. Not open sign-up: bulk fetching from Etuovi/Oikotie would get
the server blocked, so load must stay close to single-user levels.

Decisions (user, 2026-09-27): local accounts; an account is just a password,
as today, no usernames; invite-only via admin-created links; reuse cached data
across users but keep searches strictly separate.

Design:
- **Account = password.** Users table: id, label (admin-set, e.g. "Mum", only
  shown to the admin), role admin/user, disabled flag, created/last-seen.
- **Passwords are generated, not chosen:** redeeming an invite shows a random
  passphrase once (e.g. 5 words from a word list or 20 random chars, ≥ 100 bits).
  This rules out two users picking the same password, and weak ones.
- **Lookup by password:** store `HMAC-SHA256(server_secret, password)` with a
  unique index; sign-in hashes the input and looks it up directly. Plain HMAC is
  enough because generated passwords are high-entropy (no scrypt needed); the
  server secret keeps a leaked DB from being checked offline.
- **Admin:** `HOUSEHUNT_PASSWORD` stays the admin password (bootstrap on first
  start). Existing profiles are assigned to the admin.
- **Invites:** admin creates a single-use link (random token, stored hashed,
  7-day expiry, optional label). Opening it creates the user and shows their
  password once, with a "copy" button and a note to save it in a password manager.
  Admin can regenerate a user's password (ends their sessions) or disable them.
- **Sessions:** sessions table (random token stored hashed, user id, expiry,
  last seen) replaces the stateless HMAC cookie, so they can be revoked.
- **Strict separation of searches:** `profiles.user_id`; every profile, run,
  results and CSV endpoint filters by the signed-in user and returns 404 for
  anyone else's. Admin gets no view into others' searches, only user management
  and the run queue (profile names hidden).
- **Shared caches (no user data in them):**
  - Route and geocode cache as today (keyed by rounded coordinates).
  - Listing fetch cache keyed by (source, normalized server-side filters, date),
    so identical filters on the same day reuse pages instead of re-fetching.
  - Scheduled refreshes grouped: each distinct filter set fetched once a day,
    routed per profile.
- **Scrape budget:** single worker and global per-service rate limiters for all
  users; per user one active run and a daily run quota (admin-set, default ~5);
  `max_listings` capped for non-admins.

Tasks:
- [x] Decide: local accounts vs proxy forward-auth → local accounts, password-only
- [x] Schema: users, sessions, invites; `profiles.user_id`; migrate existing data to admin
- [x] Auth: HMAC password lookup, DB sessions, global login throttle
- [x] Invite flow: admin creates link (label, expiry); redeem shows generated password once
- [x] Admin page: users (label, last seen, disable, regenerate password, quota), invites, run queue
- [x] Scope all profile/run/results/CSV endpoints by user; tests that user B gets 404 on A's data
- [x] Per-user quotas: one active run, daily run limit, `max_listings` cap
- [x] Listing fetch cache per (source, filters, day); grouped scheduled refreshes
- [x] Frontend: sign out everywhere; admin link in the top bar for the admin
- [x] Grouped scheduled refreshes come from the fetch cache: same filters on the same
      day hit the cache (TTL `HOUSEHUNT_FETCH_CACHE_HOURS`, default 6), no separate grouping
- [x] Review fixes: run quota survives deleting a search (run_log table), scheduled runs
      once per search per day and one active run per user, 20 searches / 3 refreshing per
      user, max 10 destinations, listing cap ≥ 1, strict boolean for disabled, disabling
      cancels the user's run, session lookups off the event loop, refuse to start if
      session.key is new while users exist, fetch-cache TTL ≤ 24 h, old cache entries
      treated as misses, indexed pruning, invite page warns when already signed in.
      Kept the hard login lockout (a growing delay doesn't bound parallel guessing).
- [x] Live check in the local container: migration moved the existing search to admin,
      invite single-use (410 after), admin gets 404 on a user's search, quota counts,
      an identical second search reused both sites' pages and all routes

### Travel modes, build year, nearby services
Requested 2026-09-28.

Design:
- **Rush-hour car times:** OSRM is free-flow; multiply by `car.rush_hour_factor`
  (default 1.2, conservative). The transit shortcut keeps using the raw free-flow
  time as its lower bound.
- **Cycling and walking:** FOSSGIS OSRM tables (`routing.openstreetmap.de/routed-bike`,
  `/routed-foot`), batched like car (≤ 100 coordinates), cached, shared 1 req/s
  limiter. Verified 2026-09-28: bike ≈ 14 km/h, foot ≈ 4.5 km/h. New destination
  modes `bike`, `walk`; walking is only realistic for short distances, so it's
  opt-in per destination.
- **Build year filter:** `build_year_min/max`. Oikotie `constructionYear[min|max]`,
  Etuovi `yearMin/yearMax` (both verified), plus client-side check.
- **Nearby services:** nearest daycare (`amenity=kindergarten`), school
  (`amenity=school`), grocery store (`shop=supermarket`; `convenience` also matches
  kiosks) by straight-line distance. One Overpass query for all of Uusimaa
  (~1.4 MB, ~16 s, verified), cached 7 days and shared by everyone; a stale copy is
  used if Overpass fails, and the run continues without services if there's none.

Tasks:
- [x] Rush-hour factor in config/settings, applied to displayed and scored car times
- [x] Generic OSRM table router; bike and walk modes end to end (config, pipeline, UI, colour views)
- [x] Build year filter (config, both sources, form)
- [x] Overpass fetch + cache, nearest-service distances, columns and popup
- [x] Tests, live check, docs
- [x] Columns and popup lines only for the modes a destination uses (car is still
      computed for the transit shortcut). Overpass often answers 504 when busy; 4 attempts.

### Limits follow the settings
- [x] Default max minutes 60 for a destination's main mode when it has no max; used for both
      hiding and colours
- [x] Stored results re-evaluated on load (limits, weights, colours) using a settings
      fingerprint; routing-relevant changes mark results stale with a "run again" notice
- [x] Removed the "(rush hour ×1.2)" suffix from listing labels

### Later / ideas
- [ ] More data points for the colour layer: extra sample points in sparse
      areas (grid cells with no house nearby), routed and cached like houses
- [ ] Rentals (Oikotie `cardType=101`, Vuokraovi)
- [ ] Self-hosted OSRM/OTP if public servers become limiting
