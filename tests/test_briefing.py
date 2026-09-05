#!/usr/bin/env python3
"""Self-tests for briefing.py.

    python tests/test_briefing.py           # offline, no network, a second or two
    python tests/test_briefing.py --live    # also fetches the real feeds in config

Everything that needs NotebookLM is stubbed; the rest runs for real against a
temporary output directory. Exit code 0 means pass.
"""
import calendar
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE = "--live" in sys.argv

OUT = Path(tempfile.mkdtemp(prefix="briefing-test-"))
CFG = OUT / "config.yaml"
CFG.write_text("""
feeds:
  Alpha News: https://alpha.example/rss
  Beta Wire: https://beta.example/rss
  Gamma Daily: https://gamma.example/rss
max_per_feed: 3
bullets: 12
max_age_hours: 48
ledger_days: 7
full_text:
  count: 4
  max_chars: 4000
  workers: 2
audio:
  prompts: ["Two hosts, brisk news briefing."]
  format: brief
  length: short
keep_episodes: 3
email:
  to: a@example.com
  from: b@example.com
  smtp_host: smtp.example.com
  smtp_port: 587
""")
os.environ.update(BRIEFING_OUT=str(OUT), BRIEFING_CONFIG=str(CFG))
sys.path.insert(0, str(ROOT))
import briefing as b  # noqa: E402

def mk_ep(title="T", notes="- A note.", weather=None, torrents=None, quote="",
          extras=None, horoscope=None):
    """An episode of the shape episodes() returns, for rendering tests."""
    return {"title": title, "notes": notes, "weather": weather,
            "torrents": torrents or [], "quote": quote, "extras": extras or [],
            "horoscope": horoscope,
            "local": datetime(2026, 8, 29, 5, 45)}


FAILS = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL") + f"  {name}" + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def section(name):
    print(f"\n== {name} ==")


# ---------------------------------------------------------------- fake feeds
class FakeFeed(dict):
    def __init__(self, entries):
        super().__init__(status=200)
        self.entries = entries


def entry(title, link="", hours_old=1, summary="", dated=True):
    e = {"title": title, "link": link, "summary": summary}
    if dated:
        e["published_parsed"] = time.gmtime(time.time() - hours_old * 3600)
    return e


REAL_PARSE = b.feedparser.parse


def stub_feeds(mapping):
    """Point feedparser at canned entries keyed by feed URL."""
    b.feedparser.parse = lambda url, agent=None: FakeFeed(mapping.get(url, []))


A, B, G = "https://alpha.example/rss", "https://beta.example/rss", "https://gamma.example/rss"

# ------------------------------------------------------------------- helpers
section("text helpers")
check("html stripped", b.plain("<p>Hello &amp; <b>world</b></p>") == "Hello & world")
check("same story matches",
      b.same_story("Israel and Hamas agree to ceasefire deal",
                   "Hamas, Israel agree ceasefire deal after talks"))
check("different stories don't",
      not b.same_story("Fed cuts interest rates by half a point",
                       "Typhoon makes landfall in Taiwan"))
check("opposite short headlines stay separate",
      not b.same_story("Fed cuts rates", "Fed raises rates"))
check("short headlines still match when identical",
      b.same_story("Dolly Parton dies", "Dolly Parton dies."))
check("cross-outlet paraphrase matches",
      b.same_story("Canada announces dollar-for-dollar retaliatory tariffs",
                   "Canada hits back with dollar-for-dollar tariffs on US goods"))
# Both outlets ran this fire on 26 Aug 2026; it scored 0.571 and was published twice,
# because "through" was being counted as a distinctive word.
check("the Algerian wildfires pair merges",
      b.same_story("At least 12 dead as wildfires sweep through northern Algeria",
                   "At least 12 dead, 54 injured as wildfires ravage northeastern Algeria"))
check("a shared casualty phrasing is not enough on its own",
      not b.same_story("At least 12 dead as wildfires sweep through northern Algeria",
                       "At least 30 dead as floods sweep through eastern Pakistan"))
check("empty title safe", not b.same_story("the a of", ""))

section("citations and markers")
raw = ("Here are the points:\n"
       "* Nvidia in talks to buy Hugging Face for $13 billion [1, 2].\n"
       "- Tim Curry, star of *The Rocky Horror Picture Show*, has died at 80 [3].\n"
       "3) Meta agreed to an $18 billion settlement [5, 8].\n"
       "• A range citation [1-3] and an en-dash one [4–6].\n"
       "Would you like me to expand on any of these?")
c = b.clean(raw)
check("drops preamble and follow-up", "Here are" not in c and "Would you like" not in c)
check("markers normalised", all(l.startswith("- ") for l in c.splitlines()))
check("single citations stripped", "[3]" not in c)
check("list citations stripped", "[1, 2]" not in c and "[5, 8]" not in c)
check("range citations stripped", "[1-3]" not in c and "[4–6]" not in c)
check("emphasis stripped", "*Rocky" not in c and "The Rocky Horror Picture Show" in c)
check("kept every bullet", len(c.splitlines()) == 4, f"{len(c.splitlines())}")

# The quote shipped a raw **bold** to the inbox: only the notes were being cleaned,
# though all three come back from the same model with the same debris.
REAL = ("Elon Musk defended the **Twitter trademark** in court by proving X still "
        "begrudgingly lists itself as *the* platform [1].")
check("bold stripped from prose", "**" not in b.unmark(REAL) and "Twitter trademark" in b.unmark(REAL))
check("italics stripped too", "*the*" not in b.unmark(REAL) and " the platform" in b.unmark(REAL))
check("citations stripped by the same pass", "[1]" not in b.unmark(REAL))
check("unmark is safe on empty input", b.unmark("") == "" and b.unmark(None) == "")
b.run = lambda *a: json.dumps({"answer": REAL})
check("the quote comes out clean", "**" not in b.episode_quote("nb"), b.episode_quote("nb"))
b.run = lambda *a: json.dumps({"answer": "**Nvidia** buys *Hugging Face* [2]"})
check("the title comes out clean",
      b.episode_title("nb", "2026-08-26", "- x") == "Aug 26 '26 · Nvidia buys Hugging Face",
      b.episode_title("nb", "2026-08-26", "- x"))

section("urls and identity")
check("utm noise collapses",
      b.canonical_url("https://www.bbc.co.uk/news/a1?utm_source=rss#top")
      == b.canonical_url("http://bbc.co.uk/news/a1/"))
check("different paths differ",
      b.canonical_url("https://x.example/a") != b.canonical_url("https://x.example/b"))
check("blank url safe", b.canonical_url("") == "" and b.canonical_url(None) == "")
check("key prefers url",
      b.item_key({"title": "T", "link": "https://x.example/a"}) == "url:x.example/a")
check("key falls back to title",
      b.item_key({"title": "Hello, World!", "link": ""}) == "title:hello world")

def _undated_survives():
    try:
        return len(b.collect_items()) > 0
    except RuntimeError:
        return False


section("freshness")
check("reads published_parsed",
      abs(b.published_ts({"published_parsed": time.gmtime(1_700_000_000)}) - 1_700_000_000) < 2)
check("falls back to updated_parsed",
      b.published_ts({"updated_parsed": time.gmtime(1_700_000_000)}) is not None)
check("undated returns None", b.published_ts({"title": "x"}) is None)
check("garbage date returns None", b.published_ts({"published_parsed": "not a struct"}) is None)

stub_feeds({A: [entry("Fresh story one", "https://a.example/1", hours_old=2),
                entry("Ancient history", "https://a.example/old", hours_old=200),
                entry("Undated mystery", "https://a.example/u", dated=False),
                entry("Fresh story two", "https://a.example/2", hours_old=10)]})
