"""Unit tests for build_happenings.py — both modes, schema, last-good, honesty.

Stdlib unittest only (no new dependency). Network + Falkor are monkeypatched,
so these run offline and deterministically.

Run:
  python scripts/test_build_happenings.py
  (or)  python -m pytest scripts/test_build_happenings.py
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import build_happenings as bh  # noqa: E402


# ---------------------------------------------------------------------------
# Sample feeds
# ---------------------------------------------------------------------------

RSS_TEAM = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Federal Baseball</title>
  <item>
    <title>The Washington Nationals win a thriller</title>
    <link>https://example.com/nats-win</link>
    <pubDate>Sun, 28 Jun 2026 21:08:31 +0000</pubDate>
    <description>&lt;p&gt;CJ Abrams homered.&lt;/p&gt;</description>
  </item>
  <item>
    <title>Game thread vs Orioles</title>
    <link>https://example.com/game-thread</link>
    <pubDate>Sun, 28 Jun 2026 15:31:04 +0000</pubDate>
    <description>Discussion</description>
  </item>
</channel></rss>"""

RSS_LEAGUE = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>ESPN MLB</title>
  <item>
    <title>Dodgers beat Giants in extras</title>
    <link>https://example.com/dodgers</link>
    <pubDate>Sun, 28 Jun 2026 20:00:00 +0000</pubDate>
    <description>National League West action</description>
  </item>
  <item>
    <title>Orioles drop series to the Nationals</title>
    <link>https://example.com/orioles-nats</link>
    <pubDate>Sun, 28 Jun 2026 20:50:00 +0000</pubDate>
    <description>Washington takes the series</description>
  </item>
</channel></rss>"""

ATOM_YT = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">
  <title>Washington Nationals</title>
  <entry>
    <title>Nationals vs. Orioles Game Highlights</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=abc123"/>
    <published>2026-06-28T22:00:00+00:00</published>
    <media:group>
      <media:thumbnail url="https://i.ytimg.com/vi/abc123/hqdefault.jpg"/>
      <media:description>Highlights from tonight's win.</media:description>
    </media:group>
  </entry>
</feed>"""

SOURCES = {
    "team_keywords": ["Washington Nationals", "Nationals", "Nats", "CJ Abrams"],
    "news_feeds": {
        "team": [{"id": "federal_baseball", "name": "Federal Baseball", "url": "team://news"}],
        "league": [{"id": "espn_mlb", "name": "ESPN MLB", "url": "league://news"}],
    },
    "video_feeds": {
        "team": [{"id": "yt_nationals", "name": "Nationals on YouTube", "url": "team://video"}],
        "league": [],
    },
}

NATIONALS_JSON = {
    "last_result": {
        "status": "Final",
        "is_nats_home": True,
        "home": {"short_name": "Nationals", "score": 8},
        "away": {"short_name": "Reds", "score": 7},
    },
    "next_three": [
        {"game_pk": 111, "date": "2026-06-29", "is_nats_home": True,
         "away": {"short_name": "Orioles"}, "home": {"short_name": "Nationals"}, "status": "Preview"},
        {"game_pk": 222, "date": "2026-07-01", "is_nats_home": False,
         "away": {"short_name": "Nationals"}, "home": {"short_name": "Mets"}, "status": "Preview"},
    ],
}

ENRICHED = {
    "state": "enriched", "ai_generated": True,
    "disclaimer": "ai_generated_commentary_not_source_fact", "model": "qwen3.5:9b",
    "summary": "The Nationals won a close game behind CJ Abrams.",
    "tags": ["CJ Abrams", "Nationals", "baseball"], "why_relevant": "Fans care about the win.",
    "classification": "sports",
}


def _fake_http_get_text(url, **kwargs):
    return {
        "team://news": RSS_TEAM,
        "league://news": RSS_LEAGUE,
        "team://video": ATOM_YT,
    }.get(url)


