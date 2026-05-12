"""Fetch current status of tracked Former Nationals.

For each player in content/former_nats_roster.json:
  1. Resolve to a canonical mlb_id (lookup by name if id is wrong/missing).
  2. Pull current team + season stats from MLB Stats API.
  3. Pull last game line if they appeared in their team's most recent game.
  4. Apply significance thresholds to decide whether to include them.
  5. Detect "did something cool" events for the home-screen flag system,
     respecting the per-player tone and exclude_from_flags settings.

Output: cache/former_nats.json
{
  "fetched_at": "...",
  "players": [
    {
      "mlb_id": 547180,
      "name": "Bryce Harper",
      "nats_years": "2012–2018",
      "tone": "neutral",
      "exclude_from_flags": true,
      "type": "position",
      "current_team": {"id": 143, "abbrev": "PHI", "name": "Philadelphia Phillies"},
      "season_stats": {... AVG/HR/RBI for hitters, W/L/ERA/IP for pitchers ...},
      "last_game": {"date": "...", "opponent": "WSH", "line": "2-4, HR, 3 RBI"},
      "significant_event": {                  // null if nothing notable
        "kind": "multi_hr_game",
        "summary": "Two home runs",
        "celebrate": false                    // false for tone=neutral or against_nats etc
      }
    }
  ],
  "flag_candidates": [...]                    // subset eligible for home-screen flags
}
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from common import (
    CACHE_DIR,
    CONTENT_DIR,
    NATIONALS_TEAM_ID,
    http_get_json,
    main_runner,
    now_iso,
    preserve_last_good,
    read_json,
    team_abbrev,
)

log = logging.getLogger("fetch_former_nats")

PEOPLE_SEARCH_URL = "https://statsapi.mlb.com/api/v1/people/search"
PEOPLE_URL = "https://statsapi.mlb.com/api/v1/people/{id}"
PEOPLE_STATS_URL = "https://statsapi.mlb.com/api/v1/people/{id}/stats"
PEOPLE_GAME_LOG_URL = "https://statsapi.mlb.com/api/v1/people/{id}/stats"

ROSTER_PATH = CONTENT_DIR / "former_nats_roster.json"


# ---------- player lookup ----------


def search_player_by_name(name: str) -> int | None:
    data = http_get_json(
        PEOPLE_SEARCH_URL,
        params={"names": name, "sportIds": 1},
        label=f"player search ({name})",
    )
    if not data:
        return None
    people = data.get("people") or []
    if not people:
        return None
    # Prefer active players
    active = [p for p in people if p.get("active")]
    pick = (active or people)[0]
    return pick.get("id")


def get_player_summary(mlb_id: int) -> dict[str, Any] | None:
    data = http_get_json(
        PEOPLE_URL.format(id=mlb_id),
        params={"hydrate": "currentTeam,stats(group=[hitting,pitching],type=season)"},
        label=f"player {mlb_id}",
    )
    if not data:
        return None
    people = data.get("people") or []
    return people[0] if people else None


def get_recent_game_log(mlb_id: int, *, group: str) -> list[dict[str, Any]]:
    """Get last ~5 game log entries for the player (this season)."""
    season = date.today().year
    data = http_get_json(
        PEOPLE_GAME_LOG_URL.format(id=mlb_id),
        params={"stats": "gameLog", "group": group, "season": season},
        label=f"game log {mlb_id}",
    )
    if not data:
        return []
    stats = data.get("stats") or []
    if not stats:
        return []
    splits = stats[0].get("splits") or []
    # Most recent first
    splits.sort(key=lambda s: s.get("date") or "", reverse=True)
    return splits[:5]


# ---------- normalization ----------


def _extract_season_stats(person: dict[str, Any], group: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for s in person.get("stats") or []:
        gname = (s.get("group") or {}).get("displayName")
        ttype = (s.get("type") or {}).get("displayName")
        if gname != group or ttype != "season":
            continue
        splits = s.get("splits") or []
        if not splits:
            continue
        stat = splits[0].get("stat") or {}
        if group == "hitting":
            out = {
                "ab": stat.get("atBats"),
                "avg": stat.get("avg"),
                "hr": stat.get("homeRuns"),
                "rbi": stat.get("rbi"),
                "r": stat.get("runs"),
                "h": stat.get("hits"),
                "obp": stat.get("obp"),
                "slg": stat.get("slg"),
                "ops": stat.get("ops"),
                "sb": stat.get("stolenBases"),
            }
        elif group == "pitching":
            out = {
                "w": stat.get("wins"),
                "l": stat.get("losses"),
                "era": stat.get("era"),
                "ip": stat.get("inningsPitched"),
                "k": stat.get("strikeOuts"),
                "whip": stat.get("whip"),
                "saves": stat.get("saves"),
                "g": stat.get("gamesPlayed"),
                "gs": stat.get("gamesStarted"),
            }
        break
    return out


def _passes_significance(stats: dict[str, Any], ptype: str, thresholds: dict[str, Any]) -> bool:
    if ptype == "position":
        ab = stats.get("ab")
        try:
            return int(ab or 0) >= int(thresholds.get("position_player", {}).get("season_at_bats_min", 100))
        except (TypeError, ValueError):
            return False
    elif ptype == "pitcher":
        ip = stats.get("ip")
        try:
            return float(ip or 0) >= float(thresholds.get("pitcher", {}).get("season_innings_min", 20))
        except (TypeError, ValueError):
            return False
    return False


def _detect_significant_event(
    last_games: list[dict[str, Any]],
    ptype: str,
) -> dict[str, Any] | None:
    """Look only at the *most recent* game and decide if it was 'cool'.

    Categories (must match content/significant_events.json definitions):
      Position: 2+ HR, 4+ hits, walk-off, cycle, GW grand slam, 5+ RBI
      Pitcher:  CG, no-hitter, 10+ K start, win in clinch/elimination
      Either:   anything against the Nats auto-bumps to flag
    """
    if not last_games:
        return None
    g = last_games[0]
    stat = g.get("stat") or {}
    opp_id = ((g.get("opponent") or {}).get("id"))
    against_nats = opp_id == NATIONALS_TEAM_ID

    if ptype == "position":
        hr = int(stat.get("homeRuns") or 0)
        hits = int(stat.get("hits") or 0)
        rbi = int(stat.get("rbi") or 0)
        ab = int(stat.get("atBats") or 0)
        if hr >= 2:
            return {"kind": "multi_hr_game", "summary": f"{hr}-HR game", "against_nats": against_nats}
        if hits >= 4:
            return {"kind": "four_hit_game", "summary": f"{hits}-hit game", "against_nats": against_nats}
        if rbi >= 5:
            return {"kind": "big_rbi_game", "summary": f"{rbi} RBI", "against_nats": against_nats}
        # Cycle detection (1B + 2B + 3B + HR all >=1)
        singles = int(stat.get("hits") or 0) - int(stat.get("doubles") or 0) - int(stat.get("triples") or 0) - hr
        if singles >= 1 and int(stat.get("doubles") or 0) >= 1 and int(stat.get("triples") or 0) >= 1 and hr >= 1:
            return {"kind": "cycle", "summary": "Hit for the cycle", "against_nats": against_nats}
        if against_nats and ab >= 4 and hits >= 2:
            return {"kind": "against_nats", "summary": f"{hits}-for-{ab} vs. Nats", "against_nats": True}
    elif ptype == "pitcher":
        k = int(stat.get("strikeOuts") or 0)
        ip_str = str(stat.get("inningsPitched") or "0")
        try:
            ip_whole = float(ip_str)
        except ValueError:
            ip_whole = 0.0
        er = int(stat.get("earnedRuns") or 0)
        h_allowed = int(stat.get("hits") or 0)
        if ip_whole >= 9.0 and h_allowed == 0:
            return {"kind": "no_hitter", "summary": "No-hitter", "against_nats": against_nats}
        if ip_whole >= 9.0:
            return {"kind": "complete_game", "summary": "Complete game", "against_nats": against_nats}
        if k >= 10:
            return {"kind": "high_k_start", "summary": f"{k}-K start", "against_nats": against_nats}
        if against_nats and ip_whole >= 5.0 and er <= 1:
            return {"kind": "against_nats", "summary": f"shut down the Nats ({k} K)", "against_nats": True}
    return None


def _last_game_summary(last_games: list[dict[str, Any]], ptype: str) -> dict[str, Any] | None:
    if not last_games:
        return None
    g = last_games[0]
    stat = g.get("stat") or {}
    opp = (g.get("opponent") or {})
    opp_abbrev = team_abbrev(opp.get("id"))
    if ptype == "position":
        line = f"{stat.get('hits',0)}-{stat.get('atBats',0)}"
        extras = []
        if int(stat.get("homeRuns", 0) or 0) > 0:
            extras.append(f"{stat['homeRuns']} HR")
        if int(stat.get("rbi", 0) or 0) > 0:
            extras.append(f"{stat['rbi']} RBI")
        if extras:
            line = line + ", " + ", ".join(extras)
        return {"date": g.get("date"), "opponent": opp_abbrev, "line": line}
    elif ptype == "pitcher":
        ip = stat.get("inningsPitched")
        h = stat.get("hits")
        er = stat.get("earnedRuns")
        k = stat.get("strikeOuts")
        line = f"{ip} IP, {h} H, {er} ER, {k} K"
        return {"date": g.get("date"), "opponent": opp_abbrev, "line": line}
    return None


# ---------- main ----------


def main() -> int:
    roster_doc = read_json(ROSTER_PATH)
    if not roster_doc or not isinstance(roster_doc, dict):
        log.error("Roster file missing or invalid: %s", ROSTER_PATH)
        return 1
    thresholds = roster_doc.get("_significance_thresholds") or {}
    players = roster_doc.get("players") or []
    log.info("Tracking %d roster entries", len(players))

    out_players: list[dict[str, Any]] = []
    flag_candidates: list[dict[str, Any]] = []

    for entry in players:
        name = entry.get("name")
        ptype = entry.get("type", "position")
        mlb_id = entry.get("mlb_id")

        # Try given ID, fall back to name search
        person = get_player_summary(mlb_id) if mlb_id else None
        if person is None or person.get("fullName", "").lower() != (name or "").lower():
            log.info("Resolving %s by name (given id %s missed)", name, mlb_id)
            new_id = search_player_by_name(name)
            if new_id:
                person = get_player_summary(new_id)
                if person:
                    mlb_id = new_id
                    log.info("  Resolved %s -> id %d", name, new_id)
        if not person:
            log.warning("Could not resolve %s; skipping", name)
            continue

        team = person.get("currentTeam") or {}
        group = "hitting" if ptype == "position" else "pitching"
        stats = _extract_season_stats(person, group)
        if not _passes_significance(stats, ptype, thresholds):
            log.info("  %s below significance threshold; skipping", name)
            continue
        last_games = get_recent_game_log(mlb_id, group=group)
        last_game = _last_game_summary(last_games, ptype)
        event = _detect_significant_event(last_games, ptype)

        celebrate = (entry.get("tone") != "neutral") and not entry.get("exclude_from_flags", False)
        if event:
            event["celebrate"] = celebrate

        out = {
            "mlb_id": mlb_id,
            "name": name,
            "nats_years": entry.get("nats_years"),
            "tone": entry.get("tone", "enthusiastic"),
            "exclude_from_flags": bool(entry.get("exclude_from_flags", False)),
            "type": ptype,
            "current_team": {
                "id": team.get("id"),
                "abbrev": team_abbrev(team.get("id")),
                "name": team.get("name"),
            },
            "season_stats": stats,
            "last_game": last_game,
            "significant_event": event,
            "headshot_url": f"https://img.mlbstatic.com/mlb-photos/image/upload/d_people:generic:headshot:67:current.png/w_213,q_auto:best/v1/people/{mlb_id}/headshot/67/current",
        }
        out_players.append(out)

        if event and not entry.get("exclude_from_flags", False):
            flag_candidates.append(
                {
                    "mlb_id": mlb_id,
                    "name": name,
                    "summary": event["summary"],
                    "celebrate": celebrate,
                    "against_nats": event.get("against_nats", False),
                }
            )

    payload = {
        "fetched_at": now_iso(),
        "source": "mlb_statsapi",
        "players": out_players,
        "flag_candidates": flag_candidates,
    }

    def _valid(p: dict[str, Any]) -> bool:
        return "players" in p and isinstance(p["players"], list)

    final = preserve_last_good(payload, CACHE_DIR / "former_nats.json", fresh_is_valid=_valid)
    log.info(
        "Former Nats: %d tracked, %d flag candidates, stale=%s",
        len(final.get("players", [])),
        len(final.get("flag_candidates", [])),
        final.get("_stale", False),
    )
    return 0


if __name__ == "__main__":
    main_runner(main)