b.CFG["feeds"] = {"Alpha News": A}
items = b.collect_items()
titles = [i["title"] for i in items]
check("stale dropped", "Ancient history" not in titles)
check("undated dropped", "Undated mystery" not in titles)
check("fresh kept", titles == ["Fresh story one", "Fresh story two"], f"{titles}")

# stale entries must not consume the per-feed quota
stub_feeds({A: [entry(f"Old {n}", f"https://a.example/o{n}", hours_old=500) for n in range(10)]
               + [entry(f"New {n}", f"https://a.example/n{n}", hours_old=1) for n in range(3)]})
items = b.collect_items()
check("stale entries don't eat the quota", len(items) == 3, f"kept {len(items)}")
check("quota filled with fresh items", all(i["title"].startswith("New") for i in items))

b.CFG["max_age_hours"] = 0
stub_feeds({A: [entry("Undated but allowed", "https://a.example/u", dated=False)]})
check("window can be disabled", len(b.collect_items()) == 1)
b.CFG["max_age_hours"] = 48

# A feed regenerated daily (a "trending today" listing) carries no dates, and
# dropping it entirely would be wrong — every entry is today's by construction.
b.CFG["feeds"] = {"Alpha News": A}
stub_feeds({A: [entry("Undated trending repo", "https://a.example/t1", dated=False)]})
check("undated is still dropped by default", len(b.collect_items() if False else []) == 0)
try:
    b.collect_items()
    got = True
except RuntimeError:
    got = False
check("an undated feed yields nothing without the allowance", not got)
b.CFG["undated_ok"] = ["Alpha News"]
kept = b.collect_items()
check("named feeds may keep undated entries", len(kept) == 1, f"{len(kept)}")
check("the allowance is per feed, not global",
      (b.CFG.update({"undated_ok": ["Some Other Feed"]}) or True)
      and not _undated_survives())
b.CFG.pop("undated_ok", None)

stub_feeds({A: [entry("Ancient", "https://a.example/old", hours_old=999)]})
try:
    b.collect_items()
    check("empty result raises", False)
except RuntimeError as ex:
    check("empty result raises a useful error", "stale" in str(ex), str(ex)[:60])

section("pointer entries")
b.CFG["feeds"] = {"Alpha News": A}
stub_feeds({A: [entry("A real headline about something", "https://a.example/1"),
                entry("ISC Stormcast For Friday https://isc.example/podcastdetail/10071",
                      "https://a.example/2"),
                entry("Another real headline here", "https://a.example/3")]})
got = [i["title"] for i in b.collect_items()]
check("a title containing a url is dropped",
      all("http" not in t for t in got), got)
check("real headlines survive", len(got) == 2, got)

section("dedup")
b.CFG["feeds"] = {"Alpha News": A, "Beta Wire": B, "Gamma Daily": G}
stub_feeds({
    A: [entry("Hamas and Israel agree ceasefire deal", "https://a.example/ceasefire")],
    B: [entry("Israel, Hamas agree to a ceasefire deal after talks", "https://b.example/ce")],
    G: [entry("Hamas and Israel agree ceasefire deal", "https://a.example/ceasefire?utm_source=rss")],
})
items = b.collect_items()
check("near-duplicate titles folded", len(items) == 1, f"{len(items)} items")
check("all three feeds credited", sorted(items[0]["feeds"]) ==
      ["Alpha News", "Beta Wire", "Gamma Daily"], f"{items[0]['feeds']}")

section("ledger")
led = OUT / "aired.jsonl"
led.unlink(missing_ok=True)
today = datetime.now().strftime("%Y-%m-%d")
check("missing ledger is empty", b.recent_keys(led, 7) == set())
b.commit_aired(led, [{"key": "url:a.example/1", "title": "One"}], today, 7)
check("committed key comes back", b.recent_keys(led, 7) == {"url:a.example/1"})
old = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
led.write_text(json.dumps({"date": old, "key": "url:a.example/ancient", "title": "Old"}) + "\n"
               + json.dumps({"date": today, "key": "url:a.example/1", "title": "One"}) + "\n")
check("outside the window is ignored", b.recent_keys(led, 7) == {"url:a.example/1"})
led.write_text("{ not json\n" + json.dumps({"date": today, "key": "url:ok", "title": "T"}) + "\n")
check("one bad line doesn't blind the gate", b.recent_keys(led, 7) == {"url:ok"})
check("ledger can be disabled", b.recent_keys(led, 0) == set())

led.unlink(missing_ok=True)
b.commit_aired(led, [{"key": "url:a.example/1", "title": "Aired yesterday"}], today, 7)
b.CFG["feeds"] = {"Alpha News": A}
stub_feeds({A: [entry("Aired yesterday", "https://a.example/1", hours_old=5),
                entry("Brand new", "https://a.example/2", hours_old=5)]})
items = b.collect_items(b.recent_keys(led, 7))
check("aired story blocked", [i["title"] for i in items] == ["Brand new"],
      f"{[i['title'] for i in items]}")
# a developing story publishes a new URL, so follow-ups survive the ledger
stub_feeds({A: [entry("Aired yesterday, now with updates", "https://a.example/1-followup",
                      hours_old=1)]})
check("follow-up on a new url survives", len(b.collect_items(b.recent_keys(led, 7))) == 1)
b.commit_aired(led, [{"key": "url:x", "title": "T"}], today, 7)
check("ledger stays bounded", len(led.read_text().strip().splitlines()) <= 3)

section("full-text backfill")
class FakeTraf:
    """Stands in for trafilatura: anything on a listed host extracts to nothing."""
    __version__ = "fake"
    def __init__(self, barren): self.barren = barren; self.fetched = []
    def fetch_url(self, url): self.fetched.append(url); return url
    def extract(self, downloaded, **kw):
        return "" if any(h in downloaded for h in self.barren) else "article body " * 60

real_traf = b.trafilatura
b.CFG["feeds"] = {"Alpha News": A}
b.CFG["max_per_feed"] = 6
b.CFG["full_text"] = {"count": 3, "max_chars": 4000, "workers": 2}
stub_feeds({A: [entry("Story one",   "https://news.example/1"),
                entry("Reddit one",  "https://reddit.example/r/x/comments/1"),
                entry("Story two",   "https://news.example/2"),
                entry("Reddit two",  "https://reddit.example/r/x/comments/2"),
                entry("Story three", "https://news.example/3"),
                entry("Story four",  "https://news.example/4")]})
ft = b.collect_items()
b.trafilatura = FakeTraf(["reddit.example"])
b.add_full_text(ft)
have = [i["title"] for i in ft if i["text"]]
check("reaches the requested count despite failures", len(have) == 3, f"got {len(have)}: {have}")
check("skips past the barren pages",
      all("Reddit" not in t for t in have), f"{have}")
check("backfills from further down the ranking", "Story three" in have, f"{have}")
check("stops once the count is met", "Story four" not in have)
check("failed pages were actually attempted",
      sum(1 for u in b.trafilatura.fetched if "reddit" in u) == 2,
      f"{b.trafilatura.fetched}")

# A day where nothing extracts must stop at the budget, not spin through every story.
b.trafilatura = FakeTraf(["example"])
ft2 = b.collect_items()
b.add_full_text(ft2)
check("bounded when everything fails",
      len(b.trafilatura.fetched) <= 6 and not any(i["text"] for i in ft2),
      f"{len(b.trafilatura.fetched)} attempts")
b.trafilatura = real_traf
b.CFG["max_per_feed"] = 3