class FeedParsingTests(unittest.TestCase):
    def test_to_iso_rfc822(self):
        self.assertEqual(bh.to_iso("Sun, 28 Jun 2026 21:08:31 +0000"), "2026-06-28T21:08:31+00:00")

    def test_to_iso_atom(self):
        self.assertEqual(bh.to_iso("2026-06-28T22:00:00+00:00"), "2026-06-28T22:00:00+00:00")

    def test_to_iso_bad(self):
        self.assertIsNone(bh.to_iso("not a date"))
        self.assertIsNone(bh.to_iso(None))

    def test_parse_rss(self):
        items = bh.parse_news_feed(RSS_TEAM, "Federal Baseball")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "The Washington Nationals win a thriller")
        self.assertEqual(items[0]["url"], "https://example.com/nats-win")
        self.assertEqual(items[0]["source"], "Federal Baseball")
        self.assertTrue(items[0]["published_at"].startswith("2026-06-28"))
        self.assertNotIn("<p>", items[0]["summary"])  # HTML stripped

    def test_parse_video(self):
        vids = bh.parse_video_feed(ATOM_YT, "Nationals on YouTube")
        self.assertEqual(len(vids), 1)
        self.assertEqual(vids[0]["url"], "https://www.youtube.com/watch?v=abc123")
        self.assertEqual(vids[0]["thumbnail_url"], "https://i.ytimg.com/vi/abc123/hqdefault.jpg")
        self.assertEqual(vids[0]["duration_s"], 0)

    def test_parse_bad_xml(self):
        self.assertEqual(bh.parse_news_feed("<not xml", "x"), [])


class FilterTests(unittest.TestCase):
    def test_is_nationals_positive(self):
        kw = SOURCES["team_keywords"]
        self.assertTrue(bh.is_nationals("The Nationals won", kw))
        self.assertTrue(bh.is_nationals("Go Nats tonight", kw))
        self.assertTrue(bh.is_nationals("CJ Abrams homers", kw))

    def test_is_nationals_negative(self):
        kw = SOURCES["team_keywords"]
        self.assertFalse(bh.is_nationals("National League West roundup", kw))
        self.assertFalse(bh.is_nationals("A cloud of gnats", kw))
        self.assertFalse(bh.is_nationals("Dodgers beat Giants", kw))

    def test_dedupe_by_title_and_url(self):
        items = [
            {"title": "Same Story", "url": "https://a.com/1"},
            {"title": "Same Story", "url": "https://b.com/2"},  # dup title, diff url
            {"title": "Other", "url": "https://a.com/1?utm=x"},  # dup url (normalized)
            {"title": "Unique", "url": "https://c.com/3"},
        ]
        out = bh._dedupe(items)
        self.assertEqual(len(out), 2)
        self.assertEqual({o["title"] for o in out}, {"Same Story", "Unique"})

    def test_the_and_weekday(self):
        self.assertEqual(bh._the("Reds"), "the Reds")
        self.assertEqual(bh._the("the Mets"), "the Mets")
        self.assertEqual(bh._weekday("2026-06-28"), "Sunday")
        self.assertIsNone(bh._weekday(None))


class SectionTests(unittest.TestCase):
    def test_team_pulse_win(self):
        pulse = bh.build_team_pulse(NATIONALS_JSON, None)
        self.assertEqual(pulse["result_banner"]["type"], "win")
        self.assertIn("Curly W", pulse["result_banner"]["text"])
        self.assertIn("the Reds", pulse["result_banner"]["text"])
        self.assertNotIn("storyline", pulse)

    def test_team_pulse_loss(self):
        natl = {"last_result": {"status": "Final", "is_nats_home": True,
                "home": {"short_name": "Nationals", "score": 2},
                "away": {"short_name": "Braves", "score": 5}}}
        pulse = bh.build_team_pulse(natl, None)
        self.assertEqual(pulse["result_banner"]["type"], "loss")

    def test_team_pulse_none(self):
        pulse = bh.build_team_pulse({"last_result": {"status": "Preview"}}, None)
        self.assertEqual(pulse["result_banner"]["type"], "none")

    def test_what_to_watch_cites_game_pk(self):
        items = bh.build_what_to_watch(NATIONALS_JSON)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["source_url_or_game_pk"], "111")
        self.assertIn("the Orioles", items[0]["text"])
        self.assertIn("on Monday", items[0]["text"])  # 2026-06-29 is a Monday


