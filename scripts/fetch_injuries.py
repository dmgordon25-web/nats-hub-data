"""Fetch Washington Nationals injuries.

Primary: ESPN team injuries page (https://www.espn.com/mlb/team/injuries/_/name/wsh).
ESPN embeds the data as JSON inside the HTML under window['__espnfitt__']. Stable
enough that it's been the same shape for years. We extract that block, preserve
last-known-good on failure, and respect a manual override file.

Output: cache/injuries.json
{
  "fetched_at": "...",
  "source": "espn" | "manual_override" | "cache",
  "season": "2026",
  "injuries": [
    {
      "athlete_id": int,
      "athlete_name": "Clayton Beeter",
      "short_name": "C. Beeter",
      "headshot_url": "...",
      "position": "RP",
      "status_code": "INJURY_STATUS_15DAYIL",
      "status_label": "15-day IL",
      "status_abbrev": "IL15",
      "date": "May 12",
      "description": "..."
    },
    ...
  ]
}
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from common import (
    CACHE_DIR,
    CONTENT_DIR,
    http_get_text,
    main_runner,
    now_iso,
    preserve_last_good,
    read_json,
)

log = logging.getLogger("fetch_injuries")

ESPN_INJURIES_URL = "https://www.espn.com/mlb/team/injuries/_/name/wsh"
ESPN_DATA_RE = re.compile(
    r"window\[.__espnfitt__.\]\s*=\s*({.*?});</script>", re.DOTALL
)

OVERRIDE_PATH = CONTENT_DIR / "injury_overrides.json"


def fetch_espn() -> dict[str, Any] | None:
    # Empirical: ESPN's anti-bot serves a stub (~2KB) for full Chrome UA strings
    # but lets a plain "Mozilla/5.0" through to the real (~300KB) page that
    # contains the embedded data block we need. Keep it simple.
    html = http_get_text(
        ESPN_INJURIES_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        label="espn injuries",
    )
    if not html:
        return None
    m = ESPN_DATA_RE.search(html)
    if not m:
        log.error("ESPN injuries: embedded JSON pattern not found")
        return None
    try:
        return json.loads(m.group(1))
    except Exception as e:  # noqa: BLE001
        log.exception("ESPN injuries: failed to parse JSON: %s", e)
        return None


def _athlete_id_from_href(href: str | None) -> int | None:
    if not href:
        return None
    m = re.search(r"/id/(\d+)", href)
    return int(m.group(1)) if m else None


def normalize_espn(payload: dict[str, Any]) -> dict[str, Any]:
    inj_block = payload.get("page", {}).get("content", {}).get("injuries", {})
    season = inj_block.get("season")
    grouped = inj_block.get("injuries") or []
    out: list[dict[str, Any]] = []
    for grp in grouped:
        date_label = grp.get("date")
        for item in grp.get("items") or []:
            athlete = item.get("athlete") or {}
            t = item.get("type") or {}
            out.append(
                {
                    "athlete_id": _athlete_id_from_href(athlete.get("href")),
                    "athlete_name": athlete.get("name"),
                    "short_name": athlete.get("shortName"),
                    "headshot_url": (athlete.get("logo") or {}).get("href"),
                    "position": athlete.get("position"),
                    "status_code": t.get("name"),
                    "status_label": t.get("description"),
                    "status_abbrev": t.get("abbreviation"),
                    "date": date_label,
                    "description": item.get("description"),
                }
            )
    return {
        "fetched_at": now_iso(),
        "source": "espn",
        "season": season,
        "injuries": out,
    }


def is_valid(payload: dict[str, Any]) -> bool:
    # Acceptable even if 0 injuries (we want zero to be reportable).
    return "injuries" in payload and isinstance(payload.get("injuries"), list)


def main() -> int:
    log.info("Fetching Nationals injuries (primary: ESPN)")

    # Manual override always wins. Edit content/injury_overrides.json by hand
    # and push for an instant correction without waiting on scrape.
    override = read_json(OVERRIDE_PATH)
    if override is not None and isinstance(override, dict) and "injuries" in override:
        override = {**override, "fetched_at": now_iso(), "source": "manual_override"}
        log.info("Using manual override (%d injuries)", len(override.get("injuries", [])))
        from common import write_json

        write_json(CACHE_DIR / "injuries.json", override)
        return 0

    fresh: dict[str, Any] | None = None
    raw = fetch_espn()
    if raw is not None:
        try:
            fresh = normalize_espn(raw)
            log.info("ESPN parsed: %d injuries", len(fresh["injuries"]))
        except Exception as e:  # noqa: BLE001
            log.exception("ESPN normalize failed: %s", e)

    final = preserve_last_good(
        fresh,
        CACHE_DIR / "injuries.json",
        fresh_is_valid=is_valid,
    )
    log.info(
        "Injuries final: source=%s, count=%d, stale=%s",
        final.get("source"),
        len(final.get("injuries", [])),
        final.get("_stale", False),
    )
    return 0


if __name__ == "__main__":
    main_runner(main)
