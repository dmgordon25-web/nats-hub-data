"""Build happenings.json — the payload behind the TV app's "Nats Nation" page.

This is a NEW, fully separate artifact. It does NOT touch nationals.json,
normalize.py, or any existing fetcher. The TV app reads it over the same
GitHub Pages HTTPS origin it already uses, exactly like nationals.json.

Two honest generation modes
----------------------------
  rss_fallback   (default; runs anywhere incl. GitHub Actions, no Falkor)
                 Official public RSS only. No AI. why_it_matters / trending /
                 fan_vibes are omitted and disclosed in the provenance footer.

  falkor_enriched (run locally on Dustin's box, where Falkor loopback is
                 reachable) Same RSS articles, PLUS per-article AI enrichment
                 via POST /api/falkor/ingestors/enrich:
                   - why_it_matters  <- enrich.summary (labeled ai_generated)
                   - trending[]      <- aggregated enrich.tags, each cited
                 fan_vibes (sentiment) stays omitted+disclosed: Phase-0 recon
                 confirmed the sanctioned enrich endpoint emits NO sentiment
                 polarity, so we never fabricate one.

Honesty rules enforced here (see the audit's risk_and_no_touch.md):
  - News/videos come from real, cited official feeds only. No scraping, no
    social-media scraping, no invented posts/quotes/usernames/counts.
  - Every AI-touched field carries ai_generated:true. Curated fields carry
    curated:true. Missing data is disclosed in sources[]/limited_sources[].
  - A bad fetch never overwrites good content (per-section last-good).
  - User-facing copy never says "stale" — the app renders "as of <time>" etc.

Reads:
  content/happenings_sources.json   (curated allowlist)
  content/nats_history.json         (curated "On This Day", optional)
  docs/nationals.json               (existing payload — Team Pulse / What to Watch)
  cache/happenings.json             (previous good copy — per-section last-good)

Writes:
  cache/happenings.json   (last-known-good baseline, committed)
  docs/happenings.json    (the published artifact the app polls)

Usage:
  python scripts/build_happenings.py                      # rss_fallback
  python scripts/build_happenings.py --mode falkor_enriched
  FALKOR_BASE_URL=http://127.0.0.1:3001 python scripts/build_happenings.py --mode falkor_enriched
"""

from __future__ import annotations

import argparse
import html
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree as ET

import requests

from common import (
    CACHE_DIR,
    CONTENT_DIR,
    DOCS_DIR,
    http_get_text,
    now_iso,
    read_json,
    write_json,
)

log = logging.getLogger("build_happenings")

DATA_VERSION = 1

# Falkor loopback base (overridable). Only contacted in falkor_enriched mode.
FALKOR_BASE_URL = os.environ.get("FALKOR_BASE_URL", "http://127.0.0.1:3001").rstrip("/")
ENRICH_ENDPOINT = "/api/falkor/ingestors/enrich"
ENRICH_KILL_SWITCH_ENV = "FALKOR_INGESTOR_ENRICHMENT_ENABLED"  # documented; Falkor reads its own
ENRICH_TIMEOUT_S = 20
ENRICH_MAX_STORIES = 6  # bound the local model spend per run

MAX_TOP_STORIES = 8
MAX_VIDEOS = 6
MAX_TRENDING = 8

# Tags too generic to be a useful "trending topic" (they describe the whole feed).
# Compared on a normalized (lowercase, alphanumeric-only) key.
GENERIC_TAG_KEYS = {
    "baseball", "mlb", "sports", "news", "sportsnews", "nationals", "nats",
    "washington", "washingtonnationals", "mlbtraderumors", "game", "gamethread",
    "season", "highlights", "recap", "preview", "majorleaguebaseball",
}

OG_IMAGE_TIMEOUT_S = 6

# Atom / Media RSS namespaces (YouTube feeds).
_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def to_iso(value: str | None) -> str | None:
    """Parse an RSS (RFC822) or Atom (ISO8601) date into ISO8601 UTC, or None."""
    if not value:
        return None
    value = value.strip()
    # Try ISO8601 first (Atom).
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        pass
    # Fall back to RFC822 (RSS pubDate).
    try:
        dt = parsedate_to_datetime(value)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None


