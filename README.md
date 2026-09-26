# househunt

Fetches homes for sale in Uusimaa from Etuovi and Oikotie, computes travel times
to your destinations by public transport (Digitransit) and car (OSRM), and ranks
them. Output: `output/listings.csv` and `output/listings.html` (sortable table + map).

## Setup

```sh
uv venv && uv pip install -r requirements.txt
cp config.example.yaml config.yaml   # edit filters and destinations
export DIGITRANSIT_API_KEY=...       # free: https://portal-api.digitransit.fi/
.venv/bin/python -m househunt run -c config.yaml --serve
```

`--serve` opens the report at `http://localhost:8765/listings.html`. To reopen the
latest report later: `.venv/bin/python -m househunt serve`. Opening the HTML file
directly works for the table, but the OpenStreetMap tiles need a Referer that
`file://` pages don't send, so the map stays blank.

Without a Digitransit key only car times are computed.

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
- Results are cached in `.cache/househunt.sqlite`; delete it to recompute.
