"""Fetch Washington Nationals schedule + game state from MLB Stats API.

Primary source: statsapi.mlb.com (unofficial but stable).
Fallback: ESPN scoreboard JSON.

Output: cache/schedule.json with normalized shape:
{
  "fetched_at": "2026-05-12T...",
  "source": "mlb_statsapi" | "espn" | "cache",
  "games": [
    {
      "game_pk": int,
      "date": "YYYY-MM-DD",
      "start_time_utc": "...Z",
      "status": "Preview" | "Live" | "Final" | "Postponed",
      "detailed_status": "Scheduled" | "In Progress" | ...,
      "home": {"id": int, "abbrev": "WSH", "name": "...", "record": "..."},
      "away": {...},
      "is_nats_home": bool,
      "venue": {"name": "...", "lat": float, "lon": float},
      "probable_pitchers": {
        "home": {"id": int, "name": "...", "hand": "R", "era": "...", "record": "..."},
        "away": {...}
      },
      "broadcasts": [{"name": "MASN", "type": "TV", "language": "en", "is_national": bool}],
      "linescore": {... when Live or Final ...},
      "series": {"game_number": int, "games_in_series": int, "description": "..."}
    },
    ...
  ]
}
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from common import (
    CACHE_DIR,
    NATIONALS_TEAM_ID,
    http_get_json,
    main_runner,
    now_iso,
    preserve_last_good,
)

log = logging.getLogger("fetch_schedule")

MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
ESPN_SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"
)

# How far forward to look (we always want at least the next game visible).
LOOKAHEAD_DAYS = 14
LOOKBACK_DAYS = 2  # for "last result" cards


# ---------- MLB Stats API (primary) ----------


def _mlb_hydrate() -> str:
    return ",".join(
        [
            "probablePitcher(note,stats)",
            "linescore",
            "team",
            "broadcasts(all)",
            "venue(location)",
            "seriesStatus",
            "game(content(summary))",
        ]
    )


def fetch_mlb_schedule() -> dict[str, Any] | None:
    today = date.today()
    start = today - timedelta(days=LOOKBACK_DAYS)
    end = today + timedelta(days=LOOKAHEAD_DAYS)
    params = {
        "sportId": 1,
        "teamId": NATIONALS_TEAM_ID,
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "hydrate": _mlb_hydrate(),
    }
    return http_get_json(
        MLB_SCHEDULE_URL,
        params=params,
        label="mlb schedule",
    )


def _parse_player(p: dict[str, Any] | None) -> dict[str, Any] | None:
    if not p:
        return None
    out = {
        "id": p.get("id"),
        "name": p.get("fullName"),
        "hand": (p.get("pitchHand") or {}).get("code"),
    }
    # ProbablePitcher hydration sometimes includes stats.
    stats = p.get("stats") or []
    for s in stats:
        group = (s.get("group") or {}).get("displayName")
        split = s.get("stats") or {}
        if group == "pitching":
            era = split.get("era")
            w = split.get("wins")
            l = split.get("losses")
            if era is not None:
                out["era"] = str(era)
            if w is not None and l is not None:
                out["record"] = f"{w}-{l}"
            break
    return out


def _parse_team(team_block: dict[str, Any]) -> dict[str, Any]:
    team = team_block.get("team") or {}
    rec = team_block.get("leagueRecord") or {}
    return {
        "id": team.get("id"),
        "abbrev": team.get("abbreviation") or team.get("teamCode", "").upper(),
        "name": team.get("name"),
        "short_name": team.get("teamName") or team.get("shortName") or team.get("name"),
        "record": f"{rec.get('wins', '?')}-{rec.get('losses', '?')}"
        if "wins" in rec
        else None,
        "score": team_block.get("score"),
    }


def _parse_venue(venue: dict[str, Any] | None) -> dict[str, Any]:
    if not venue:
        return {}
    loc = venue.get("location") or {}
    coords = loc.get("defaultCoordinates") or {}
    return {
        "id": venue.get("id"),
        "name": venue.get("name"),
        "city": loc.get("city"),
        "state": loc.get("stateAbbrev") or loc.get("state"),
        "lat": coords.get("latitude"),
        "lon": coords.get("longitude"),
    }


def _parse_broadcasts(bcasts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not bcasts:
        return []
    out = []
    for b in bcasts:
        # Only English TV/streaming, drop radio unless we want it explicitly elsewhere.
        btype = b.get("type")
        out.append(
            {
                "name": b.get("name") or b.get("callSign"),
                "call_sign": b.get("callSign"),
                "type": btype,  # TV, FM, AM, etc.
                "language": b.get("language") or "en",
                "is_national": bool(b.get("isNational")),
                "home_away": b.get("homeAway"),
                "media_state": (b.get("mediaState") or {}).get("mediaStateCode"),
            }
        )
    return out


def _parse_linescore(ls: dict[str, Any] | None) -> dict[str, Any] | None:
    if not ls:
        return None
    return {
        "current_inning": ls.get("currentInning"),
        "inning_half": ls.get("inningHalf"),
        "inning_state": ls.get("inningState"),
        "is_top_inning": ls.get("isTopInning"),
        "balls": ls.get("balls"),
        "strikes": ls.get("strikes"),
        "outs": ls.get("outs"),
        "home_runs": ((ls.get("teams") or {}).get("home") or {}).get("runs"),
        "away_runs": ((ls.get("teams") or {}).get("away") or {}).get("runs"),
        "home_hits": ((ls.get("teams") or {}).get("home") or {}).get("hits"),
        "away_hits": ((ls.get("teams") or {}).get("away") or {}).get("hits"),
        "home_errors": ((ls.get("teams") or {}).get("home") or {}).get("errors"),
        "away_errors": ((ls.get("teams") or {}).get("away") or {}).get("errors"),
    }


def _normalize_mlb_game(g: dict[str, Any]) -> dict[str, Any] | None:
    teams = g.get("teams") or {}
    home_block = teams.get("home") or {}
    away_block = teams.get("away") or {}
    home = _parse_team(home_block)
    away = _parse_team(away_block)

    is_nats_home = home["id"] == NATIONALS_TEAM_ID
    is_nats_away = away["id"] == NATIONALS_TEAM_ID
    if not (is_nats_home or is_nats_away):
        return None  # belt-and-suspenders; we asked for teamId=120 only

    status = g.get("status") or {}
    abstract = status.get("abstractGameState")  # Preview | Live | Final
    detailed = status.get("detailedState")  # In Progress, Scheduled, Postponed, etc.

    return {
        "game_pk": g.get("gamePk"),
        "date": g.get("officialDate") or g.get("gameDate", "")[:10],
        "start_time_utc": g.get("gameDate"),
        "status": abstract,
        "detailed_status": detailed,
        "is_nats_home": is_nats_home,
        "home": home,
        "away": away,
        "venue": _parse_venue(g.get("venue")),
        "probable_pitchers": {
            "home": _parse_player(home_block.get("probablePitcher")),
            "away": _parse_player(away_block.get("probablePitcher")),
        },
        "broadcasts": _parse_broadcasts(g.get("broadcasts")),
        "linescore": _parse_linescore(g.get("linescore")),
        "series": {
            "game_number": g.get("seriesGameNumber"),
            "games_in_series": g.get("gamesInSeries"),
            "description": g.get("seriesDescription"),
        },
        "double_header": g.get("doubleHeader"),
        "_source": "mlb_statsapi",
    }


def normalize_mlb(raw: dict[str, Any]) -> dict[str, Any]:
    games: list[dict[str, Any]] = []
    for date_block in raw.get("dates", []) or []:
        for g in date_block.get("games", []) or []:
            ng = _normalize_mlb_game(g)
            if ng:
                games.append(ng)
    games.sort(key=lambda g: g.get("start_time_utc") or "")
    return {
        "fetched_at": now_iso(),
        "source": "mlb_statsapi",
        "games": games,
    }


# ---------- ESPN fallback ----------


def fetch_espn_scoreboard() -> dict[str, Any] | None:
    """ESPN scoreboard - covers today only, but useful as live-state fallback."""
    return http_get_json(
        ESPN_SCOREBOARD_URL,
        label="espn scoreboard",
    )


def _normalize_espn_event(ev: dict[str, Any]) -> dict[str, Any] | None:
    competitions = ev.get("competitions") or []
    if not competitions:
        return None
    c = competitions[0]
    competitors = c.get("competitors") or []
    if len(competitors) < 2:
        return None
    home_block = next((x for x in competitors if x.get("homeAway") == "home"), None)
    away_block = next((x for x in competitors if x.get("homeAway") == "away"), None)
    if not home_block or not away_block:
        return None

    def _team(b: dict[str, Any]) -> dict[str, Any]:
        t = b.get("team") or {}
        return {
            "id": None,  # ESPN team id != MLB team id; leave null
            "abbrev": t.get("abbreviation"),
            "name": t.get("displayName"),
            "short_name": t.get("name"),
            "record": (b.get("records") or [{}])[0].get("summary"),
            "score": b.get("score"),
        }

    home = _team(home_block)
    away = _team(away_block)
    if "WSH" not in (home.get("abbrev"), away.get("abbrev")):
        return None
    is_nats_home = home.get("abbrev") == "WSH"

    status = (ev.get("status") or {}).get("type") or {}
    abstract_map = {"STATUS_SCHEDULED": "Preview", "STATUS_IN_PROGRESS": "Live", "STATUS_FINAL": "Final"}
    abstract = abstract_map.get(status.get("name", ""), status.get("state", "").title())

    venue = c.get("venue") or {}
    addr = venue.get("address") or {}

    return {
        "game_pk": int(ev.get("id")) if str(ev.get("id", "")).isdigit() else None,
        "date": (ev.get("date") or "")[:10],
        "start_time_utc": ev.get("date"),
        "status": abstract,
        "detailed_status": status.get("description"),
        "is_nats_home": is_nats_home,
        "home": home,
        "away": away,
        "venue": {
            "name": venue.get("fullName"),
            "city": addr.get("city"),
            "state": addr.get("state"),
            "lat": None,
            "lon": None,
        },
        "probable_pitchers": {"home": None, "away": None},
        "broadcasts": [
            {"name": b.get("media", {}).get("shortName"), "type": "TV", "language": "en"}
            for b in (c.get("broadcasts") or [])
        ],
        "linescore": None,
        "series": {},
        "_source": "espn",
    }


def normalize_espn(raw: dict[str, Any]) -> dict[str, Any]:
    events = raw.get("events") or []
    games = [g for g in (_normalize_espn_event(e) for e in events) if g]
    return {
        "fetched_at": now_iso(),
        "source": "espn",
        "games": games,
    }


# ---------- main ----------


def is_valid_schedule(payload: dict[str, Any]) -> bool:
    # Acceptable if it has the structure, even if 0 games (off-day).
    return "games" in payload and isinstance(payload.get("games"), list)


def main() -> int:
    log.info("Fetching Nationals schedule (primary: MLB statsapi)")
    fresh: dict[str, Any] | None = None

    raw_mlb = fetch_mlb_schedule()
    if raw_mlb is not None:
        try:
            fresh = normalize_mlb(raw_mlb)
            log.info("MLB statsapi: %d games", len(fresh["games"]))
        except Exception as e:  # noqa: BLE001
            log.exception("Failed to normalize MLB response: %s", e)
            fresh = None

    if fresh is None or not fresh.get("games"):
        # Fallback to ESPN. Note: ESPN only gives today, so this is partial coverage.
        log.warning("MLB primary failed or empty; trying ESPN fallback")
        raw_espn = fetch_espn_scoreboard()
        if raw_espn is not None:
            try:
                espn_norm = normalize_espn(raw_espn)
                log.info("ESPN: %d games", len(espn_norm["games"]))
                # Only use ESPN if MLB gave us nothing at all.
                if fresh is None:
                    fresh = espn_norm
            except Exception as e:  # noqa: BLE001
                log.exception("Failed to normalize ESPN response: %s", e)

    final = preserve_last_good(
        fresh,
        CACHE_DIR / "schedule.json",
        fresh_is_valid=is_valid_schedule,
    )
    log.info("Schedule final: source=%s, games=%d, stale=%s",
             final.get("source"), len(final.get("games", [])), final.get("_stale", False))
    return 0


if __name__ == "__main__":
    main_runner(main)
