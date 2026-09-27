# househunt

Fetches homes for sale in Uusimaa from Etuovi and Oikotie, computes travel times
to your destinations by public transport (Digitransit) and car (OSRM), and ranks
them on a map. Runs as a web app (settings, runs and results in the browser) or
as a command-line tool that writes `output/listings.csv` and `output/listings.html`.

## Web app

Try it locally in the same container setup as the server:

```sh
./run-local.sh            # builds, creates .env with a generated password, serves http://localhost:8000
./run-local.sh up         # same, in the background (then: logs, down)
./run-local.sh import config.yaml   # turn a CLI config into a saved search
./run-local.sh reset      # delete the local data volume (asks first)
```

Add `DIGITRANSIT_API_KEY` to `.env` for public transport times. Saved searches,
results and the route cache persist across `down`/`up` in the `househunt-local`
Docker volume; only `reset` deletes them.

On the server:

```sh
cp .env.example .env                 # set HOUSEHUNT_PASSWORD and DIGITRANSIT_API_KEY
cp compose.example.yaml compose.yaml
docker compose up -d --build         # listens on 127.0.0.1:8000
```

Put it behind your reverse proxy with TLS; the session cookie is marked `Secure`
when the proxy sends `X-Forwarded-Proto: https`. Data (saved searches, results,
listing history, route cache) lives in the `/data` volume. If you bind-mount a
host directory instead of the named volume, make it writable for the container
user: `chown 10001 <dir>`.

Without Docker:

```sh
uv venv && uv pip install -r requirements.txt
HOUSEHUNT_PASSWORD=... DIGITRANSIT_API_KEY=... .venv/bin/python -m househunt web --data-dir data
.venv/bin/python -m househunt web --no-auth   # local only, 127.0.0.1
```

Environment:

| Variable | Meaning |
|---|---|
| `HOUSEHUNT_PASSWORD` / `HOUSEHUNT_PASSWORD_FILE` | Admin password (required unless `--no-auth`) |
| `DIGITRANSIT_API_KEY` | Public transport routing; without it only car times |
| `HOUSEHUNT_DIGITRANSIT_RPS` | Digitransit requests per second (default 2) |
| `HOUSEHUNT_DATA_DIR`, `HOUSEHUNT_HOST`, `HOUSEHUNT_PORT` | Defaults `data`, `127.0.0.1`, `8000` |
| `HOUSEHUNT_TRUSTED_PROXIES` | Proxy addresses allowed to set forwarded headers |
| `HOUSEHUNT_DEFAULT_DAILY_RUNS` | Manual runs per day for invited users (default 5) |
| `HOUSEHUNT_DEFAULT_MAX_LISTINGS` | Listing cap per site for invited users (default 300) |
| `HOUSEHUNT_FETCH_CACHE_HOURS` | How long fetched listing pages are reused (default 6) |

### Accounts

An account is just a password. `HOUSEHUNT_PASSWORD` is the admin's. Under
**Admin**, create an invite link and send it; opening it creates the account and
shows its generated password once. Each person sees only their own searches,
including the admin. The admin can rename, disable or delete accounts, issue a
new password (ends that person's sessions), and set per-user limits.

Everyone shares the scraping budget: runs go through one queue, a person can
have one run going at a time and a daily number of manual runs, and listing
pages fetched with identical filters are reused for a few hours. Route times are
cached for everyone.

In the app: create a search (filters, destinations with address search, transit
time, map colours, optional daily refresh), press **Run now**, and follow the
progress. Runs happen one at a time on the server and keep going if the page is
closed. Each run records when a listing was first seen and its price changes;
the table and popups show **new** and **price −/+** badges. Import an existing
YAML config with `python -m househunt web --import-config config.yaml`.

## Command line

```sh
uv venv && uv pip install -r requirements-dev.txt
cp config.example.yaml config.yaml   # edit filters and destinations
export DIGITRANSIT_API_KEY=...       # free: https://portal-api.digitransit.fi/
.venv/bin/python -m househunt run -c config.yaml --serve
```

`--serve` opens the report at `http://localhost:8765/listings.html`. To reopen the
latest report later: `.venv/bin/python -m househunt serve`. Opening the HTML file
directly works for the table, but the OpenStreetMap tiles need a Referer that
`file://` pages don't send, so the map stays blank.

Without a Digitransit key only car times are computed. A progress bar shows on
the terminal; Ctrl-C stops the run, and finished routes stay cached.

## Config

- `filters`: `house_types` (kerrostalo, rivitalo, paritalo, erillistalo,
  omakotitalo, luhtitalo, puutalo-osake), `rooms` (1–5, 5 = 5+),
  `plot_ownership` (own, rent), `price_min/max`, `size_min/max`, `municipalities`.
- `destinations`: `address` or `lat`/`lon`, `modes` (transit, car), `weight`,
  `max_minutes` per mode.
- `max_listings`: cap per source.
- `transit`: `arrive_by` or `depart_at`, `day` (next occurrence of that weekday).

The map colours the area around the houses by travel time. Green up to
`max_minutes × map.green_factor` (default half the limit), shading to fully red at
`max_minutes × map.red_factor` (default the limit itself). Destinations without
`max_minutes` use `map.default_max_minutes`.

"Colour by" picks what drives the colours:
- **All destinations · worst** (default): each destination's time as a percentage
  of its own limit, and the worst one counts. One short trip can't make a house
  green if another one is long.
- **All destinations · weighted average**: the same percentages averaged with the
  destination weights.
- A single destination and mode, in minutes.

While a combined view is selected, "Include" checkboxes pick which destinations
count (e.g. leave out one you visit rarely). The browser remembers the choice.

The combined views use the same mode per destination as `score`. The layer is
interpolated from the houses' own times, so it only covers areas near listings.
Houses over a `max_minutes` limit are hidden unless "Show houses over the limit" is
ticked, but still feed the colours.

`score` is the weighted sum of minutes to each destination, using the first mode
in that destination's `modes` that has a time. Lower is better.

## Notes

- Transit times only when both ends are in Uusimaa; otherwise car only.
- Car times are free-flow (no rush-hour traffic).
- Etuovi and Oikotie are queried through their undocumented website APIs; they
  may change without notice. Keep `max_listings` modest.
- Routes are cached in `.cache/househunt.sqlite` (CLI) or `data/cache.sqlite`
  (web); delete it to recompute.
- Tests: `.venv/bin/python -m pytest`.
