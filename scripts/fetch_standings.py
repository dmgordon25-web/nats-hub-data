"""Fetch NL East standings + Nationals streak/last-10 from MLB Stats API.

Output: cache/standings.json
{
  "fetched_at": "...",
  "source": "mlb_statsapi",
  "division": {
    "id": 204, "name": "NL East",
    "teams": [
      {"id": 120, "abbrev": "WSH", "name": "...", "wins": ..., "losses": ...,
       "pct": "0.500", "gb": "-", "streak": "W2", "last_ten": "6-4",
       "division_rank": 3, "wc_rank": ..., "run_diff": ...}
    ]
  },
  "nats": { ... same as the WSH entry above, hoisted for convenience ... }
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


def fetch_mlb_standings() -> dict[str, Any] | None:
    return http_get_json(
        MLB_STANDINGS_URL,
        params={
            "leagueId": "104",  # NL
            "standingsTypes": "regularSeason",
            "hydrate": "team",
        },
        label="mlb standings",
    )


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    division = None
    nats_entry = None
    for record in raw.get("records") or []:
        div = record.get("division") or {}
        if div.get("id") != NL_EAST_DIVISION_ID:
            continue
        teams: list[dict[str, Any]] = []
        for tr in record.get("teamRecords") or []:
            team = tr.get("team") or {}
            streak = tr.get("streak") or {}
            split_records = tr.get("records", {}).get("splitRecords") or []
            last_ten = next(
                (f"{s.get('wins')}-{s.get('losses')}" for s in split_records if s.get("type") == "lastTen"),
                None,
            )
            entry = {
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
            teams.append(entry)
            if entry["id"] == NATIONALS_TEAM_ID:
                nats_entry = entry
        teams.sort(key=lambda t: t.get("division_rank") or 99)
        # MLB hydrate doesn't always return division.name; fall back to known map.
        div_name = div.get("name") or {204: "NL East", 203: "NL Central", 205: "NL West"}.get(div.get("id"))
        division = {"id": div.get("id"), "name": div_name, "teams": teams}
        break

    return {
        "fetched_at": now_iso(),
        "source": "mlb_statsapi",
        "division": division,
        "nats": nats_entry,
    }


def is_valid(payload: dict[str, Any]) -> bool:
    return payload.get("nats") is not None


def main() -> int:
    log.info("Fetching standings")
    fresh: dict[str, Any] | None = None
    raw = fetch_mlb_standings()
    if raw is not None:
        try:
            fresh = normalize(raw)
            if fresh.get("nats"):
                log.info(
                    "Nats: %s, streak %s, L10 %s, %s GB",
                    f"{fresh['nats']['wins']}-{fresh['nats']['losses']}",
                    fresh["nats"].get("streak"),
                    fresh["nats"].get("last_ten"),
                    fresh["nats"].get("gb"),
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
