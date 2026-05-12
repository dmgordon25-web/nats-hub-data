# nats-hub-data

Backend pipeline for the [nats-hub](https://github.com/dmgordon25-web/nats-hub) Google TV app — Washington Nationals concierge for senior fans.

This repo polls free public sources (MLB stats API, ESPN, Open-Meteo) every few minutes during game windows, normalizes the data into a single JSON payload, and publishes it to GitHub Pages. The TV app reads from one URL and gets everything: schedule, live game state, probable pitchers, injuries, standings, weather, former-Nats tracker, provider routing rules, and tidbits.

> **Why a backend at all?** Free sources are fragile. Putting a normalization layer here means: when ESPN's HTML changes or MLB rotates a path, we fix it in one place and the TV app keeps working from cache. Senior-proof.

## What's published

GitHub Pages serves `docs/` at:

```
https://dmgordon25-web.github.io/nats-hub-data/
├── nationals.json     # The single payload the app reads on every refresh
├── former_nats.json   # Former-Nats tracker (also embedded in nationals.json)
├── providers.json     # Provider routing rules (app caches, refreshes daily)
├── tidbits.json       # Curated content pack
└── version.json       # App auto-update check
```

The TV app's primary network call is to `nationals.json`. Everything else is lazy-loaded.

## Repo layout

```
nats-hub-data/
├── scripts/
│   ├── common.py            # Shared HTTP, JSON, cache, team-id helpers
│   ├── fetch_schedule.py    # MLB stats API → schedule + probables + broadcasts
│   ├── fetch_standings.py   # MLB stats API → NL East + Nats streak
│   ├── fetch_injuries.py    # ESPN HTML scrape (embedded JSON block)
│   ├── fetch_former_nats.py # MLB stats API for tracked ex-Nationals
│   ├── fetch_weather.py     # Open-Meteo for next ballpark
│   └── normalize.py         # Combine cache/* into docs/nationals.json
├── content/                 # Editable, hand-curated
│   ├── former_nats_roster.json    # Who to track (edit anytime, push, done)
│   ├── providers.json             # Routing rules: broadcast name → app intent
│   ├── significant_events.json    # Thresholds for "did something cool" flags
│   ├── tidbits.json               # Curated nuggets for home-screen rotation
│   └── injury_overrides.json      # (optional) hand override the scraper
├── cache/                   # Last-good payloads from each source
├── docs/                    # Published artifacts (GitHub Pages serves this)
└── .github/workflows/
    ├── poll-frequent.yml    # Every 10 min during game windows
    ├── poll-daily.yml       # 4× daily for slow-changing data
    └── publish-pages.yml    # Deploy docs/ to Pages on every push
```

## Senior-proof reliability rules

Every fetcher follows the same pattern via `preserve_last_good()`:

1. **Try fresh.** Hit the upstream source with retry + backoff.
2. **Validate.** Each source has a `is_valid()` predicate (e.g. "must have a games array").
3. **Preserve on failure.** Bad fetches *never* overwrite good cache. Cache file gets `_stale: true` flag and `_stale_since` timestamp.
4. **Surface staleness.** `normalize.py` reads each cache file and includes a `stale_sources` list in the published JSON so the app can show "as of Tuesday 2:30 PM" instead of going blank.

This means: if ESPN's injury page returns garbage at 2:30 PM, the app keeps showing the 2:00 PM version. If it's still bad at midnight, the app shows yesterday's data with a quiet timestamp. The Watch button (which depends on the schedule, not injuries) is unaffected.

## Local development

```bash
pip install -r requirements.txt
python scripts/fetch_schedule.py
python scripts/fetch_standings.py
python scripts/fetch_injuries.py
python scripts/fetch_former_nats.py
python scripts/fetch_weather.py
python scripts/normalize.py
# Inspect: cat docs/nationals.json | jq '.focus_game'
```

A clean run from empty `cache/` takes ~15 seconds (most of it is the per-player Former Nats lookups, ~1 sec each).

## Updating things by hand (no code changes)

Most things you'd want to fix during the season are config edits. Push to `main` and the next poll cycle picks them up — typically within 10 minutes.

| What changed | Edit | App sees it |
|---|---|---|
| Provider rights shifted (e.g., a game now on a service we don't map) | `content/providers.json` — add/edit a rule | Within 10 min |
| Former Nats roster needs adjustment | `content/former_nats_roster.json` | Within 4 hr (daily poll) |
| Injury scraper broke and needs a manual override | `content/injury_overrides.json` (created on demand) | Within 4 hr |
| New tidbit / nugget / fun fact | `content/tidbits.json` — append to `entries` | Within 4 hr |
| Significance threshold for ex-Nat flags | `content/significant_events.json` | Within 4 hr |
| App APK update available | `docs/version.json` — bump `latest_app_version` and set `apk_url` | Within 1 hr (app polls hourly) |

## Data sources

| Source | What it gives | Failure mode |
|---|---|---|
| `statsapi.mlb.com` | Schedule, probables, linescore, broadcasts, standings, player stats | Falls back to ESPN scoreboard for schedule |
| `site.api.espn.com` (scoreboard) | Schedule fallback | Falls back to last-known-good cache |
| `espn.com/mlb/team/injuries/_/name/wsh` | Injury list (embedded JSON in HTML) | Falls back to last-known-good + manual override |
| `api.open-meteo.com` | Weather forecast | Falls back to last-known-good (or "Weather unavailable" copy) |

All sources are free and require no API keys. Open-Meteo's free tier allows up to 10K requests/day; we use far less than that.

## Polling schedule

GitHub Actions cron is best-effort and may delay 5–15 min. That's fine for our purposes.

| Workflow | Cadence | What it does |
|---|---|---|
| `poll-frequent.yml` | Every 10 min, 12:00–05:00 UTC (covers afternoon → late West Coast) | Schedule, standings, weather + normalize |
| `poll-daily.yml` | 4× per day | Injuries, former Nats, full refresh |
| `publish-pages.yml` | On every push to `main` that touches `docs/` | Deploy to Pages |

Estimated GitHub Actions usage: ~400 free-tier minutes/month. Free tier gives 2,000.

## Companion repo

The TV app lives at [dmgordon25-web/nats-hub](https://github.com/dmgordon25-web/nats-hub).
