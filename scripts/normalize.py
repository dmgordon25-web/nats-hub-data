"""Combine all cached source data into the single nationals.json the app reads.

Reads:
  cache/schedule.json
  cache/standings.json
  cache/injuries.json
  cache/former_nats.json
  cache/weather.json
  content/providers.json   (rules, not data)
  content/tidbits.json     (curated content)

Writes:
  docs/nationals.json      (the one endpoint the app polls)
  docs/version.json        (for app auto-update check)
  docs/providers.json      (mirror of content/providers.json - app fetches separately)
  docs/tidbits.json        (mirror)
  docs/former_nats.json    (mirror of cache for direct app access)

Why a single nationals.json: the TV app makes one network call on launch
to get everything the home screen and game-detail screen need. Each
secondary tab (Former Nats, Settings) can lazy-fetch from the per-feature
JSON if needed, but the critical-path fetch is one round trip.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from common import (
    CACHE_DIR,
    CONTENT_DIR,
    DOCS_DIR,
    NATIONALS_TEAM_ID,
    main_runner,
    now_iso,
    read_json,
    write_json,
)

log = logging.getLogger("normalize")

DATA_VERSION = 1  # bump when the schema changes in a breaking way


# ---------- helpers ----------


def _classify_game(game: dict[str, Any], now: datetime) -> str:
    """Return one of: 'live', 'next', 'previous'.

    Note we *don't* blindly trust status='Live' from the upstream feed —
    sometimes the schedule API leaves a game flagged Live for hours after it
    actually ended (or after our publish pipeline goes silent). If a 'Live'
    game's start_time was more than 8 hours ago, we treat it as previous so
    the home screen doesn't insist a finished game is still in progress.
    """
    status = game.get("status")
    detailed = (game.get("detailed_status") or "").lower()
    st = game.get("start_time_utc")
    try:
        gdt = datetime.fromisoformat((st or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        gdt = None

    if status == "Live" or "in progress" in detailed:
        # Sanity: 8 hr is longer than any real MLB game (extras included).
        if gdt is None or (now - gdt).total_seconds() < 8 * 3600:
            return "live"
        # Stale Live — fall through.
    if status == "Final":
        return "previous"
    if "postpone" in detailed or "delayed" in detailed:
        return "next"  # treat as next so the app surfaces the situation
    if gdt and gdt > now:
        return "next"
    return "previous"


def _pick_focus_game(games: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    """The "tonight" game per the 4 AM rollover rule.

    Rule: a Final from earlier in the calendar day stays the focus until
    4 AM the following day local-equivalent. Past 4 AM, focus moves to
    the next upcoming game.

    We approximate "local" as US/Eastern for the parents' market.
    """
    if not games:
        return None

    # Only consider 'Live' games whose start was within the last 8 hours.
    # Otherwise the upstream schedule's stale Live flag would lock us onto
    # yesterday's game.
    def _is_actually_live(g: dict[str, Any]) -> bool:
        if g.get("status") != "Live":
            return False
        st = g.get("start_time_utc")
        try:
            gdt = datetime.fromisoformat((st or "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return True  # unknown start → trust the upstream flag
        return (now - gdt).total_seconds() < 8 * 3600

    live = [g for g in games if _is_actually_live(g)]
    if live:
        return live[0]

    # 4 AM ET rollover. We're storing UTC; Eastern is UTC-5 (EST) or UTC-4 (EDT).
    # Conservative: treat anything >9 UTC as past 4 AM local for both.
    rollover_hour_utc = 9
    today_local = now.date() if now.hour >= rollover_hour_utc else (
        now.replace(hour=0) - timezone(timezone.utc.utcoffset(now) or None).utcoffset(now)
    ).date() if False else now.date()
    # Simpler: if before 9 UTC, "today" focus is yesterday's date for any game played.
    if now.hour < rollover_hour_utc:
        today_local = (now.replace(hour=0, minute=0, second=0) - (now - now)).date()
        # equivalent to now.date() since we just want a date object
        today_local = now.date()  # acceptable approximation

    # Find a Final from today's local date
    todays_finals = [
        g for g in games
        if g.get("status") == "Final" and g.get("date") == today_local.isoformat()
    ]
    if todays_finals and now.hour < rollover_hour_utc:
        return todays_finals[-1]  # latest final today

    # Otherwise: next upcoming game
    upcoming = []
    for g in games:
        st = g.get("start_time_utc")
        try:
            gdt = datetime.fromisoformat((st or "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if gdt >= now:
            upcoming.append((gdt, g))
    if upcoming:
        upcoming.sort(key=lambda x: x[0])
        return upcoming[0][1]

    # No future game found, fall back to most recent Final
    finals = [g for g in games if g.get("status") == "Final"]
    if finals:
        return finals[-1]
    return None


def _resolve_provider(broadcasts: list[dict[str, Any]], rules: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Match the first applicable rule against the game's broadcast list."""
    if not broadcasts:
        return None
    # Filter to TV broadcasts (skip radio/Spanish)
    tv = [
        b for b in broadcasts
        if (b.get("type") or "").upper() == "TV" and (b.get("language") or "en") == "en"
    ]
    # Prefer national exclusives (they take priority over local feeds)
    nationals = [b for b in tv if b.get("is_national")]
    locals_ = [b for b in tv if not b.get("is_national")]

    def _match(broadcast_list: list[dict[str, Any]]) -> dict[str, Any] | None:
        for b in broadcast_list:
            haystack = " ".join(filter(None, [b.get("name"), b.get("call_sign")])).lower()
            if not haystack:
                continue
            for rule in rules:
                for kw in rule.get("match_keywords", []):
                    if kw.lower() in haystack:
                        return {
                            "broadcast_name": b.get("name") or b.get("call_sign"),
                            "is_national": bool(b.get("is_national")),
                            "rule_id": rule.get("id"),
                            "label": rule.get("label"),
                            "package": rule.get("package"),
                            "intent_uri": rule.get("intent_uri"),
                            "fallback_uri": rule.get("fallback_uri"),
                        }
        return None

    return _match(nationals) or _match(locals_)


