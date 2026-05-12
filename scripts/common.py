"""Shared utilities for nats-hub-data fetch scripts.

Keep this small and dependency-light. Only stdlib + requests + bs4.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

import requests

# ---------- paths ----------

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "cache"
CONTENT_DIR = REPO_ROOT / "content"
DOCS_DIR = REPO_ROOT / "docs"

for _d in (CACHE_DIR, CONTENT_DIR, DOCS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------- logging ----------

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

# ---------- constants ----------

NATIONALS_TEAM_ID = 120  # MLB statsapi team id
USER_AGENT = "nats-hub-data/0.1 (+https://github.com/dmgordon25/nats-hub-data)"

# Static map: MLB statsapi team id -> abbreviation. The /people hydrate doesn't
# include team abbreviation, and team abbreviations are stable, so a local map
# avoids 30 extra HTTP calls per run.
MLB_TEAM_ABBREV: dict[int, str] = {
    108: "LAA", 109: "ARI", 110: "BAL", 111: "BOS", 112: "CHC", 113: "CIN",
    114: "CLE", 115: "COL", 116: "DET", 117: "HOU", 118: "KC",  119: "LAD",
    120: "WSH", 121: "NYM", 133: "ATH", 134: "PIT", 135: "SD",  136: "SEA",
    137: "SF",  138: "STL", 139: "TB",  140: "TEX", 141: "TOR", 142: "MIN",
    143: "PHI", 144: "ATL", 145: "CWS", 146: "MIA", 147: "NYY", 158: "MIL",
}


def team_abbrev(team_id: int | None) -> str | None:
    if team_id is None:
        return None
    return MLB_TEAM_ABBREV.get(team_id)

DEFAULT_TIMEOUT = 15  # seconds
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = (1, 3, 7)  # exponential-ish


# ---------- HTTP ----------

T = TypeVar("T")


def http_get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    label: str = "request",
) -> dict[str, Any] | None:
    """GET a JSON URL with retry. Returns None on persistent failure."""
    base_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        base_headers.update(headers)

    last_err: Exception | None = None
    for attempt, backoff in enumerate(RETRY_BACKOFF_SECONDS, start=1):
        try:
            resp = requests.get(url, params=params, headers=base_headers, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            logging.warning("%s attempt %d failed: %s", label, attempt, e)
            if attempt < len(RETRY_BACKOFF_SECONDS):
                time.sleep(backoff)
    logging.error("%s failed after %d attempts: %s", label, MAX_RETRIES, last_err)
    return None


def http_get_text(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    label: str = "request",
) -> str | None:
    """GET a URL returning text body (HTML scraping)."""
    base_headers = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*"}
    if headers:
        base_headers.update(headers)

    last_err: Exception | None = None
    for attempt, backoff in enumerate(RETRY_BACKOFF_SECONDS, start=1):
        try:
            resp = requests.get(url, params=params, headers=base_headers, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except Exception as e:  # noqa: BLE001
            last_err = e
            logging.warning("%s attempt %d failed: %s", label, attempt, e)
            if attempt < len(RETRY_BACKOFF_SECONDS):
                time.sleep(backoff)
    logging.error("%s failed after %d attempts: %s", label, MAX_RETRIES, last_err)
    return None


# ---------- JSON IO ----------


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logging.error("Failed to read %s: %s", path, e)
        return default


def write_json(path: Path, data: Any, *, sort_keys: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, sort_keys=sort_keys, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(path)
    logging.info("Wrote %s (%d bytes)", path, path.stat().st_size)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def preserve_last_good(
    fresh: dict[str, Any] | None,
    cache_path: Path,
    *,
    fresh_is_valid: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """If fresh data is valid, write it to cache and return it.

    Otherwise mark the existing cache as stale (so downstream knows) and
    return that. Critical for senior-proof reliability: a bad scrape never
    overwrites a good cache, but downstream should know it's looking at
    yesterday's data.
    """
    if fresh is not None and (fresh_is_valid is None or fresh_is_valid(fresh)):
        # Make sure to clear any stale markers from a previous failed run.
        fresh.pop("_stale", None)
        fresh.pop("_stale_since", None)
        write_json(cache_path, fresh)
        return fresh
    cached = read_json(cache_path)
    if cached is not None:
        if not cached.get("_stale"):
            cached["_stale"] = True
            cached["_stale_since"] = now_iso()
        cached["_last_attempt"] = now_iso()
        write_json(cache_path, cached)
        logging.warning("Using stale cache from %s (since %s)", cache_path, cached["_stale_since"])
        return cached
    logging.error("No fresh data and no cache for %s", cache_path)
    empty = {"error": "no data available", "_fetched_at": now_iso(), "_stale": True}
    write_json(cache_path, empty)
    return empty


def main_runner(fn: Callable[[], int]) -> None:
    """Wrap a script's main fn so unexpected errors don't kill the cron."""
    try:
        code = fn()
        sys.exit(int(code))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:  # noqa: BLE001
        logging.exception("Unhandled error: %s", e)
        sys.exit(1)
