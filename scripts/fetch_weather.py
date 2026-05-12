"""Fetch weather forecast for the next Nationals game's ballpark.

Source: Open-Meteo (free, no API key, no rate limit for personal use).
Reads cache/schedule.json for venue lat/lon + start time.

Output: cache/weather.json
{
  "fetched_at": "...",
  "source": "open_meteo" | "cache",
  "game_pk": int,
  "venue_name": "...",
  "forecast_at": "2026-05-12T19:05:00Z",
  "temperature_f": 68,
  "feels_like_f": 65,
  "wind_mph": 8,
  "wind_dir": "SW",
  "precip_chance": 20,
  "precip_inches": 0.0,
  "summary": "Partly cloudy",
  "is_dome": bool
}
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from common import (
    CACHE_DIR,
    http_get_json,
    main_runner,
    now_iso,
    preserve_last_good,
    read_json,
)

log = logging.getLogger("fetch_weather")

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Domed/retractable parks where outdoor weather is largely moot.
# Venue ids confirmed against statsapi.mlb.com /api/v1/venues for 2026.
DOMED_VENUES = {
    12: "Tropicana Field (TB, fixed dome)",
    14: "Rogers Centre (TOR, retractable)",
    15: "Chase Field (ARI, retractable)",
    32: "American Family Field (MIL, retractable)",
    680: "T-Mobile Park (SEA, retractable)",
    2392: "Daikin Park (HOU, retractable; formerly Minute Maid)",
    4169: "loanDepot park (MIA, retractable)",
    5325: "Globe Life Field (TEX, retractable)",
}


def _wind_dir(deg: float | None) -> str | None:
    if deg is None:
        return None
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[int((deg + 11.25) // 22.5) % 16]


WMO_SUMMARY = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 48: "Foggy",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow",
    80: "Rain showers", 81: "Heavy showers", 82: "Violent showers",
    95: "Thunderstorms", 96: "Thunderstorms with hail", 99: "Severe thunderstorms",
}


def find_next_game(schedule: dict[str, Any]) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc)
    upcoming = []
    for g in schedule.get("games") or []:
        st = g.get("start_time_utc")
        if not st:
            continue
        try:
            dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
        except ValueError:
            continue
        # Include in-progress and not-yet-started; prefer the soonest future,
        # but if a game is live now, that's "next" too.
        if g.get("status") == "Live" or dt >= now:
            upcoming.append((dt, g))
    if not upcoming:
        return None
    upcoming.sort(key=lambda x: x[0])
    return upcoming[0][1]


def fetch_weather_at(lat: float, lon: float, when_iso: str) -> dict[str, Any] | None:
    """Open-Meteo hourly forecast - we'll pick the hour closest to game time."""
    return http_get_json(
        OPEN_METEO_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,apparent_temperature,precipitation_probability,precipitation,wind_speed_10m,wind_direction_10m,weather_code",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "forecast_days": 3,
            "timezone": "UTC",
        },
        label=f"open-meteo {lat},{lon}",
    )


def pick_hour_index(hourly_times: list[str], when_iso: str) -> int:
    target = datetime.fromisoformat(when_iso.replace("Z", "+00:00"))
    best_i, best_delta = 0, None
    for i, t in enumerate(hourly_times):
        try:
            dt = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        d = abs((dt - target).total_seconds())
        if best_delta is None or d < best_delta:
            best_i, best_delta = i, d
    return best_i


def main() -> int:
    sched = read_json(CACHE_DIR / "schedule.json")
    if not sched:
        log.error("No schedule cache; run fetch_schedule first")
        return 1
    game = find_next_game(sched)
    if not game:
        log.info("No upcoming game found; nothing to fetch")
        # Don't preserve stale weather; just write an empty payload.
        from common import write_json
        write_json(CACHE_DIR / "weather.json", {
            "fetched_at": now_iso(),
            "source": "none",
            "game_pk": None,
            "venue_name": None,
            "summary": "No upcoming game",
        })
        return 0

    venue = game.get("venue") or {}
    venue_id = venue.get("id")
    is_dome = venue_id in DOMED_VENUES

    if is_dome:
        log.info("Venue %s is domed/retractable; skipping forecast", venue.get("name"))
        from common import write_json
        write_json(CACHE_DIR / "weather.json", {
            "fetched_at": now_iso(),
            "source": "open_meteo",
            "game_pk": game.get("game_pk"),
            "venue_name": venue.get("name"),
            "forecast_at": game.get("start_time_utc"),
            "is_dome": True,
            "summary": "Indoor / retractable roof",
        })
        return 0

    lat, lon = venue.get("lat"), venue.get("lon")
    if lat is None or lon is None:
        log.warning("Venue %s missing coordinates", venue.get("name"))
        from common import write_json
        write_json(CACHE_DIR / "weather.json", {
            "fetched_at": now_iso(),
            "source": "none",
            "game_pk": game.get("game_pk"),
            "venue_name": venue.get("name"),
            "summary": "Weather unavailable",
        })
        return 0

    raw = fetch_weather_at(lat, lon, game["start_time_utc"])
    fresh: dict[str, Any] | None = None
    if raw and raw.get("hourly"):
        h = raw["hourly"]
        idx = pick_hour_index(h.get("time") or [], game["start_time_utc"])
        code = (h.get("weather_code") or [None])[idx]
        fresh = {
            "fetched_at": now_iso(),
            "source": "open_meteo",
            "game_pk": game.get("game_pk"),
            "venue_name": venue.get("name"),
            "forecast_at": (h.get("time") or [None])[idx],
            "temperature_f": (h.get("temperature_2m") or [None])[idx],
            "feels_like_f": (h.get("apparent_temperature") or [None])[idx],
            "wind_mph": (h.get("wind_speed_10m") or [None])[idx],
            "wind_dir": _wind_dir((h.get("wind_direction_10m") or [None])[idx]),
            "precip_chance": (h.get("precipitation_probability") or [None])[idx],
            "precip_inches": (h.get("precipitation") or [None])[idx],
            "summary": WMO_SUMMARY.get(code, "Forecast"),
            "is_dome": False,
        }

    final = preserve_last_good(fresh, CACHE_DIR / "weather.json")
    log.info(
        "Weather: %s, %s°F, %s",
        final.get("venue_name"),
        final.get("temperature_f"),
        final.get("summary"),
    )
    return 0


if __name__ == "__main__":
    main_runner(main)