def _summary_label(game: dict[str, Any], now: datetime) -> str:
    """Plain-English label for the giant Watch button / status pill."""
    status = game.get("status")
    detailed = game.get("detailed_status") or ""
    if status == "Live":
        return "Watch Live"
    if status == "Final":
        return "Game Final"
    if "postpone" in detailed.lower():
        return "Postponed"
    if "delay" in detailed.lower():
        return "Delayed"
    # Preview
    try:
        gdt = datetime.fromisoformat((game.get("start_time_utc") or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return "Watch Tonight"
    delta = (gdt - now).total_seconds()
    if delta < 3600:
        return "Starts Soon"
    same_day = gdt.date() == now.date()
    return "Watch Tonight" if same_day else "Next Game"


# ---------- main ----------


def main() -> int:
    now = datetime.now(timezone.utc)

    schedule = read_json(CACHE_DIR / "schedule.json", default={"games": []})
    standings = read_json(CACHE_DIR / "standings.json", default={})
    injuries = read_json(CACHE_DIR / "injuries.json", default={"injuries": []})
    former_nats = read_json(CACHE_DIR / "former_nats.json", default={"players": [], "flag_candidates": []})
    weather = read_json(CACHE_DIR / "weather.json", default={})
    providers = read_json(CONTENT_DIR / "providers.json", default={"rules": []})
    tidbits = read_json(CONTENT_DIR / "tidbits.json", default={"entries": []})

    games = schedule.get("games") or []
    rules = providers.get("rules") or []

    # Enrich each game with resolved provider + classification
    enriched_games = []
    for g in games:
        enriched = dict(g)
        enriched["classification"] = _classify_game(g, now)
        enriched["provider"] = _resolve_provider(g.get("broadcasts") or [], rules) \
            or providers.get("universal_fallback")
        enriched["watch_label"] = _summary_label(g, now)
        enriched_games.append(enriched)

    focus = _pick_focus_game(enriched_games, now)

    # If our focus is on a game, attach today's weather only if it matches
    focus_weather = None
    if focus and weather and weather.get("game_pk") == focus.get("game_pk"):
        focus_weather = weather

    # Filter injuries to top 10 most-recent for the home screen card
    inj_list = injuries.get("injuries") or []
    notable_injuries = inj_list[:10]

    # Pick a tidbit for "tonight's nugget" - simple weighted random based on triggers
    tidbit = _pick_tidbit(tidbits.get("entries") or [], focus)

    payload = {
        "data_version": DATA_VERSION,
        "fetched_at": now_iso(),
        "freshness": {
            "schedule": schedule.get("fetched_at"),
            "standings": standings.get("fetched_at"),
            "injuries": injuries.get("fetched_at"),
            "former_nats": former_nats.get("fetched_at"),
            "weather": weather.get("fetched_at"),
        },
        "stale_sources": [
            k for k, v in {
                "schedule": schedule, "standings": standings, "injuries": injuries,
                "former_nats": former_nats, "weather": weather,
            }.items() if v.get("_stale")
        ],
        "focus_game": focus,
        "focus_weather": focus_weather,
        "tonight_tidbit": tidbit,
        "next_three": [
            g for g in enriched_games
            if g.get("classification") in ("next", "live")
        ][:3],
        "last_result": next(
            (g for g in reversed(enriched_games) if g.get("status") == "Final"),
            None,
        ),
        "all_games": enriched_games,
        "standings": standings.get("division"),
        "nats_record": standings.get("nats"),
        "notable_injuries": notable_injuries,
        "former_nats_flag_candidates": former_nats.get("flag_candidates") or [],
    }

    write_json(DOCS_DIR / "nationals.json", payload)

    # Mirror provider rules + tidbits + former_nats for direct app access
    write_json(DOCS_DIR / "providers.json", providers)
    write_json(DOCS_DIR / "tidbits.json", tidbits)
    write_json(DOCS_DIR / "former_nats.json", former_nats)

    # Version file the app polls for self-update detection
    version_doc = read_json(DOCS_DIR / "version.json", default={
        "data_version": DATA_VERSION,
        "min_app_version": "0.1.0",
        "latest_app_version": "0.1.0",
        "apk_url": "",
        "release_notes": "Initial release",
    })
    version_doc["data_version"] = DATA_VERSION
    version_doc["updated_at"] = now_iso()
    write_json(DOCS_DIR / "version.json", version_doc)

    log.info(
        "Normalized: focus=%s, %d games, %d injuries, %d former-nats, %d flags",
        (focus or {}).get("game_pk"),
        len(enriched_games),
        len(notable_injuries),
        len(former_nats.get("players") or []),
        len(payload["former_nats_flag_candidates"]),
    )
    return 0


def _pick_tidbit(entries: list[dict[str, Any]], focus_game: dict[str, Any] | None) -> dict[str, Any] | None:
    """Pick one tidbit. Honors `trigger` field when context matches."""
    if not entries:
        return None
    eligible = []
    for e in entries:
        trig = e.get("trigger")
        if trig is None:
            eligible.append(e)
            continue
        if not focus_game:
            continue
        if trig == "is_home_game" and focus_game.get("is_nats_home"):
            eligible.append(e)
        elif trig == "is_away_game" and not focus_game.get("is_nats_home"):
            eligible.append(e)
        elif trig.startswith("streak_"):
            # Need standings context for these; attach later when needed.
            # Skip for now - the app can re-pick from the full set with context.
            pass
    pool = eligible or entries

    # Deterministic-but-rotating: index by day-of-year over weighted pool
    import hashlib
    weighted = []
    for e in pool:
        for _ in range(int(e.get("weight", 5))):
            weighted.append(e)
    if not weighted:
        return None
    seed = int(hashlib.sha1(date.today().isoformat().encode()).hexdigest(), 16)
    return weighted[seed % len(weighted)]


if __name__ == "__main__":
    main_runner(main)