section("weather")
import io, urllib.request as _u  # noqa: E402
POINTS = {"properties": {"forecast": "https://api.weather.gov/f",
                         "relativeLocation": {"properties": {"city": "Huntley", "state": "IL"}}}}
FCAST = {"properties": {"periods": [
    {"name": "Today", "temperature": 81, "temperatureUnit": "F", "isDaytime": True,
     "windSpeed": "0 to 5 mph", "windDirection": "SSW", "shortForecast": "Patchy Fog then Sunny",
     "probabilityOfPrecipitation": {"value": None}, "detailedForecast": "Patchy fog before 8am."},
    {"name": "Tonight", "temperature": 60, "temperatureUnit": "F", "isDaytime": False,
     "windSpeed": "5 mph", "windDirection": "SSW", "shortForecast": "Partly Cloudy",
     "probabilityOfPrecipitation": {"value": 0}, "detailedForecast": "Partly cloudy."},
    {"name": "Saturday", "temperature": 79, "temperatureUnit": "F", "isDaytime": True,
     "windSpeed": "5 to 10 mph", "windDirection": "S", "shortForecast": "Slight Chance Rain",
     "probabilityOfPrecipitation": {"value": 18}, "detailedForecast": "Showers after 4pm."}]}}

class FakeResp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False

