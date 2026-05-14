"""Fetch standings for all 6 MLB divisions + Nationals streak/last-10.

Output: cache/standings.json
{
  "fetched_at": "...",
  "source": "mlb_statsapi",
  "divisions": [
    {"id": 200, "name": "AL West", "league": "AL", "teams": [...]},
    {"id": 201, "name": "AL East", "league": "AL", "teams": [...]},
    {"id": 202, "name": "AL Central", "league": "AL", "teams": [...]},
    {"id": 203, "name": "NL West", "league": "NL", "teams": [...]},
    {"id": 204, "name": "NL East", "league": "NL", "teams": [...]},
    {"id": 205, "name": "NL Central", "league": "NL", "teams": [...]}
  ],
  "division": { ... NL East hoisted for backwards compatibility ... },
  "nats": { ... Nats team entry hoisted ... }
}
"""

from __future__ import annotations

import logging
from typing import Any

from common import (
    CACHE_DIR,
    NATIONALS_TEAM_ID,
    http_get_json,
    main_runner,
    now_iso,
    preserve_last_good,
)

log = logging.getLogger("fetch_standings")

MLB_STANDINGS_URL = "https://statsapi.mlb.com/api/v1/standings"
NL_EAST_DIVISION_ID = 204

# Static map of division id → display name and league. The MLB API's `name`
# field is sometimes empty after hydrate, so we keep our own labels.
DIVISIONS = {
    200: ("AL West", "AL"),
    201: ("AL East", "AL"),
    202: ("AL Central", "AL"),
    203: ("NL West", "NL"),
    204: ("NL East", "NL"),
    205: ("NL Central", "NL"),
}


def fetch_mlb_standings() -> list[dict[str, Any]] | None:
    """Fetch both AL (103) and NL (104) standings and return all records."""
    out: list[dict[str, Any]] = []
    for league_id in (103, 104):
        data = http_get_json(
            MLB_STANDINGS_URL,
            params={
                "leagueId": str(league_id),
                "standingsTypes": "regularSeason",
                "hydrate": "team",
            },
            label=f"mlb standings (league {league_id})",
        )
        if not data:
            continue
        out.extend(data.get("records") or [])
    return out or None


def _team_entry(tr: dict[str, Any]) -> dict[str, Any]:
    team = tr.get("team") or {}
    streak = tr.get("streak") or {}
    split_records = tr.get("records", {}).get("splitRecords") or []
    last_ten = next(
        (f"{s.get('wins')}-{s.get('losses')}" for s in split_records if s.get("type") == "lastTen"),
        None,
    )
    return {
        "id": team.get("id"),
        "abbrev": team.get("abbreviation"),
        "name": team.get("name"),
        "short_name": team.get("teamName"),
        "wins": tr.get("wins"),
        "losses": tr.get("losses"),
        "pct": tr.get("winningPercentage"),
        "gb": tr.get("gamesBack"),
        "wc_gb": tr.get("wildCardGamesBack"),
        "streak": streak.get("streakCode"),
        "last_ten": last_ten,
        "division_rank": int(tr.get("divisionRank")) if tr.get("divisionRank") else None,
        "league_rank": int(tr.get("leagueRank")) if tr.get("leagueRank") else None,
        "wc_rank": int(tr.get("wildCardRank")) if tr.get("wildCardRank") else None,
        "run_diff": tr.get("runDifferential"),
        "runs_scored": tr.get("runsScored"),
        "runs_allowed": tr.get("runsAllowed"),
        "elim_num": tr.get("eliminationNumber"),
    }


def normalize(records: list[dict[str, Any]]) -> dict[str, Any]:
    divisions: list[dict[str, Any]] = []
    nl_east = None
    nats_entry = None
    for record in records:
        div = record.get("division") or {}
        div_id = div.get("id")
        if div_id not in DIVISIONS:
            continue
        name, league = DIVISIONS[div_id]
        teams = [_team_entry(tr) for tr in (record.get("teamRecords") or [])]
        teams.sort(key=lambda t: t.get("division_rank") or 99)
        d = {"id": div_id, "name": name, "league": league, "teams": teams}
        divisions.append(d)
        if div_id == NL_EAST_DIVISION_ID:
            nl_east = d
            for t in teams:
                if t.get("id") == NATIONALS_TEAM_ID:
                    nats_entry = t

    # Order divisions for display: NL East first (Nats!), then the rest of NL,
    # then AL East, AL Central, AL West.
    order = [204, 203, 205, 201, 202, 200]
    divisions.sort(key=lambda d: order.index(d["id"]) if d["id"] in order else 99)

    return {
        "fetched_at": now_iso(),
        "source": "mlb_statsapi",
        "divisions": divisions,
        # Backwards-compat hoist: app v0.1.0 reads `division` (singular) for
        # the NL East. Keep populating it so an old client doesn't lose data.
        "division": nl_east,
        "nats": nats_entry,
    }


def is_valid(payload: dict[str, Any]) -> bool:
    return payload.get("nats") is not None and len(payload.get("divisions") or []) >= 4


def main() -> int:
    log.info("Fetching standings (all 6 divisions)")
    fresh: dict[str, Any] | None = None
    records = fetch_mlb_standings()
    if records is not None:
        try:
            fresh = normalize(records)
            if fresh.get("nats"):
                log.info(
                    "Nats: %s, streak %s, L10 %s, %s GB | %d divisions normalized",
                    f"{fresh['nats']['wins']}-{fresh['nats']['losses']}",
                    fresh["nats"].get("streak"),
                    fresh["nats"].get("last_ten"),
                    fresh["nats"].get("gb"),
                    len(fresh["divisions"]),
                )
        except Exception as e:  # noqa: BLE001
            log.exception("Normalize failed: %s", e)

    final = preserve_last_good(
        fresh,
        CACHE_DIR / "standings.json",
        fresh_is_valid=is_valid,
    )
    log.info("Standings final: source=%s, stale=%s", final.get("source"), final.get("_stale", False))
    return 0


if __name__ == "__main__":
    main_runner(main)