def _published_sort_key(item: dict[str, Any]) -> str:
    return item.get("published_at") or ""


# ---------------------------------------------------------------------------
# Feed parsing (stdlib only — no feedparser dependency)
# ---------------------------------------------------------------------------


def _strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def og_image(url: str | None) -> str | None:
    """Best-effort article thumbnail: fetch the page's og:image (its social
    preview image). Returns None on any failure — a missing image just means no
    picture for that story. This is standard link-preview behavior (one GET, read
    a meta tag), not scraping; identifies itself via User-Agent."""
    if not url or not url.startswith("http"):
        return None
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; nats-hub-data/0.1; link-preview)"},
            timeout=OG_IMAGE_TIMEOUT_S,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    page = resp.text
    for pat in (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
    ):
        m = re.search(pat, page, re.IGNORECASE)
        if m:
            img = html.unescape(m.group(1).strip())  # decode &amp; etc. in the URL
            if img.startswith("http"):
                return img
    return None


def parse_news_feed(xml_text: str, source_name: str) -> list[dict[str, Any]] | None:
    """Parse an RSS 2.0 or Atom news feed into normalized story dicts.

    Returns a list (possibly empty for a valid feed with no items) on success,
    or None when the body is not parseable feed XML (e.g. a 200-OK error page).
    Callers MUST treat None as a failed fetch — not a valid empty feed — so a
    bad body never blanks a section that has good last-good content."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.warning("%s: XML parse error: %s", source_name, e)
        return None
    # A 200-OK error page can be well-formed XML/XHTML that isn't a feed at all
    # (root <html>). Treat anything that isn't an RSS/Atom root as a failed fetch.
    if _localname(root.tag).lower() not in ("rss", "feed"):
        log.warning("%s: body is not a feed (root=%s)", source_name, _localname(root.tag))
        return None

    items: list[dict[str, Any]] = []

    # RSS 2.0: channel/item
    for item in root.iter():
        if _localname(item.tag) != "item":
            continue
        title = link = pub = summary = None
        for child in item:
            name = _localname(child.tag)
            if name == "title":
                title = (child.text or "").strip()
            elif name == "link":
                link = (child.text or "").strip()
            elif name == "pubDate":
                pub = child.text
            elif name in ("description", "summary", "encoded"):
                if summary is None:
                    summary = _strip_html(child.text)
        if title and link:
            items.append({
                "title": title,
                "url": link,
                "source": source_name,
                "published_at": to_iso(pub),
                "summary": (summary or "")[:600],
            })

    if items:
        return items

    # Atom: feed/entry
    for entry in root.iter():
        if _localname(entry.tag) != "entry":
            continue
        title = link = pub = summary = None
        for child in entry:
            name = _localname(child.tag)
            if name == "title":
                title = _strip_html(child.text)
            elif name == "link":
                # Atom links carry the URL in href; prefer rel="alternate".
                href = child.get("href")
                rel = child.get("rel", "alternate")
                if href and (link is None or rel == "alternate"):
                    link = href
            elif name in ("published", "updated"):
                if pub is None:
                    pub = child.text
            elif name in ("summary", "content"):
                if summary is None:
                    summary = _strip_html(child.text)
        if title and link:
            items.append({
                "title": title,
                "url": link,
                "source": source_name,
                "published_at": to_iso(pub),
                "summary": (summary or "")[:600],
            })
    return items


def parse_video_feed(xml_text: str, source_name: str) -> list[dict[str, Any]] | None:
    """Parse a YouTube channel Atom feed into normalized video dicts.

    Returns a list (possibly empty) on success, or None when the body is not
    parseable XML (treated as a failed fetch, not a valid empty feed)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.warning("%s: video XML parse error: %s", source_name, e)
        return None
    if _localname(root.tag).lower() != "feed":  # YouTube feeds are Atom <feed>
        log.warning("%s: video body is not an Atom feed (root=%s)", source_name, _localname(root.tag))
        return None

    videos: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", _NS):
        title_el = entry.find("atom:title", _NS)
        link_el = entry.find("atom:link", _NS)
        pub_el = entry.find("atom:published", _NS)
        group = entry.find("media:group", _NS)
        title = (title_el.text or "").strip() if title_el is not None else None
        url = link_el.get("href") if link_el is not None else None
        thumb = None
        summary = ""
        if group is not None:
            thumb_el = group.find("media:thumbnail", _NS)
            if thumb_el is not None:
                thumb = thumb_el.get("url")
            desc_el = group.find("media:description", _NS)
            if desc_el is not None:
                summary = _strip_html(desc_el.text)[:600]
        if title and url:
            videos.append({
                "title": title,
                "url": url,
                "source": source_name,
                "thumbnail_url": thumb,
                "duration_s": 0,  # not exposed by the YouTube RSS feed
                "published_at": to_iso(pub_el.text) if pub_el is not None else None,
                "summary": summary,
            })
    return videos