class PayloadTests(unittest.TestCase):
    SCHEMA_KEYS = {
        "data_version", "generated_at", "mode", "freshness", "sources",
        "limited_sources", "top_stories", "team_pulse", "videos", "fan_vibes",
        "trending", "what_to_watch", "on_this_day", "player_spotlight",
    }

    def setUp(self):
        self._orig_http = bh.http_get_text
        self._orig_enrich = bh.falkor_enrich
        bh.http_get_text = _fake_http_get_text

    def tearDown(self):
        bh.http_get_text = self._orig_http
        bh.falkor_enrich = self._orig_enrich

    def test_rss_fallback_schema_and_honesty(self):
        bh.falkor_enrich = lambda *a, **k: None
        p = bh.build_payload(SOURCES, NATIONALS_JSON, None, None, "rss_fallback")
        self.assertEqual(self.SCHEMA_KEYS, set(p.keys()))
        self.assertEqual(p["mode"], "rss_fallback")
        # Real stories came through; league feed filtered to the Nats item only.
        titles = [s["title"] for s in p["top_stories"]]
        self.assertIn("The Washington Nationals win a thriller", titles)
        self.assertIn("Orioles drop series to the Nationals", titles)
        self.assertNotIn("Dodgers beat Giants in extras", titles)  # filtered out
        # No AI fields in rss mode.
        for s in p["top_stories"]:
            self.assertNotIn("why_it_matters", s)
            self.assertNotIn("summary", s)  # helper field stripped
        self.assertEqual(p["trending"], [])
        # Honesty: every AI-dependent section disclosed; never the word "stale".
        disclosed = {d["id"] for d in p["limited_sources"]}
        self.assertIn("fan_vibes", disclosed)
        self.assertIn("why_it_matters", disclosed)
        self.assertIn("trending", disclosed)
        self.assertNotIn("stale", json.dumps(p).lower())
        # fan_vibes is deliberately empty (no AI sentiment) → must NOT claim to be
        # ai_generated, and must carry no themes (so the app self-hides it).
        self.assertFalse(p["fan_vibes"]["ai_generated"])
        self.assertEqual(p["fan_vibes"]["themes"], [])

    def test_falkor_enriched_labels_and_citations(self):
        bh.falkor_enrich = lambda *a, **k: dict(ENRICHED)
        p = bh.build_payload(SOURCES, NATIONALS_JSON, None, None, "falkor_enriched")
        self.assertEqual(p["mode"], "falkor_enriched")
        # why_it_matters present and labeled ai_generated.
        wim = [s for s in p["top_stories"] if "why_it_matters" in s]
        self.assertTrue(wim)
        for s in wim:
            self.assertTrue(s["why_it_matters"]["ai_generated"])
        # Trending clusters trace to cited source_urls (no fabrication).
        self.assertTrue(p["trending"])
        for t in p["trending"]:
            self.assertTrue(t["source_urls"], "trending item must cite sources")
            self.assertNotIn(t["label"].lower(), bh.GENERIC_TAG_KEYS)  # generic dropped
        # AI disclosures lifted, but fan_vibes (no sentiment primitive) stays disclosed
        # AND is never labeled ai_generated even in falkor_enriched mode.
        disclosed = {d["id"] for d in p["limited_sources"]}
        self.assertIn("fan_vibes", disclosed)
        self.assertNotIn("why_it_matters", disclosed)
        self.assertFalse(p["fan_vibes"]["ai_generated"])
        # storyline labeled ai_generated.
        self.assertTrue(p["team_pulse"].get("storyline", {}).get("ai_generated"))

    def test_last_good_news_fallback(self):
        # All feeds fail → reuse last-good news, disclose "last_check_too_old".
        bh.http_get_text = lambda url, **k: None
        bh.falkor_enrich = lambda *a, **k: None
        prev = {
            "top_stories": [{"title": "Yesterday's news", "url": "https://x/y", "source": "Federal Baseball"}],
            "videos": [{"title": "Old clip", "url": "https://x/v", "source": "Nationals on YouTube",
                        "thumbnail_url": None, "duration_s": 0}],
            "freshness": {"news": "2026-06-27T00:00:00+00:00", "videos": "2026-06-27T00:00:00+00:00"},
        }
        p = bh.build_payload(SOURCES, NATIONALS_JSON, None, prev, "rss_fallback")
        self.assertEqual(p["top_stories"][0]["title"], "Yesterday's news")
        self.assertEqual(p["freshness"]["news"], "2026-06-27T00:00:00+00:00")
        disclosed = {d["id"]: d["reason"] for d in p["limited_sources"]}
        self.assertEqual(disclosed.get("news"), "last_check_too_old")
        for row in p["sources"]:
            self.assertIn(row["status"], ("timed_out", "not_current"))

    def test_empty_quiet_day_self_hides(self):
        # Feeds respond but with no Nationals items → empty sections, disclosed,
        # never blank/fabricated.
        empty_rss = '<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
        bh.http_get_text = lambda url, **k: empty_rss
        bh.falkor_enrich = lambda *a, **k: None
        p = bh.build_payload(SOURCES, {}, None, None, "rss_fallback")
        self.assertEqual(p["top_stories"], [])
        self.assertEqual(p["videos"], [])
        # team_pulse banner self-hides (none) when there's no nationals.json.
        self.assertEqual(p["team_pulse"]["result_banner"]["type"], "none")
        disclosed = {d["id"] for d in p["limited_sources"]}
        self.assertIn("news", disclosed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