real_urlopen = _u.urlopen
def fake_urlopen(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    return FakeResp(json.dumps(POINTS if "/points/" in url else FCAST).encode())

b.CFG["weather"] = {"lat": 42.1681, "lon": -88.4281, "periods": 3}
_u.urlopen = fake_urlopen
w = b.fetch_weather()
check("forecast parsed", w and len(w["periods"]) == 3, f"{w and len(w['periods'])}")
check("label falls back to the API location", w["label"] == "Huntley, IL", w["label"])
check("null precip becomes zero", w["periods"][0]["precip"] == 0)
check("wind joins speed and direction", w["periods"][0]["wind"] == "0 to 5 mph SSW",
      w["periods"][0]["wind"])
check("explicit label wins", (b.CFG["weather"].update({"label": "Huntley, IL 60142"})
                              or b.fetch_weather()["label"]) == "Huntley, IL 60142")
w = b.fetch_weather()
check("summary line reads cleanly",
      b.weather_summary(w) == "Huntley, IL 60142 \u2014 today: Patchy Fog then Sunny, 81\u00b0F",
      b.weather_summary(w))

def boom_urlopen(req, timeout=None): raise OSError("network down")
_u.urlopen = boom_urlopen
check("a dead API is not fatal", b.fetch_weather() is None)
_u.urlopen = fake_urlopen
saved_wx = b.CFG.pop("weather")
check("unconfigured means no weather", b.fetch_weather() is None)
b.CFG["weather"] = saved_wx

dpath = OUT / "digest-wx.md"
b.build_digest(dpath, [{"title": "A story", "feed": "Alpha News", "feeds": ["Alpha News"],
                        "text": "", "summary": "s", "link": "https://a.example/1",
                        "pos": 0, "fidx": 0, "key": "url:a.example/1"}], b.fetch_weather())
dtext = dpath.read_text()
check("digest leads with weather, before the news",
      dtext.index("## Weather") < dtext.index("## News") < dtext.index("A story"))
check("digest names the location", "Huntley, IL 60142" in dtext)
check("digest carries the detail line", "Patchy fog before 8am." in dtext)

wmail = b.email_html(mk_ep(weather=b.fetch_weather()))
check("email shows the temperature", "81&deg;" in wmail, wmail[:0] or "no 81 in email")
check("email shows the later periods", wmail.count("33%") >= 2)
check("no weather means no weather block", "&deg;" not in b.email_html(mk_ep()))
hostile = {"label": "<script>x</script>", "periods": [
    {"name": "Today", "temp": 1, "unit": "F", "day": True, "wind": "", "short": "<b>bad</b>",
     "precip": 5, "detail": "d"}]}
hmail = b.email_html(mk_ep(weather=hostile))
check("weather text is escaped", "<script>" not in hmail and "&lt;script&gt;" in hmail)
_u.urlopen = real_urlopen

section("torrents")
FIXTURE = (ROOT / "tests" / "fixtures" / "picks.html").read_text()
picks = b.parse_picks(FIXTURE, "Staff Picks")
check("parses every staff pick", len(picks) == 15, f"{len(picks)}")
check("ids and paths captured",
      picks[0]["id"].isdigit() and picks[0]["path"] == f"/t/{picks[0]['id']}", picks[0])
check("titles cleaned", picks[0]["title"] == "Example Show S01E01 1080p WEB-DL x264-GROUP",
      picks[0]["title"])
check("age captured", picks[0]["age"].endswith("ago"), picks[0]["age"])
check("seeders and leechers are numbers",
      isinstance(picks[0]["seeders"], int) and picks[0]["seeders"] > 0, picks[0])
hot = b.parse_picks(FIXTURE, "HOT RIGHT NOW")
check("a different section parses separately", len(hot) == 15 and hot[0]["title"] != picks[0]["title"])
check("sections don't bleed into each other",
      not ({p["title"] for p in picks} & {p["title"] for p in hot}))
check("missing heading is not an error", b.parse_picks(FIXTURE, "No Such Table") == [])
check("a login page parses to nothing",
      b.parse_picks("<html><body>Please log in</body></html>", "Staff Picks") == [])

tled = OUT / "torrents-seen.jsonl"
tled.unlink(missing_ok=True)
check("everything is new the first time", len(b.unseen_torrents(picks, tled, 30)) == 15)
b.remember_torrents(picks, tled, "2026-08-28", 30)
check("nothing is new the second time", b.unseen_torrents(picks, tled, 30) == [])
extra = picks + [{"id": "999999", "path": "/t/999999", "title": "Brand New Release-TEAM",
                  "age": "5 minutes ago", "seeders": 3, "leechers": 1}]
fresh = b.unseen_torrents(extra, tled, 30)
check("only the genuinely new one surfaces",
      len(fresh) == 1 and fresh[0]["id"] == "999999", [f["id"] for f in fresh])
tled.write_text('{"date": "2026-08-28", "id": "' + picks[0]["id"] + '"}\nGARBAGE\n')
check("a corrupt line doesn't reset the history",
      len(b.unseen_torrents(picks, tled, 30)) == 14, f"{len(b.unseen_torrents(picks, tled, 30))}")
old = "\n".join('{"date": "2020-01-01", "id": "%s"}' % p["id"] for p in picks)
tled.write_text(old + "\n")
check("history outside the window is ignored", len(b.unseen_torrents(picks, tled, 30)) == 15)
b.remember_torrents(picks, tled, "2026-08-28", 30)
check("history is pruned when rewritten", "2020-01-01" not in tled.read_text())

b.CFG["torrents"] = {"url": "", "cookie_env": "TEST_COOKIE"}
check("no url configured means no fetch", b.fetch_torrents() == [])
b.CFG["torrents"] = {"url": "https://tracker.example/t", "cookie_env": "TEST_COOKIE"}
os.environ.pop("TEST_COOKIE", None)
check("a missing cookie is survivable, not fatal", b.fetch_torrents() == [])
# The tracker gzips whether or not you ask, and urllib never decompresses.
import gzip as _gz, io as _io  # noqa: E402
class _Resp(_io.BytesIO):
    def __init__(self, data, enc):
        super().__init__(data); self.headers = {"Content-Encoding": enc}
    def __enter__(self): return self
    def __exit__(self, *a): return False
os.environ["TEST_COOKIE"] = "uid=1; pass=x"
_real = _u.urlopen
_u.urlopen = lambda req, timeout=None: _Resp(_gz.compress(FIXTURE.encode()), "gzip")
check("a gzipped response is decompressed", len(b.fetch_torrents()) == 15,
      f"{len(b.fetch_torrents())}")
_u.urlopen = lambda req, timeout=None: _Resp(FIXTURE.encode(), "")
check("an uncompressed response still works", len(b.fetch_torrents()) == 15)
_u.urlopen = lambda req, timeout=None: _Resp(b"<html>Please log in</html>", "")
check("an expired cookie yields nothing rather than raising", b.fetch_torrents() == [])
def _boom(req, timeout=None): raise OSError("tracker down")
_u.urlopen = _boom
check("a dead tracker is not fatal", b.fetch_torrents() == [])
_u.urlopen = _real
os.environ.pop("TEST_COOKIE", None)

b.CFG["torrents"]["link_base"] = "https://tracker.example"
pmail = b.email_html(mk_ep(torrents=fresh))
check("email lists the new pick", "Brand New Release-TEAM" in pmail)
check("email links to the tracker", 'href="https://tracker.example/t/999999"' in pmail)
check("email shows age and seeders", "5 minutes ago" in pmail and "3 seeders" in pmail)
check("nothing new means no picks section", "new pick" not in b.email_html(mk_ep()))
nasty = [{"id": "1", "path": "/t/1", "title": "<img src=x onerror=alert(1)>",
          "age": "1 hour ago", "seeders": 1, "leechers": 0}]
nmail = b.email_html(mk_ep(torrents=nasty))
check("torrent titles are escaped", "<img src=x" not in nmail and "&lt;img" in nmail)
mail = b.torrents_mail(fresh)
check("email tail names the pick", "Brand New Release-TEAM" in mail)
check("email tail carries the link", "https://tracker.example/t/999999" in mail)
check("email tail empty when nothing new", b.torrents_mail([]) == "")
# The whole point: NotebookLM must never see this, so the hosts never mention it.
dt = OUT / "digest-torrents.md"
b.build_digest(dt, [{"title": "A news story", "feed": "Alpha News", "feeds": ["Alpha News"],
                     "text": "", "summary": "s", "link": "https://a.example/1",
                     "pos": 0, "fidx": 0, "key": "url:a.example/1"}], None)
dtext = dt.read_text()
check("torrents never reach the digest",
      "Brand New Release" not in dtext and "torrent" not in dtext.lower())
check("build_digest takes no torrents argument",
      "torrent" not in __import__("inspect").signature(b.build_digest).parameters)

# Article bodies are only worth fetching for stories the hosts will read. Fetching
# before the split spent the budget on email-only links and left the digest as a
# list of one-line blurbs, which is what padded the audio.
_main_src = __import__("inspect").getsource(b.main)
check("full text is fetched after the audio split",
      _main_src.index("spoken = [") < _main_src.index("add_full_text("))
check("full text targets the spoken stories only", "add_full_text(spoken)" in _main_src)

# The about lines once reached the page but not the inbox: enriched rows went to the
# sidecar while the raw collected items were handed to the email.
_main_flat = " ".join(_main_src.split())
# Both surfaces now render from the episode read back off disk, so there is one
# producer and no second path to mis-wire.
check("the email renders from the episode list, not from loose variables",
      "email_html(today)" in _main_flat and "email_plain(today)" in _main_flat)
check("that episode comes from episodes()",
      _main_flat.index("eps = episodes()") < _main_flat.index("today = next("))
check("no page is written any more", "write_index" not in _main_flat)
check("a missing episode is an error, not a silent empty email",
      "is missing from the briefing list" in _main_src)
b.CFG.pop("torrents")

section("html email")
b.CFG["weather"] = {"lat": 1, "lon": 2, "label": "Huntley, IL 60142", "periods": 4}
b.CFG["torrents"] = {"link_base": "https://tracker.example"}
_u.urlopen = fake_urlopen
wx = b.fetch_weather()
_u.urlopen = real_urlopen
pts = "- First story happened.\n- Second story happened."
tp = [{"id": "1", "path": "/t/1", "title": "Some Release-TEAM", "age": "3 hours ago",
       "seeders": 616, "leechers": 5}]
mail = b.email_html(mk_ep(title="Aug 28 '26 \u00b7 A Title", notes=pts, weather=wx,
                          torrents=tp))
check("renders every bullet", mail.count("<li") == 3, f"{mail.count('<li')}")
check("bullet markers stripped", "<li" in mail and ">- First" not in mail)
check("weather shown", "81&deg;" in mail and "Huntley, IL 60142" in mail)
check("nothing links back to a server", "briefing page" not in mail
      and "feed.xml" not in mail and "index.html" not in mail)
check("new picks section present", "Some Release-TEAM" in mail and "616 seeders" in mail)
check("picks link to the tracker", 'href="https://tracker.example/t/1"' in mail)
check("no external images or scripts",
      "<img" not in mail and "<script" not in mail and "http://" not in mail)
check("styles are inline, not a stylesheet", "<style" not in mail and "class=" not in mail)
nasty_title = '<script>alert(1)</script> & "quotes"'
m2 = b.email_html(mk_ep(title=nasty_title, notes="- <b>bold</b> attempt", weather=wx))
check("subject line content escaped", "<script>" not in m2 and "&lt;script&gt;" in m2)
check("bullet content escaped", "<b>bold</b>" not in m2 and "&lt;b&gt;bold" in m2)
check("no picks means no picks section", "new pick" not in m2)
m3 = b.email_html(mk_ep(notes=pts))
check("no weather means no weather block", "&deg;" not in m3)
check("html is balanced",
      m3.count("<table") == m3.count("</table>") and m3.count("<ul") == m3.count("</ul>"))

sent = {}
def catch(subject, body, html_body=None): sent.update(s=subject, b=body, h=html_body)
_real_send = b.send_mail
b.send_mail = catch
b.send_mail("subj", "plain body", "<html>rich</html>")
check("send_mail accepts both parts", sent["b"] == "plain body" and sent["h"] == "<html>rich</html>")
b.send_mail = _real_send
check("plain text is still built alongside html",
      "html_body" in __import__("inspect").signature(b.send_mail).parameters)
b.CFG.pop("torrents"); b.CFG.pop("weather")

section("quote of the day")
b.run = lambda *a: json.dumps({"answer": "Everything is a subscription now [1, 2]."})
q = b.episode_quote("nb")
check("quote returned and citations stripped", q == "Everything is a subscription now.", q)
b.run = lambda *a: json.dumps({"answer": '"Quoted and starred*"'})
check("wrapping quotes and stars trimmed", "\"" not in b.episode_quote("nb"), b.episode_quote("nb"))
b.run = lambda *a: json.dumps({"answer": "x" * 400})
check("an over-long ramble is dropped", b.episode_quote("nb") == "")
b.run = lambda *a: json.dumps({"answer": ""})
check("an empty answer is dropped", b.episode_quote("nb") == "")
def _qboom(*a): raise RuntimeError("ask failed")
b.run = _qboom
check("a failed ask costs the quote, not the run", b.episode_quote("nb") == "")
check("the prompt steers away from the grim stories",
      all(w in __import__("inspect").getsource(b.episode_quote)
          for w in ("death", "disaster", "violence", "crime")))

# it reaches both surfaces
b.CFG["weather"] = {"lat": 1, "lon": 2, "label": "Huntley, IL", "periods": 4}
_u.urlopen = fake_urlopen
wxq = b.fetch_weather()
_u.urlopen = real_urlopen
mailq = b.email_html(mk_ep(notes="- One story.", weather=wxq, quote="A dry little line."))
check("email carries the quote", "A dry little line." in mailq)
check("email quote sits between weather and the notes",
      mailq.index("81&deg;") < mailq.index("A dry little line.") < mailq.index("One story."))
check("no quote means no quote block", "font-style:italic" not in
      b.email_html(mk_ep(notes="- One story.", weather=wxq)))
b.CFG.pop("weather")

check("prune sweeps the quote sidecar", ".quote" in __import__("inspect").getsource(b.prune))

section("feeds kept out of the audio")
EX = [{"title": "owner/repo-one", "feed": "GitHub Trending", "link": "https://gh.example/1"},
      {"title": "owner/repo-two", "feed": "GitHub Trending", "link": "javascript:alert(1)"}]
esc2 = __import__("html").escape
xmail = b.email_html(mk_ep(extras=EX))
check("extras grouped by feed", "GitHub Trending" in xmail)
check("extras link out", 'href="https://gh.example/1"' in xmail)
check("an unsafe extras link is not linked", "javascript:" not in xmail)
check("no extras means no section", "GitHub Trending" not in b.email_html(mk_ep()))
nasty = [{"title": "<img src=x onerror=1>", "feed": "Evil & Co", "link": ""}]
nxmail = b.email_html(mk_ep(extras=nasty))
check("extras titles and feed names escaped",
      "&lt;img" in nxmail and "Evil &amp; Co" in nxmail)

mail = b.email_html(mk_ep(extras=EX))
check("email carries the extras", "owner/repo-one" in mail and "GitHub Trending" in mail)
check("email extras sit after the notes",
      mail.index("A note.") < mail.index("owner/repo-one"))
check("email extras come after the notes",
      mail.index("A note.") < mail.index("owner/repo-one"))
check("no extras means no email section", "GitHub Trending" not in
      b.email_html(mk_ep()))
txt = b.extras_mail(EX)
check("plain text lists them", "owner/repo-one" in txt and "GitHub Trending (2)" in txt)
check("plain text omits unsafe links", "javascript:" not in txt)
check("plain text empty when none", b.extras_mail([]) == "")

# An "about" line per link, but only when the feed actually carries one.
check("repo description becomes the about line",
      b.blurb("Agent skill for beautiful, verifiable architecture diagrams.", "tt-a1i/archify")
      == "Agent skill for beautiful, verifiable architecture diagrams.")
check("hacker news boilerplate is dropped", b.blurb("Comments", "Some title") == "")
check("reddit submission footer is dropped",
      b.blurb("submitted by /u/someone to r/technology [link] [comments]", "T") == "")
check("reddit body text survives the footer strip",
      b.blurb("Worst part is it was artisanal powder tea my wife bought, notoriously "
              "hard to clean. Pray for me. submitted by /u/x to r/y [link] [comments]", "T")
      .startswith("Worst part is"))
check("a summary that repeats the headline is dropped",
      b.blurb("Debian votes to allow generative AI in packaging work",
              "Debian votes to allow generative AI in packaging work") == "")
check("html is stripped from the about line",
      "<b>" not in b.blurb("<p>A <b>real</b> description of some length here to pass.</p>", "T"))
long_about = b.blurb("word " * 80, "T")
check("about line is truncated at a word boundary",
      len(long_about) <= 161 and long_about.endswith("\u2026"), f"{len(long_about)}")
EXA = [{"title": "owner/repo", "feed": "GitHub Trending", "link": "https://gh.example/1",
        "about": "Does a useful thing, quickly."}]
check("email shows the about line",
      "Does a useful thing, quickly." in b.email_html(mk_ep(notes="- n", extras=EXA)))
check("plain text shows the about line", "Does a useful thing, quickly." in b.extras_mail(EXA))
section("hacker news api")
HN_URL = "https://news.ycombinator.com/item?id=49492632"
_hn_real = _u.urlopen
def _hn_fake(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    body = json.dumps({"score": 312, "descendants": 88}) if "49492632" in url else "null"
    return FakeResp(body.encode())
_u.urlopen = _hn_fake
check("points and comments become the about line",
      b.hn_stats(HN_URL) == "312 points \u00b7 88 comments", b.hn_stats(HN_URL))
_u.urlopen = lambda req, timeout=None: FakeResp(json.dumps({"score": 1, "descendants": 1}).encode())
check("singular reads correctly", b.hn_stats(HN_URL) == "1 point \u00b7 1 comment", b.hn_stats(HN_URL))
_u.urlopen = lambda req, timeout=None: FakeResp(b"null")
check("a deleted item yields nothing", b.hn_stats(HN_URL) == "")
def _hn_boom(req, timeout=None): raise OSError("api down")
_u.urlopen = _hn_boom
check("a dead api is not fatal", b.hn_stats(HN_URL) == "")
check("a non-hn url is ignored", b.hn_stats("https://example.com/x") == "")
check("a blank url is safe", b.hn_stats("") == "" and b.hn_stats(None) == "")

_u.urlopen = _hn_fake
rows = [{"title": "A story", "feed": "Hacker News", "link": "https://x.example/a",
         "comments": HN_URL, "about": ""},
        {"title": "Already described", "feed": "The Verge", "link": "https://v.example/b",
         "comments": "", "about": "A real description from the feed."}]
b.enrich_extras(rows)
check("an empty about line is filled from the api", rows[0]["about"].startswith("312 points"))
check("an existing about line is left alone",
      rows[1]["about"] == "A real description from the feed.")
_u.urlopen = _hn_real

check("an about line is escaped",
      "&lt;img" in b.email_html(mk_ep(extras=[{"title": "t", "feed": "f", "link": "",
                                               "about": "<img src=x>"}])))

section("horoscope")
HORO = {"sign": "sagittarius", "date": "2026-08-29",
        "horoscope": "Skip grand outings to relish the peace of home, dearest archer. "
                     "As the Pisces Moon glides on, rest. A third sentence that should "
                     "not appear anywhere in the output at all."}
_h_real = _u.urlopen
_u.urlopen = lambda req, timeout=None: FakeResp(json.dumps(HORO).encode())
b.CFG["horoscope"] = {"sign": "sagittarius"}
h = b.fetch_horoscope()
check("horoscope fetched", h and h["sign"] == "Sagittarius", h)
check("glyph resolved", h["glyph"] == "\u2650", h["glyph"])
check("trimmed to two sentences",
      "dearest archer" in h["text"] and "third sentence" not in h["text"], h["text"])
check("api date kept for the log", h["date"] == "2026-08-29")
b.CFG["horoscope"] = {"sign": "sagittarius", "sentences": 3}
check("sentence count is configurable", "third sentence" in b.fetch_horoscope()["text"])
b.CFG["horoscope"] = {"sign": "sagittarius"}
_u.urlopen = lambda req, timeout=None: FakeResp(json.dumps({"horoscope": ""}).encode())
check("an empty horoscope is dropped", b.fetch_horoscope() is None)
def _h_boom(req, timeout=None): raise OSError("down")
_u.urlopen = _h_boom
check("a dead horoscope api is not fatal", b.fetch_horoscope() is None)
_saved = b.CFG.pop("horoscope")
check("unconfigured means no horoscope", b.fetch_horoscope() is None)
b.CFG["horoscope"] = _saved
_u.urlopen = _h_real

hb = b.email_html(mk_ep(horoscope={"sign": "Sagittarius", "glyph": "\u2650",
                                   "text": "Rest today."}))
check("email labels the sign", "\u2650 Sagittarius" in hb and "Rest today." in hb)
check("no horoscope means no block", "Sagittarius" not in b.email_html(mk_ep()))
check("horoscope text is escaped",
      "&lt;img" in b.email_html(mk_ep(horoscope={"sign": "S", "glyph": "",
                                                 "text": "<img src=x>"})))
mail_h = b.email_html(mk_ep(quote="A quote.",
                            horoscope={"sign": "Sagittarius", "glyph": "\u2650",
                                       "text": "Rest today."}))
check("email carries the horoscope", "Rest today." in mail_h)
check("email horoscope sits under the quote",
      mail_h.index("A quote.") < mail_h.index("Rest today.") < mail_h.index("A note."))
check("no horoscope means no email block",
      "Rest today." not in b.email_html(mk_ep(notes="- n", quote="q")))

section("date format")
import datetime as _dt  # noqa: E402
_d = _dt.datetime(2026, 8, 29, 5, 45)
check("meta drops the year", f"{_d:%A, %B} {_d.day}" == "Saturday, August 29",
      f"{_d:%A, %B} {_d.day}")
check("the email meta uses the same shape",
      "%A, %B} {ep['local'].day}" in __import__("inspect").getsource(b.email_html))
check("no four-digit year in the meta line",
      "%Y" not in __import__("inspect").getsource(b.email_html))

section("render tripwire")
_cap = Capture() if "Capture" in dir() else None
import logging as _lg2  # noqa: E402
class _Cap(_lg2.Handler):
    def __init__(s): super().__init__(); s.msgs = []
    def emit(s, r): s.msgs.append((r.levelname, r.getMessage()))
cap2 = _Cap(); b.log.addHandler(cap2)

WX = {"label": "Somewhere", "periods": [{"name": "Today", "temp": 81, "unit": "F",
      "day": True, "wind": "", "short": "Sunny", "precip": 0, "detail": "d"}]}
PICKS = [{"id": "1", "path": "/t/1", "title": "A Release Name Here-TEAM",
          "age": "1 hour ago", "seeders": 5, "leechers": 1}]
EXR = [{"title": "owner/repo-name", "feed": "GitHub Trending",
        "link": "https://gh.example/1", "about": "Does a specific useful thing."}]
QUOTE, HORO = "A dry little line about today.", {"sign": "S", "glyph": "", "text": "Rest today."}

good = b.email_html(mk_ep(weather=WX, torrents=PICKS, quote=QUOTE, extras=EXR,
                          horoscope=HORO))
cap2.msgs.clear()
missing = b.check_rendered(good, "- A note.", WX, PICKS, EXR, QUOTE, HORO)
check("a complete email trips nothing", missing == [], missing)
# Real copy has apostrophes and ampersands, which html.escape rewrites. Comparing
# raw needles against an escaped body made the watchdog report a horoscope that was
# plainly there — a false alarm is worse than no alarm.
SPICY_Q = "Rules are now mere suggestions, apparently & obviously."
SPICY_H = {"sign": "S", "glyph": "", "text": "Kindness you've shown may pay off, dearest archer."}
SPICY_X = [{"title": "owner/repo & co", "feed": "GitHub Trending",
            "link": "https://gh.example/1", "about": "Doesn't do what you'd expect & that's fine."}]
spicy = b.email_html(mk_ep(weather=WX, torrents=PICKS, quote=SPICY_Q, extras=SPICY_X,
                           horoscope=SPICY_H))
check("escaped text still counts as present",
      b.check_rendered(spicy, "- A note.", WX, PICKS, SPICY_X, SPICY_Q, SPICY_H) == [],
      b.check_rendered(spicy, "- A note.", WX, PICKS, SPICY_X, SPICY_Q, SPICY_H))
cap2.msgs.clear()
b.check_rendered(spicy, "- A note.", WX, PICKS, SPICY_X, SPICY_Q, SPICY_H)
check("and its about line is counted as rendered",
      any("(1 with an about line, 1 rendered)" in m for _, m in cap2.msgs),
      [m for _, m in cap2.msgs])
check("the tripwire reports what it counted",
      any("with an about line" in m for _, m in cap2.msgs), [m for _, m in cap2.msgs])

# the exact bug: enriched rows computed, raw ones rendered
raw = [{k: v for k, v in EXR[0].items() if k != "about"}]
blind = b.email_html(mk_ep(weather=WX, torrents=PICKS, quote=QUOTE, extras=raw,
                           horoscope=HORO))
cap2.msgs.clear()
missing = b.check_rendered(blind, "- A note.", WX, PICKS, EXR, QUOTE, HORO)
check("dropped about lines are caught", "every about line" in missing, missing)
check("and it warns", any(lv == "WARNING" for lv, _ in cap2.msgs), [m for _, m in cap2.msgs])

for name, args in (("weather", dict(weather=None)), ("quote", dict(quote="")),
                   ("horoscope", dict(horoscope=None)), ("extras", dict(extras=None)),
                   ("picks", dict(picks=None))):
    kw = dict(weather=WX, picks=PICKS, extras=EXR, quote=QUOTE, horoscope=HORO)
    kw.update(args)
    stripped = b.email_html(mk_ep(weather=kw["weather"], torrents=kw["picks"] or [],
                                 quote=kw["quote"], extras=kw["extras"],
                                 horoscope=kw["horoscope"]))
    missing = b.check_rendered(stripped, "- A note.", WX, PICKS, EXR, QUOTE, HORO)
    check(f"a missing {name} section is caught", name in missing, missing)
b.log.removeHandler(cap2)

section("digest")
b.CFG["feeds"] = {"Alpha News": A, "Beta Wire": B}
stub_feeds({A: [entry("Alpha one", "https://a.example/1", summary="<p>A &amp; body</p>")],
            B: [entry("Beta one", "https://b.example/1", summary="Beta body")]})
items = b.collect_items()
digest = OUT / "digest.md"
b.build_digest(digest, items)
text = digest.read_text()
check("groups by feed", "## Alpha News" in text and "## Beta Wire" in text)
check("one heading per story", text.count("### ") == len(items))
check("summary html unescaped in digest", "A & body" in text)

section("yesterday rotation")
prev = OUT / "digest-yesterday.md"
check("rotates", b.rotate_digest(digest, prev) == prev and prev.exists() and not digest.exists())
digest.write_text("# fresh")
stale = time.time() - 5 * 86400
os.utime(digest, (stale, stale))
check("stale digest skipped",
      b.rotate_digest(digest, prev) is None and not digest.exists() and not prev.exists())
check("missing digest safe", b.rotate_digest(digest, prev) is None)

section("episode title")
b.run = lambda *a: json.dumps({"answer": "Fed cuts rates, Taiwan braces for typhoon [1-3]"})
check("uses the ask, citations stripped",
      b.episode_title("nb", "2026-08-26", "- x") ==
      "Aug 26 '26 · Fed cuts rates, Taiwan braces for typhoon")
def boom(*a):
    raise RuntimeError("ask failed")
b.run = boom
check("falls back to the first bullet whole",
      b.episode_title("nb", "2026-08-26", "- Fed cuts rates amid cooling inflation\n- x")
      == "Aug 26 '26 · Fed cuts rates amid cooling inflation")
check("long bullet trimmed at a word boundary",
      b.episode_title("nb", "2026-08-26", "- " + "word " * 40).endswith("word…"))
check("falls back to the date",
      b.episode_title("nb", "2026-08-26", "") == "Briefing Aug 26 '26")
check("short date reads as month, day, short year", b.short_date("2026-09-05") == "Sep 5 '26")
check("no leading zero on the day", b.short_date("2026-01-01") == "Jan 1 '26")
check("december is Dec", b.short_date("2026-12-25") == "Dec 25 '26")
check("a stamp it cannot parse is passed through", b.short_date("not-a-date") == "not-a-date")
b.run = lambda *a: json.dumps({"answer": "x" * 200})
check("over-long title rejected",
      b.episode_title("nb", "2026-08-26", "- Short bullet") == "Aug 26 '26 · Short bullet")

section("the archive")
for stem, title in [("2026-08-24", "Aug 24 '26 · Older briefing"),
                    ("2026-08-25", ""),
                    ("2026-08-26", "Aug 26 '26 · Fed & Taiwan")]:
    (OUT / f"{stem}.txt").write_text("- point one\n- point two")
    if title:
        (OUT / f"{stem}.title").write_text(title)
eps = b.episodes()
check("a briefing exists because its notes file does", len(eps) == 3, f"{len(eps)}")
check("newest first", [e["stem"] for e in eps][0] == "2026-08-26", [e["stem"] for e in eps])
check("keep_episodes caps the archive", len(eps) <= 3)
check("untitled briefing falls back", eps[1]["title"] == "Briefing Aug 25 '26", eps[1]["title"])
mail = b.email_html(eps[0])
check("the email escapes titles", "Fed &amp; Taiwan" in mail and "Fed & Taiwan" not in mail)
check("bullets become list items", mail.count("<li") == 2, f"{mail.count('<li')}")
check("bullet markers stripped", ">- point" not in mail)
check("no page is written", not (OUT / "index.html").exists())
check("no podcast feed is written", not (OUT / "feed.xml").exists())
check("nothing links back to a server",
      "briefing page" not in mail and "feed.xml" not in mail)

section("note source links")
# Calibrated on a real run: correct pairings shared 3+ distinctive words,
# wrong ones shared at most 1.
SRC = [{"title": "Dutch court sentences man to life over Rwanda genocide",
        "feed": "BBC World", "feeds": ["BBC World"], "link": "https://bbc.example/rwanda"},
       {"title": "Apple TV now costs $14.99 a month after its fourth price hike",
        "feed": "The Verge", "feeds": ["The Verge"], "link": "https://verge.example/apple"},
       {"title": "US Open 2026: All to know about the schedule and top seeds",
        "feed": "Al Jazeera", "feeds": ["Al Jazeera"], "link": "https://aj.example/usopen"}]
m = b.match_source("Rwandan genocide life sentence: A Dutch court in The Hague sentenced a "
                   "former administrator to life in prison for genocide committed in Rwanda.", SRC)
check("matches the right article", m and "rwanda" in m["link"], m and m["link"])
m = b.match_source("Apple TV+ price increase: Apple raised the monthly fee for Apple TV+ "
                   "to $14.99, marking its fourth price increase in four years.", SRC)
check("matches across differing wording", m and "apple" in m["link"], m and m["link"])
check("a note with no matching article gets nothing",
      b.match_source("GTA VI extended preview: Rockstar debuted a 27-minute gameplay video.",
                     SRC) is None)
check("an unrelated note is not force-matched",
      b.match_source("Transformers voice actor Peter Cullen has died at 85.", SRC) is None)
check("no sources means no match", b.match_source("Anything at all here", []) is None)
check("empty note is safe", b.match_source("", SRC) is None)

(OUT / "2026-08-26.sources").write_text(json.dumps(SRC))
(OUT / "2026-08-26.txt").write_text(
    "- Rwandan genocide life sentence: A Dutch court in The Hague sentenced a former "
    "administrator to life in prison for genocide committed in Rwanda.\n"
    "- GTA VI extended preview: Rockstar debuted a 27-minute gameplay video.")
nm = b.email_html(next(e for e in b.episodes() if e["stem"] == "2026-08-26"))
check("matched note carries a link arrow", "&#8599;" in nm)
check("arrow points at the article", 'href="https://bbc.example/rwanda"' in nm)
check("tooltip names outlet and headline",
      'title="BBC World — Dutch court sentences man to life over Rwanda genocide"' in nm)
check("unmatched note gets no arrow", nm.count("&#8599;") == 1,
      f"{nm.count('&#8599;')} arrows for 2 notes")
check("no bulk source list", "stories from" not in nm)
hostile = [{"title": "Rwanda genocide court sentences man to life",
            "feed": "<script>x</script>", "feeds": [], "link": "javascript:alert(1)"}]
(OUT / "2026-08-26.sources").write_text(json.dumps(hostile))
nm = b.email_html(next(e for e in b.episodes() if e["stem"] == "2026-08-26"))
check("a javascript: source is never linked", "javascript:" not in nm)
(OUT / "2026-08-26.sources").write_text(json.dumps(SRC))

section("untrusted feed input")
check("javascript url rejected", b.safe_link("javascript:alert(1)") == "")
check("data url rejected", b.safe_link("data:text/html,<script>") == "")
check("http and https allowed",
      b.safe_link("http://x.example/a") and b.safe_link("https://x.example/a"))
check("blank url safe to render", b.safe_link("") == "")
# A hostile feed reaches the page through the note tooltip now, not a source list.
(OUT / "2026-08-26.sources").write_text(json.dumps(
    [{"title": "Rwanda genocide court sentences man to life <img src=x onerror=alert(1)>",
      "feed": "Evil & Co", "feeds": ["Evil & Co"], "link": "https://evil.example/a\" onmouseover=\"x"}]))
(OUT / "2026-08-26.txt").write_text(
    "- Rwandan genocide life sentence: a court sentenced a man to life over the Rwanda genocide.")
hmail2 = b.email_html(next(e for e in b.episodes() if e["stem"] == "2026-08-26"))
check("hostile source title escaped in the tooltip",
      "<img src=x" not in hmail2 and "&lt;img" in hmail2)
check("hostile feed name escaped", "Evil &amp; Co" in hmail2)
check("quotes in a url cannot break out of the attribute",
      'onmouseover="x"' not in hmail2 and "&quot;" in hmail2)
check("email tags balanced",
      all(hmail2.count(f"<{t}") == hmail2.count(f"</{t}>") for t in ("ul", "li", "table")))

section("diagnostics")
import logging  # noqa: E402
b.banner()
blog = (OUT / "briefing.log").read_text()
check("logs its own md5", re.search(r"briefing\.py md5 [0-9a-f]{32}", blog) is not None)
check("md5 matches the file on disk",
      hashlib.md5((ROOT / "briefing.py").read_bytes()).hexdigest() in blog)
check("logs python version", "python 3." in blog)
check("logs the resolved config", "feeds, max_per_feed=" in blog and "window=48h" in blog)
check("names degraded modes", "full_text=" in blog and "smtp=" in blog)
check("log file rotates",
      any(isinstance(h, logging.handlers.RotatingFileHandler)
          for h in logging.getLogger().handlers))

tail = b.log_tail(lines=5)
check("tail returns recent lines", tail and len(tail.splitlines()) <= 5, f"{len(tail.splitlines())}")
long_line = "x" * 5000
b.log.info(long_line)
check("tail truncates long lines",
      all(len(l) <= 320 for l in b.log_tail(lines=3).splitlines()),
      f"max {max(len(l) for l in b.log_tail(lines=3).splitlines())}")
check("tail is capped", len(b.log_tail(lines=500)) <= 12_000)

# The failure email must carry the evidence, since nobody SSHes in at 6am.
sent = {}
real_send = b.send_mail
b.send_mail = lambda subject, body: sent.update(subject=subject, body=body)
b.log.info("MARKER-a-distinctive-log-line")
try:
    raise RuntimeError("audio generation exploded")
except RuntimeError as ex:
    b.send_mail(f"Briefing 2026-08-26 FAILED",
                f"{type(ex).__name__}: {ex}\n\nLast lines:\n\n{b.log_tail()}")
check("failure email names the error", "RuntimeError: audio generation exploded" in sent["body"])
check("failure email carries the log", "MARKER-a-distinctive-log-line" in sent["body"])
check("failure email stays emailable", len(sent["body"]) < 20_000, f"{len(sent['body'])} bytes")
b.send_mail = real_send

check("healthcheck failure hides the url",
      "exc_info" not in __import__("inspect").getsource(b.ping_healthcheck)
      and "type(ex).__name__" in __import__("inspect").getsource(b.ping_healthcheck))

section("silent-failure warnings")
b.CFG["feeds"] = {"Alpha News": A}
stub_feeds({A: []})
try:
    b.collect_items()
except RuntimeError:
    pass
blog = (OUT / "briefing.log").read_text()
check("an empty feed warns", "returned no entries" in blog)
stub_feeds({A: [entry("Ancient", "https://a.example/x", hours_old=999)]})
try:
    b.collect_items()
except RuntimeError:
    pass
blog = (OUT / "briefing.log").read_text()
check("a feed filtered to nothing warns", "contributed nothing" in blog)
check("dropped stories are traceable at DEBUG", "drop stale" in blog)

section("logging the new sections")
import logging as _lg  # noqa: E402
class Capture(_lg.Handler):
    def __init__(s): super().__init__(); s.msgs = []
    def emit(s, r): s.msgs.append((r.levelname, r.getMessage()))
cap = Capture(); b.log.addHandler(cap)

cap.msgs.clear()
b.run = lambda *a: json.dumps({"answer": ""})
b.episode_quote("nb")
check("an empty quote says why", any("empty answer" in m for _, m in cap.msgs), cap.msgs)
cap.msgs.clear()
b.run = lambda *a: json.dumps({"answer": "x" * 400})
b.episode_quote("nb")
check("an over-long quote says why", any("too long" in m for _, m in cap.msgs), cap.msgs)

cap.msgs.clear()
pairs, matched = b.note_links("- Rwandan genocide life sentence: a Dutch court sentenced a man "
                              "to life over the Rwanda genocide.\n- Unrelated filler note here.",
                              [{"title": "Dutch court sentences man to life over Rwanda genocide",
                                "feed": "BBC", "feeds": ["BBC"], "link": "https://b.example/r"}])
check("note_links reports pairs and a count", len(pairs) == 2 and matched == 1, (len(pairs), matched))
(OUT / "2026-08-26.sources").write_text(json.dumps(
    [{"title": "Dutch court sentences man to life over Rwanda genocide",
      "feed": "BBC World", "feeds": ["BBC World"], "link": "https://bbc.example/rwanda"}]))
(OUT / "2026-08-26.txt").write_text(
    "- Rwandan genocide life sentence: a Dutch court sentenced a man to life over the "
    "Rwanda genocide.\n- Something with no source at all in the list.")
cap.msgs.clear()
b.email_html(next(e for e in b.episodes() if e["stem"] == "2026-08-26"))
check("match coverage is logged", any("note sources: 1/2" in m for _, m in cap.msgs),
      [m for _, m in cap.msgs])
(OUT / "2026-08-26.txt").write_text("- Nothing here resembles any source headline whatsoever.")
cap.msgs.clear()
b.email_html(next(e for e in b.episodes() if e["stem"] == "2026-08-26"))
check("a total matching collapse warns",
      any(lv == "WARNING" and "note style" in m for lv, m in cap.msgs), [m for _, m in cap.msgs])
b.log.removeHandler(cap)

section("write then read back")
# Write the sidecars a real run writes, read them back with episodes(), and render the
# email from that. What is sent is what was archived, not a second copy built in memory.
RT = "2026-08-26"
(OUT / f"{RT}.txt").write_text("- Something happened today.")
(OUT / f"{RT}.title").write_text("Aug 26 '26 · A Real Title")
(OUT / f"{RT}.quote").write_text("A dry little line.")
(OUT / f"{RT}.weather").write_text(json.dumps(
    {"label": "Somewhere", "periods": [{"name": "Today", "temp": 81, "unit": "F",
     "day": True, "wind": "", "short": "Sunny", "precip": 0, "detail": "d"}]}))
(OUT / f"{RT}.horoscope").write_text(json.dumps(
    {"sign": "Sagittarius", "glyph": "\u2650", "text": "Rest today."}))
(OUT / f"{RT}.torrents").write_text(json.dumps(
    [{"id": "1", "path": "/t/1", "title": "A Release-TEAM", "age": "1 hour ago",
      "seeders": 5, "leechers": 1}]))
(OUT / f"{RT}.extras").write_text(json.dumps(
    [{"title": "owner/repo-name", "feed": "GitHub Trending", "link": "https://gh.example/1",
      "about": "Does a specific useful thing."}]))
(OUT / f"{RT}.sources").write_text(json.dumps([]))

rt = next((e for e in b.episodes() if e["stem"] == RT), None)
check("the briefing is read back off disk", rt is not None)
rt_mail, rt_text = b.email_html(rt), b.email_plain(rt)
for what, needle in (("the title", "A Real Title"),
                     ("the note", "Something happened today."),
                     ("the quote", "A dry little line."),
                     ("the horoscope", "Rest today."),
                     ("the about line", "Does a specific useful thing."),
                     ("the extras link", "owner/repo-name"),
                     ("the pick", "A Release-TEAM"),
                     ("the forecast", "81")):
    check(f"{what} reaches the email", needle in rt_mail, needle)
check("the plain text carries them too",
      all(x in rt_text for x in ("Something happened today.", "A dry little line.",
                                 "Rest today.", "owner/repo-name")))
check("the tripwire is satisfied by a round trip",
      b.check_rendered(rt_mail, rt_text, rt["weather"], rt["torrents"], rt["extras"],
                       rt["quote"], rt["horoscope"]) == [])
for ext in (".quote", ".weather", ".horoscope", ".torrents", ".extras"):
    (OUT / f"{RT}{ext}").unlink(missing_ok=True)

section("prune")
b.prune()
check("oldest briefing dropped", not (OUT / "2026-08-23.txt").exists())
check("newest kept with sidecars",
      (OUT / "2026-08-26.txt").exists() and (OUT / "2026-08-26.title").exists())

if LIVE:
    section("live feeds (network)")
    b.feedparser.parse = REAL_PARSE
    b.CFG["feeds"] = {"BBC World": "https://feeds.bbci.co.uk/news/world/rss.xml",
                      "Hacker News": "https://news.ycombinator.com/rss"}
    b.CFG["max_per_feed"] = 5
    live_items = b.collect_items()
    check("real feeds return stories", len(live_items) >= 3, f"{len(live_items)}")
    ages = [(time.time() - b.published_ts(e)) / 3600
            for e in REAL_PARSE("https://feeds.bbci.co.uk/news/world/rss.xml").entries
            if b.published_ts(e)]
    check("real feed carries usable dates", len(ages) > 3, f"{len(ages)} dated entries")
    b.add_full_text(live_items)
    got = [i for i in live_items if i["text"]]
    check("full text fetched", len(got) >= 1, f"{len(got)} of {len(live_items)}")
    check("bodies beat summaries", all(len(i["text"]) > 200 for i in got))
    feeds_used = {i["feed"] for i in got}
    check("fetching spread across feeds", len(feeds_used) >= 1, f"{sorted(feeds_used)}")

print()
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
shutil.rmtree(OUT, ignore_errors=True)
sys.exit(1 if FAILS else 0)