# ---------------------------------------------------------------------------
# Nationals relevance filter (for MLB-wide league feeds)
# ---------------------------------------------------------------------------


def is_nationals(text: str, keywords: list[str]) -> bool:
    """True if the text plausibly refers to the Washington Nationals.

    Conservative on purpose: 'Nationals' (plural) almost always means the team
    in MLB context, while 'National League' uses the singular and won't match.
    'Nats' is matched only as a whole word to avoid 'gnats'/'fanatics'.
    """
    if not text:
        return False
    low = text.lower()
    for kw in keywords:
        k = kw.lower()
        if k == "nats":
            if re.search(r"\bnats\b", low):
                return True
        elif k in low:
            return True
    return False


# ---------------------------------------------------------------------------
# Falkor enrichment (falkor_enriched mode only)
# ---------------------------------------------------------------------------


def falkor_enrich(title: str, summary: str | None, source_name: str | None) -> dict[str, Any] | None:
    """POST one article to the sanctioned Falkor enrich endpoint.

    Returns the 'enriched' result dict, or None for disabled/degraded/unreachable
    (caller treats None as "no AI for this item" — never fabricates).
    """
    url = f"{FALKOR_BASE_URL}{ENRICH_ENDPOINT}"
    body = {"title": title[:300], "summary": (summary or "")[:2000], "category": "Sports"}
    if source_name:
        body["source_name"] = source_name[:120]
    try:
        resp = requests.post(
            url,
            json=body,
            headers={"User-Agent": "nats-hub-data/0.1", "Accept": "application/json"},
            timeout=ENRICH_TIMEOUT_S,
        )
    except requests.RequestException as e:
        log.warning("enrich unreachable (%s); continuing without AI", e)
        return None
    if resp.status_code != 200:
        log.warning("enrich HTTP %s; treating as unavailable", resp.status_code)
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    result = data.get("result") or {}
    if result.get("state") != "enriched":
        log.info("enrich state=%s (no AI for this item)", result.get("state"))
        return None
    return result


# ---------------------------------------------------------------------------
# News gathering
# ---------------------------------------------------------------------------


def _norm_title(title: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())


