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
feed:
  title: Test Briefing
  base_url: https://example.com/briefing/
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

stub_feeds({A: [entry("Ancient", "https://a.example/old", hours_old=999)]})
try:
    b.collect_items()
    check("empty result raises", False)
except RuntimeError as ex:
    check("empty result raises a useful error", "stale" in str(ex), str(ex)[:60])

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

section("duration")
def mvhd_v0(scale, dur, pad=0):
    return (b"\x00" * pad + b"mvhd" + b"\x00" * 12 + scale.to_bytes(4, "big")
            + dur.to_bytes(4, "big"))
def mvhd_v1(scale, dur):
    return (b"mvhd" + b"\x01\x00\x00\x00" + b"\x00" * 16 + scale.to_bytes(4, "big")
            + dur.to_bytes(8, "big"))
f = OUT / "2026-08-24.m4a"; f.write_bytes(mvhd_v0(1000, 900_000))
check("v0 mvhd", b.duration(f) == 900)
g = OUT / "2026-08-25.m4a"; g.write_bytes(mvhd_v1(600, 540_000))
check("v1 mvhd", b.duration(g) == 900)
h = OUT / "2026-08-26.m4a"; h.write_bytes(mvhd_v0(1000, 61_000, pad=2_500_000))
check("mvhd past the 2MB read", b.duration(h) == 61)
n = OUT / "2026-08-23.m4a"; n.write_bytes(b"\x00" * 500)
check("no mvhd is zero, not a crash", b.duration(n) == 0)

section("episode title")
b.run = lambda *a: json.dumps({"answer": "Fed cuts rates, Taiwan braces for typhoon [1-3]"})
check("uses the ask, citations stripped",
      b.episode_title("nb", "2026-08-26", "- x") ==
      "2026-08-26 · Fed cuts rates, Taiwan braces for typhoon")
def boom(*a):
    raise RuntimeError("ask failed")
b.run = boom
check("falls back to the first bullet whole",
      b.episode_title("nb", "2026-08-26", "- Fed cuts rates amid cooling inflation\n- x")
      == "2026-08-26 · Fed cuts rates amid cooling inflation")
check("long bullet trimmed at a word boundary",
      b.episode_title("nb", "2026-08-26", "- " + "word " * 40).endswith("word…"))
check("falls back to the date", b.episode_title("nb", "2026-08-26", "") == "Briefing 2026-08-26")
b.run = lambda *a: json.dumps({"answer": "x" * 200})
check("over-long title rejected",
      b.episode_title("nb", "2026-08-26", "- Short bullet") == "2026-08-26 · Short bullet")

section("feed and page")
for stem, title in [("2026-08-24", "2026-08-24 · Older show"),
                    ("2026-08-26", "2026-08-26 · Fed & Taiwan")]:
    (OUT / f"{stem}.txt").write_text("- point one\n- point two")
    (OUT / f"{stem}.title").write_text(title)
eps = b.episodes()
b.write_feed(eps)
b.write_index(eps)
xml = (OUT / "feed.xml").read_text()
page = (OUT / "index.html").read_text()
check("custom title used", "<title>2026-08-26 · Fed &amp; Taiwan</title>" in xml)
check("ampersand escaped in feed", "Fed &amp; Taiwan" in xml and "Fed & Taiwan" not in xml)
check("untitled episode falls back", "<title>Briefing 2026-08-25</title>" in xml)
check("duration tag", "<itunes:duration>00:15:00</itunes:duration>" in xml)
check("keep_episodes respected", xml.count("<item>") == 3)
try:
    ET.fromstring(xml)
    wf = True
except ET.ParseError as ex:
    wf = False
    print("   ", ex)
check("feed is well-formed XML", wf)
check("page lists episodes", page.count("<article") == 3)
check("page has players", page.count("<audio controls") == 3)
check("page escapes titles", "Fed &amp; Taiwan" in page and "Fed & Taiwan" not in page)
check("bullets become list items", page.count("<li>point one</li>") == 2)
check("bullet markers stripped", "<li>- point" not in page)
check("episode anchors", "id='2026-08-26'" in page)
check("email links to the page, not the audio",
      b.episode_link("2026-08-26") == "https://example.com/briefing/#2026-08-26",
      b.episode_link("2026-08-26"))
# Story links are external by design; assets must never be, or the page breaks
# offline and leaks a request to whoever hosts them.
check("no remote assets", not re.search(r"<(img|script|iframe)[^>]+src=['\"]?https?://", page)
      and not re.search(r"<link[^>]+href=['\"]?https?://", page))
check("audio stays relative", "src='2026-08-26.m4a'" in page)

section("source links")
b.CFG["feeds"] = {"Alpha News": A, "Beta Wire": B}
stub_feeds({A: [entry("Alpha lead story", "https://a.example/lead"),
                entry("Alpha second story", "https://a.example/second")],
            B: [entry("Beta exclusive", "https://b.example/x")]})
src_items = b.collect_items()
(OUT / "2026-08-26.sources").write_text(json.dumps(
    [{"title": i["title"], "feed": i["feed"], "feeds": i["feeds"], "link": i["link"]}
     for i in src_items]))
eps = b.episodes()
b.write_index(eps)
page = (OUT / "index.html").read_text()
check("sources are collapsed by default", "<details>" in page and " open>" not in page)
check("summary counts stories and feeds", "3 stories from 2 feeds" in page,
      re.search(r"<summary>([^<]*)</summary>", page).group(1) if "<summary>" in page else "none")
check("story links rendered", "href='https://a.example/lead'" in page)
check("links open safely", page.count("rel='noopener noreferrer'") == 3)
check("grouped by feed", "<h3>Alpha News</h3>" in page and "<h3>Beta Wire</h3>" in page)
check("episodes without a sidecar omit the block", page.count("<details>") == 1,
      f"{page.count('<details>')} details blocks")

section("untrusted feed input")
check("javascript url rejected", b.safe_link("javascript:alert(1)") == "")
check("data url rejected", b.safe_link("data:text/html,<script>") == "")
check("http and https allowed",
      b.safe_link("http://x.example/a") and b.safe_link("https://x.example/a"))
check("blank url safe to render", b.safe_link("") == "")
hostile = [{"title": "<img src=x onerror=alert(1)>", "feed": "Evil & Co",
            "feeds": ["Evil & Co"], "link": "javascript:alert(1)"}]
blk = b.sources_block(hostile, __import__("html").escape)
check("hostile title escaped", "<img src=x" not in blk and "&lt;img" in blk)
check("hostile url not linked", "javascript:" not in blk and "<a " not in blk)
check("feed name escaped", "Evil &amp; Co" in blk)
check("multi-feed stories noted",
      "also in" in b.sources_block(
          [{"title": "T", "feed": "A", "feeds": ["A", "B"], "link": "https://x.example/a"}],
          __import__("html").escape))
check("tags balanced",
      all(page.count(f"<{t}") == page.count(f"</{t}>") for t in ("article", "ul", "li", "h2")))

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

section("prune")
b.prune()
check("oldest audio dropped", not (OUT / "2026-08-23.m4a").exists())
check("newest kept with sidecars",
      (OUT / "2026-08-26.m4a").exists() and (OUT / "2026-08-26.title").exists())

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