def _dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicate items by normalized URL AND by normalized title, so the
    same story syndicated across two feeds appears once (keep the first seen —
    callers sort freshest-first before deduping)."""
    seen_url: set[str] = set()
    seen_title: set[str] = set()
    out: list[dict[str, Any]] = []
    for it in items:
        url_key = (it.get("url") or "").split("?")[0].lower()
        title_key = _norm_title(it.get("title"))
        if url_key and url_key in seen_url:
            continue
        if title_key and title_key in seen_title:
            continue
        if url_key:
            seen_url.add(url_key)
        if title_key:
            seen_title.add(title_key)
        out.append(it)
    return out


def gather_news(sources: dict[str, Any]) -> dict[str, Any]:
    """Fetch every news feed, returning items + per-source status.

    Returns: { stories, sources[], any_feed_live }
    """
    keywords = sources.get("team_keywords") or []
    feeds = sources.get("news_feeds") or {}
    out_items: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    any_live = False

    for kind in ("team", "league"):
        for feed in feeds.get(kind) or []:
            fid, name, url = feed.get("id"), feed.get("name"), feed.get("url")
            xml = http_get_text(url, label=f"feed {fid}")
            parsed = parse_news_feed(xml, name) if xml is not None else None
            if parsed is None:
                # No body, or a 200 that wasn't valid feed XML → a failed fetch,
                # NOT a valid empty feed. Don't mark live, so build_payload can
                # fall back to last-good instead of blanking the section.
                source_rows.append({"id": fid, "name": name, "last_updated_at": None, "status": "timed_out"})
                continue
            any_live = True
            # team feeds: keep all; league feeds: keep only Nationals-relevant.
            if kind == "league":
                parsed = [p for p in parsed if is_nationals(f"{p.get('title','')} {p.get('summary','')}", keywords)]
            source_rows.append({
                "id": fid,
                "name": name,
                "last_updated_at": now_iso(),
                "status": "live",
            })
            out_items.extend(parsed)

    stories = _dedupe(out_items)
    stories.sort(key=_published_sort_key, reverse=True)
    return {"stories": stories[:MAX_TOP_STORIES], "sources": source_rows, "any_feed_live": any_live}


def gather_videos(sources: dict[str, Any]) -> dict[str, Any]:
    keywords = sources.get("team_keywords") or []
    feeds = sources.get("video_feeds") or {}
    out: list[dict[str, Any]] = []
    source_rows: list[dict[str, Any]] = []
    any_live = False
    for kind in ("team", "league"):
        for feed in feeds.get(kind) or []:
            fid, name, url = feed.get("id"), feed.get("name"), feed.get("url")
            xml = http_get_text(url, label=f"video {fid}")
            parsed = parse_video_feed(xml, name) if xml is not None else None
            if parsed is None:
                # Failed fetch or unparseable body (not a valid empty feed).
                source_rows.append({"id": fid, "name": name, "last_updated_at": None, "status": "timed_out"})
                continue
            any_live = True
            if kind == "league":
                parsed = [p for p in parsed if is_nationals(f"{p.get('title','')} {p.get('summary','')}", keywords)]
            source_rows.append({"id": fid, "name": name, "last_updated_at": now_iso(), "status": "live"})
            out.extend(parsed)
    out = _dedupe(out)
    out.sort(key=_published_sort_key, reverse=True)
    # Strip helper fields the app schema doesn't need.
    videos = [{
        "title": v["title"], "url": v["url"], "source": v["source"],
        "thumbnail_url": v.get("thumbnail_url"), "duration_s": v.get("duration_s", 0),
    } for v in out[:MAX_VIDEOS]]
    return {"videos": videos, "sources": source_rows, "any_feed_live": any_live}


# ---------------------------------------------------------------------------
# Sections derived from the existing nationals.json (always available)
# ---------------------------------------------------------------------------


def _nats_side(game: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (nats_team, opp_team) dicts from a game's home/away."""
    if game.get("is_nats_home"):
        return game.get("home") or {}, game.get("away") or {}
    return game.get("away") or {}, game.get("home") or {}


def _the(name: str) -> str:
    """Prepend 'the' for MLB team short names ('Reds' -> 'the Reds'), unless the
    name already starts with an article."""
    if not name:
        return name
    return name if name.lower().startswith(("the ", "a ", "an ")) else f"the {name}"


def _weekday(date_str: str | None) -> str | None:
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str[:10], "%Y-%m-%d").strftime("%A")
    except ValueError:
        return None


def _parse_iso(value: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def schedule_is_current(natl: dict[str, Any], now: datetime, max_age_hours: int = 36) -> bool:
    """Whether nationals.json's schedule data reflects 'now'.

    Team Pulse / What to Watch are copied from nationals.json. If that payload
    is being served from last-good cache (normalize flagged it, or it hasn't
    refreshed recently), those sections would show weeks-old games as the current
    pulse. Gate + disclose instead of silently presenting stale games as live."""
    if "schedule" in (natl.get("stale_sources") or []):
        return False
    fetched = _parse_iso(natl.get("fetched_at"))
    if fetched is None:
        return False
    return (now - fetched) <= timedelta(hours=max_age_hours)


def build_team_pulse(natl: dict[str, Any], storyline: dict[str, Any] | None) -> dict[str, Any]:
    """Curly W win/loss banner from nationals.json::last_result + optional AI storyline."""
    banner = {"type": "none", "text": ""}
    last = natl.get("last_result")
    if last and last.get("status") == "Final":
        nats, opp = _nats_side(last)
        ns, os_ = nats.get("score"), opp.get("score")
        opp_name = _the(opp.get("short_name") or opp.get("name") or opp.get("abbrev") or "the visitors")
        if isinstance(ns, int) and isinstance(os_, int):
            if ns > os_:
                banner = {"type": "win", "text": f"Curly W! The Nats beat {opp_name} {ns}-{os_}."}
            elif ns < os_:
                banner = {"type": "loss", "text": f"Tough one — the Nats fell to {opp_name} {os_}-{ns}."}
    pulse: dict[str, Any] = {"result_banner": banner}
    if storyline and storyline.get("text"):
        pulse["storyline"] = {"text": storyline["text"], "ai_generated": True}
    return pulse


def build_what_to_watch(natl: dict[str, Any]) -> list[dict[str, Any]]:
    """Schedule-derived, source-backed items (each cites a game_pk). Always honest."""
    items: list[dict[str, Any]] = []
    for g in (natl.get("next_three") or [])[:3]:
        if g.get("status") == "Live":
            continue
        nats, opp = _nats_side(g)
        opp_name = _the(opp.get("short_name") or opp.get("name") or opp.get("abbrev") or "TBD")
        verb = "host" if g.get("is_nats_home") else "visit"
        day = _weekday(g.get("date"))
        when = f" on {day}" if day else ""
        items.append({
            "text": f"The Nats {verb} {opp_name}{when}.",
            "source_url_or_game_pk": str(g.get("game_pk") or ""),
        })
    return items


def build_on_this_day(history: dict[str, Any] | None) -> dict[str, Any]:
    """Curated 'On This Day in Nats history' from content/nats_history.json (optional)."""
    if not history:
        return {}
    entries = history.get("entries") or []
    today = datetime.now(timezone.utc)
    key = f"{today.month:02d}-{today.day:02d}"
    todays = [e for e in entries if e.get("date") == key]
    if not todays:
        return {}
    # Deterministic pick if multiple share a date.
    pick = todays[today.day % len(todays)]
    return {"text": pick.get("text", ""), "curated": True}


def build_player_spotlight(history: dict[str, Any] | None) -> dict[str, Any]:
    """Curated, rotating player spotlight from content/nats_history.json (optional).

    curated:true / ai_generated:false — these are hand-written, source-backed
    notes, never AI-invented. Rotates deterministically by day-of-year.
    """
    if not history:
        return {}
    spotlights = history.get("player_spotlights") or []
    if not spotlights:
        return {}
    doy = datetime.now(timezone.utc).timetuple().tm_yday
    pick = spotlights[doy % len(spotlights)]
    return {
        "name": pick.get("name", ""),
        "text": pick.get("text", ""),
        "image_url": pick.get("image_url"),
        "source_urls": pick.get("source_urls", []),
        "ai_generated": False,
        "curated": True,
    }


# ---------------------------------------------------------------------------
# Assembly (pure — network happens via gather_*/falkor_enrich, both mockable)
# ---------------------------------------------------------------------------


def build_payload(
    sources: dict[str, Any],
    natl: dict[str, Any],
    history: dict[str, Any] | None,
    prev: dict[str, Any] | None,
    requested_mode: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the full happenings.json payload from already-loaded inputs.

    Pulled out of main() so it is unit-testable: gather_news/gather_videos call
    the module-level http_get_text, and enrichment calls falkor_enrich — tests
    monkeypatch those rather than hitting the network. `now` is injectable so the
    schedule-freshness gate is deterministic in tests.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    # 1) News + videos (with per-section last-good).
    news = gather_news(sources)
    vids = gather_videos(sources)

    limited: list[dict[str, Any]] = []
    freshness: dict[str, Any] = {"news": None, "videos": None, "vibes": None}
    source_rows: list[dict[str, Any]] = list(news["sources"]) + list(vids["sources"])

    if news["any_feed_live"]:
        top_stories = news["stories"]
        freshness["news"] = now_iso()
        if not top_stories:
            limited.append({"id": "news", "reason": "no_current_nationals_headlines"})
    else:
        # Every news feed failed → fall back to last-good news (never blank).
        top_stories = (prev or {}).get("top_stories", []) if prev else []
        freshness["news"] = (prev or {}).get("freshness", {}).get("news") if prev else None
        # Only the NEWS source rows go not_current — video rows keep their own
        # (possibly live) status. (news["sources"] are the same dicts in source_rows.)
        for row in news["sources"]:
            if row.get("status") == "live":
                row["status"] = "not_current"
        limited.append({"id": "news", "reason": "last_check_too_old"})

    if vids["any_feed_live"]:
        videos = vids["videos"]
        freshness["videos"] = now_iso() if videos else None
    else:
        videos = (prev or {}).get("videos", []) if prev else []
        freshness["videos"] = (prev or {}).get("freshness", {}).get("videos") if prev else None
        if videos:
            limited.append({"id": "videos", "reason": "last_check_too_old"})

    # 2) Optional Falkor enrichment (falkor_enriched mode only).
    ai_applied = False
    storyline: dict[str, Any] | None = None
    trending: list[dict[str, Any]] = []
    if requested_mode == "falkor_enriched" and top_stories:
        # Group tags by a normalized key so "Luis Garcia Jr." and "Luis Garcia Jr"
        # merge into one cluster; keep the first-seen spelling as the display label.
        tag_index: dict[str, dict[str, Any]] = {}
        for story in top_stories[:ENRICH_MAX_STORIES]:
            result = falkor_enrich(story.get("title", ""), story.get("summary"), story.get("source"))
            if not result:
                continue
            ai_applied = True
            why = (result.get("summary") or result.get("why_relevant") or "").strip()
            if why:
                story["why_it_matters"] = {"text": why, "ai_generated": True}
            for tag in result.get("tags") or []:
                t = (tag or "").strip()
                key = _norm_title(t)
                if not key or key in GENERIC_TAG_KEYS:
                    # Drop generic, non-discriminating tags from the trending list.
                    continue
                entry = tag_index.setdefault(key, {"label": t, "urls": []})
                if story.get("url"):
                    entry["urls"].append(story["url"])
        # Strip the summary helper field (not part of the published schema).
        for story in top_stories:
            story.pop("summary", None)
        # Merge substring-variant clusters ("Orioles" within "Baltimore Orioles")
        # into the one with more sources, then attach the freshest cited headline
        # as context so each trending chip says something concrete (not a bare tag).
        merged: list[dict[str, Any]] = []
        for entry in sorted(tag_index.values(), key=lambda e: len(set(e["urls"])), reverse=True):
            ekey = _norm_title(entry["label"])
            host = next(
                (m for m in merged
                 if ekey == _norm_title(m["label"])
                 or ekey in _norm_title(m["label"]) or _norm_title(m["label"]) in ekey),
                None,
            )
            if host:
                host["urls"].extend(entry["urls"])
            else:
                merged.append(entry)
        for entry in merged[:MAX_TRENDING]:
            urls = sorted(set(entry["urls"]))
            url_set = set(urls)
            context = next((s.get("title") for s in top_stories if s.get("url") in url_set), None)
            trending.append({
                "label": entry["label"],
                "sentiment": "",
                "context": context,
                "source_urls": urls,
            })
        # Storyline: AI commentary grounded in the freshest cited story.
        if ai_applied and top_stories and top_stories[0].get("why_it_matters"):
            storyline = {"text": top_stories[0]["why_it_matters"]["text"]}
    else:
        for story in top_stories:
            story.pop("summary", None)

    # Thumbnails: best-effort og:image per fresh story (both modes). Last-good
    # stories keep whatever image they already carried.
    for story in top_stories:
        if "image_url" not in story:
            story["image_url"] = og_image(story.get("url"))

    effective_mode = "falkor_enriched" if ai_applied else "rss_fallback"

    # 3) Honesty disclosures for AI-dependent sections that did not run.
    if not ai_applied:
        limited.append({"id": "why_it_matters", "reason": "ai_enrichment_unavailable"})
        limited.append({"id": "trending", "reason": "ai_enrichment_unavailable"})
    # Fan Vibes (sentiment) is ALWAYS disclosed unavailable: Phase-0 recon found
    # the sanctioned enrich endpoint emits no sentiment polarity, so we never
    # fabricate one. (Re-enable only if a sanctioned sentiment source appears.)
    limited.append({"id": "fan_vibes", "reason": "ai_sentiment_unavailable"})
    freshness["vibes"] = None

    # 4) Sections derived from the existing nationals.json + curated content.
    #    Team Pulse (the Curly W banner) and What to Watch copy live games from
    #    nationals.json. If that payload is stale, gate them + disclose rather
    #    than presenting weeks-old games as the current pulse. The AI storyline
    #    is grounded in fresh news, so it survives even when the schedule doesn't.
    if schedule_is_current(natl, now):
        team_pulse = build_team_pulse(natl, storyline)
        what_to_watch = build_what_to_watch(natl)
    else:
        team_pulse = {"result_banner": {"type": "none", "text": ""}}
        if storyline and storyline.get("text"):
            team_pulse["storyline"] = {"text": storyline["text"], "ai_generated": True}
        what_to_watch = []
        limited.append({"id": "schedule", "reason": "schedule_not_current"})
    on_this_day = build_on_this_day(history)
    player_spotlight = build_player_spotlight(history)

    return {
        "data_version": DATA_VERSION,
        "generated_at": now_iso(),
        "mode": effective_mode,
        "freshness": freshness,
        "sources": source_rows,
        "limited_sources": limited,
        "top_stories": top_stories,
        "team_pulse": team_pulse,
        "videos": videos,
        # ai_generated:False — fan_vibes is deliberately empty (the sanctioned
        # enrich endpoint emits no sentiment), so NOTHING AI-touched it. It is
        # disclosed as unavailable in limited_sources above. Labeling an empty,
        # non-AI field as ai_generated:true would itself be dishonest.
        "fan_vibes": {"overall": "", "ai_generated": False, "themes": []},
        "trending": trending,
        "what_to_watch": what_to_watch,
        "on_this_day": on_this_day,
        "player_spotlight": player_spotlight,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Build happenings.json for the Nats Nation page.")
    parser.add_argument(
        "--mode",
        choices=["rss_fallback", "falkor_enriched"],
        default="rss_fallback",
        help="rss_fallback (default, no Falkor) or falkor_enriched (local, uses Falkor enrich).",
    )
    args = parser.parse_args()
    requested_mode = args.mode

    sources = read_json(CONTENT_DIR / "happenings_sources.json")
    if not sources:
        log.error("Missing content/happenings_sources.json")
        return 1
    natl = read_json(DOCS_DIR / "nationals.json", default={}) or {}
    history = read_json(CONTENT_DIR / "nats_history.json", default=None)
    prev = read_json(CACHE_DIR / "happenings.json", default=None)

    payload = build_payload(sources, natl, history, prev, requested_mode)

    write_json(CACHE_DIR / "happenings.json", payload)
    write_json(DOCS_DIR / "happenings.json", payload)
    log.info(
        "happenings.json: mode=%s (requested=%s) stories=%d videos=%d trending=%d what_to_watch=%d",
        payload["mode"], requested_mode, len(payload["top_stories"]),
        len(payload["videos"]), len(payload["trending"]), len(payload["what_to_watch"]),
    )
    return 0


if __name__ == "__main__":
    from common import main_runner

    main_runner(main)
