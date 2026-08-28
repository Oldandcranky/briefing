#!/usr/bin/env python3
"""Daily news briefing: RSS -> NotebookLM audio overview -> podcast feed + email."""
import calendar
import concurrent.futures as futures
import gzip
import hashlib
import html
import json
import logging
import logging.handlers
import os
import random
import re
import smtplib
import subprocess
import time
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

import feedparser
import yaml

try:
    import trafilatura
except ImportError:
    trafilatura = None

OUT = Path(os.environ.get("BRIEFING_OUT", "/data"))
CFG_PATH = Path(os.environ.get("BRIEFING_CONFIG", "/data/config.yaml"))
CFG = yaml.safe_load(CFG_PATH.read_text())
UA = "Mozilla/5.0 (compatible; briefing/1.0)"

WEATHER_NOTE = (" Open with the local weather outlook at the top of the digest \u2014 a"
                " sentence or two, conversational, then move into the news.")

# Appended to the audio prompt when yesterday's digest is attached as a second source.
DELTA_NOTE = (" Yesterday's digest is attached as a second source: lead with what is new or "
              "has moved since then, and don't re-tell stories that haven't changed.")

# NotebookLM's source citations: [1], [1, 2], [1-3].
CITE = re.compile(r"\s*\[\d+(?:\s*[-–,]\s*\d+)*\]")

# Words too common to help decide whether two headlines are the same story.
# Keep this to function words. A content verb like "sweep" also looked tempting
# once, but dropping real vocabulary is how unrelated stories start matching.
STOP = {"the", "a", "an", "of", "to", "in", "for", "on", "and", "as", "at", "by", "with",
        "from", "after", "over", "into", "through", "its", "is", "are", "was", "were",
        "be", "has", "have", "will", "says", "say", "said", "new", "amid", "how", "why",
        "what"}

OUT.mkdir(parents=True, exist_ok=True)
LOG_FILE = OUT / "briefing.log"
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              # Bounded: a daily DEBUG log otherwise grows without limit, and this
              # one sits in a web-served directory.
              logging.handlers.RotatingFileHandler(
                  LOG_FILE, maxBytes=2_000_000, backupCount=5)])
log = logging.getLogger("briefing")

# trafilatura logs every scored DOM node at DEBUG, which buries our own lines
# hundreds to one. Nothing below WARNING from the libraries is worth keeping.
for _noisy in ("trafilatura", "urllib3", "charset_normalizer", "httpx", "httpcore",
               "htmldate", "courlan", "justext"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# Optional: mirror logs to a syslog server (e.g. the NAS's log center).
if os.environ.get("SYSLOG_HOST"):
    _h = logging.handlers.SysLogHandler(address=(os.environ["SYSLOG_HOST"], 514))
    _h.setLevel(logging.INFO)   # the log centre wants milestones, not cli stdout dumps
    _h.setFormatter(logging.Formatter(
        f"%(asctime)s {os.environ.get('SYSLOG_TAG', 'briefing')}: %(message)s",
        datefmt="%b %d %H:%M:%S"))
    _h.formatter.converter = time.localtime
    logging.getLogger().addHandler(_h)


def run(*args):
    """Run the notebooklm CLI, log its output, raise on failure."""
    log.info("cli: %s", " ".join(args))
    p = subprocess.run(["notebooklm", *args], capture_output=True, text=True)
    if p.stdout.strip():
        log.debug("cli stdout: %s", p.stdout.strip()[:2000])
    if p.stderr.strip():
        log.info("cli stderr: %s", p.stderr.strip()[:2000])
    if p.returncode != 0:
        raise RuntimeError(f"notebooklm {' '.join(args)} exited {p.returncode}: "
                           f"{(p.stderr or p.stdout).strip()[:2000]}")
    return p.stdout


def jparse(out):
    """CLI sometimes prints a 'Matched:' line before the JSON."""
    return json.loads(out[out.index("{"):])


def clean(answer):
    """Keep only bullet lines; drops NotebookLM's trailing follow-up offers."""
    lines = []
    for ln in answer.splitlines():
        s = ln.strip()
        if not s:
            continue
        if not re.match(r"^([-*•]|\d+[.)])\s", s):
            continue
        s = re.sub(r"^([-*•]|\d+[.)])\s*", "", s)      # normalise every marker to "- "
        s = CITE.sub("", s).replace("\\$", "$").replace("**", "")
        s = re.sub(r"\*([^*]+)\*", r"\1", s)           # markdown emphasis reads as litter
        lines.append(f"- {s.strip()}")
    return "\n".join(lines)


def plain(s):
    """Feed summaries arrive as HTML fragments."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s or "")).split())


def keywords(title):
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 2 and w not in STOP}


def same_story(a, b):
    """Two headlines about one story share most of their distinctive words.

    Scored against the shorter headline, so a wire byline padded with extra words
    still matches the terse version. That ratio alone is reckless on short titles
    — "Fed cuts rates" and "Fed raises rates" share two words of three — so a few
    words have to overlap outright before the ratio gets a say.
    """
    ka, kb = keywords(a), keywords(b)
    if not ka or not kb:
        return False
    shared = len(ka & kb)
    if shared < 3:
        return ka == kb
    return shared / min(len(ka), len(kb)) >= 0.6


def canonical_url(url):
    """Drop www/query/fragment/trailing slash, so ?utm= copies collapse to one key."""
    if not url:
        return ""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return url.strip().lower()
    return f"{host}{(p.path or '').rstrip('/')}".lower()


def safe_link(url):
    """Feed contents are untrusted: only ever emit http(s) hrefs into the page."""
    try:
        return url if urlsplit(url).scheme in ("http", "https") else ""
    except ValueError:
        return ""


def item_key(item):
    """Stable identity: the canonical URL when there is one, else the flattened title."""
    u = canonical_url(item.get("link", ""))
    if u:
        return f"url:{u}"
    return "title:" + " ".join(re.sub(r"[^a-z0-9\s]", " ", item["title"].lower()).split())


def published_ts(entry):
    """Epoch seconds for a feed entry, or None when it carries no usable date."""
    for field in ("published_parsed", "updated_parsed"):
        stamp = entry.get(field)
        if stamp:
            try:
                return calendar.timegm(stamp)
            except (TypeError, ValueError):
                pass
    return None


def recent_keys(path, days):
    """Keys aired within the trailing window. One bad line must not blind the gate."""
    if not days:
        return set()
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    keys = set()
    try:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (rec.get("date") or "") >= cutoff and rec.get("key"):
                keys.add(rec["key"])
    except FileNotFoundError:
        pass
    return keys


def commit_aired(path, items, stamp, days):
    """Record what went into today's episode, dropping entries past the window."""
    if not days:
        return
    cutoff = (datetime.now() - timedelta(days=days * 2)).strftime("%Y-%m-%d")
    kept = []
    try:
        for line in path.read_text().splitlines():
            if line.strip() and (json.loads(line).get("date") or "") >= cutoff:
                kept.append(line)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    kept += [json.dumps({"date": stamp, "key": i["key"], "title": i["title"]}) for i in items]
    path.write_text("\n".join(kept) + "\n")
    log.info("ledger: recorded %d stories, %d retained", len(items), len(kept))


def collect_items(blocked=frozenset()):
    """Pull every feed, dropping stale and already-aired stories, folding duplicates
    into the first feed that ran them. Filtering happens before the per-feed cap, so
    a run of old entries can't crowd out today's news."""
    window, now = CFG.get("max_age_hours", 48), time.time()
    items, seen = [], 0
    for fidx, (name, url) in enumerate(CFG["feeds"].items()):
        d = feedparser.parse(url, agent=UA)
        kept = stale = undated = aired = 0
        for e in d.entries:
            if kept >= CFG["max_per_feed"]:
                break
            title = plain(e.get("title"))
            if not title:
                continue
            if window:
                ts = published_ts(e)
                # A missing date is not evidence of freshness — it is the usual way
                # a months-old item sneaks into a "last 48 hours" briefing.
                if ts is None:
                    undated += 1
                    log.debug("drop undated: %s", title[:90])
                    continue
                if (now - ts) / 3600 > window:
                    stale += 1
                    log.debug("drop stale (%.0fh): %s", (now - ts) / 3600, title[:90])
                    continue
            item = {"title": title, "feed": name, "feeds": [name],
                    "link": e.get("link") or "", "summary": plain(e.get("summary")),
                    "text": "", "pos": kept, "fidx": fidx}
            item["key"] = item_key(item)
            if item["key"] in blocked:
                aired += 1
                log.debug("drop already aired: %s", title[:90])
                continue
            dup = next((i for i in items
                        if i["key"] == item["key"] or same_story(i["title"], title)), None)
            if dup:
                if name not in dup["feeds"]:
                    dup["feeds"].append(name)
                seen += 1
                log.debug("fold duplicate: %s -> %s", title[:70], dup["title"][:70])
                continue
            items.append(item)
            kept += 1
        log.info("feed %s: %d entries (http %s) -> kept %d (%d stale, %d undated, %d aired)",
                 name, len(d.entries), d.get("status"), kept, stale, undated, aired)
        # A feed that returns nothing contributes silently otherwise — Reddit answers
        # 429 under load and simply vanishes from the briefing.
        if not d.entries:
            log.warning("feed %s returned no entries (http %s)%s", name, d.get("status"),
                        f": {d.bozo_exception}" if getattr(d, "bozo_exception", None) else "")
        elif not kept:
            log.warning("feed %s contributed nothing: %d stale, %d undated, %d aired",
                        name, stale, undated, aired)
    if not items:
        raise RuntimeError(
            f"no usable headlines: every entry was stale (>{window}h), undated, "
            "or already covered — check the feeds and max_age_hours")
    log.info("collected %d stories (%d duplicate headlines folded in)", len(items), seen)
    return items


def add_full_text(items):
    """Fetch article bodies for the top stories so the hosts have more than headlines."""
    cfg = CFG.get("full_text") or {}
    count, cap = cfg.get("count", 10), cfg.get("max_chars", 4000)
    if not count:
        return
    if trafilatura is None:
        log.warning("trafilatura not installed, using feed summaries only")
        return
    # Stories several feeds carried are the day's big ones. Otherwise take each feed's
    # lead story before any feed's second, so one chatty feed can't eat the whole budget.
    ranked = sorted(items, key=lambda x: (-len(x["feeds"]), x["pos"], x["fidx"]))
    queue = [i for i in ranked if i["link"]]
    # Some pages never yield an article — a Reddit comment thread has no body to extract.
    # Rather than ending up short, keep reaching down the ranking, but bounded.
    budget = cfg.get("max_attempts", count * 2)

    def grab(item):
        try:
            # favor_precision keeps nav chrome and related-link teasers out of the digest.
            body = trafilatura.extract(trafilatura.fetch_url(item["link"]),
                                       include_comments=False, include_tables=False,
                                       favor_precision=True) or ""
            return re.sub(r"^\s*[-•]?\s*Published\s*", "", body)
        except Exception:
            log.debug("extract failed: %s", item["link"], exc_info=True)
            return ""

    t0, got, attempts = datetime.now(), 0, 0
    with futures.ThreadPoolExecutor(max_workers=cfg.get("workers", 4)) as pool:
        while got < count and queue and attempts < budget:
            take = min(count - got, len(queue), budget - attempts)
            batch, queue = queue[:take], queue[take:]
            attempts += len(batch)
            for item, text in zip(batch, pool.map(grab, batch)):
                item["text"] = " ".join(text.split())[:cap]
                if item["text"]:
                    got += 1
                else:
                    log.info("no article text from %s", item["link"][:120])
    log.info("full text: %d/%d articles from %d attempts in %.1fs",
             got, count, attempts, (datetime.now() - t0).total_seconds())



# Some listing pages only answer a browser-shaped request.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")


def parse_picks(text, heading):
    """Rows of one named table on a listing page.

    The markup leaves most tags unclosed, so this walks <tr> chunks between the
    heading and the table's own </table> rather than trusting a tree parser.
    """
    low = text.lower()
    start = low.find(heading.lower())
    if start < 0:
        return []
    end = low.find("</table>", start)
    segment = text[start:end if end > 0 else None]
    rows = []
    for chunk in segment.split("<tr>"):
        link = re.search(r'href="(/t/(\d+))"[^>]*>([^<]+)</a>', chunk)
        if not link:
            continue
        age = re.search(r'<div class="sub">\(([^)]*)\)</div>', chunk)
        nums = re.findall(r"<td>(\d[\d,]*)", chunk)
        rows.append({"id": link.group(2), "path": link.group(1),
                     "title": plain(link.group(3)),
                     "age": age.group(1).strip() if age else "",
                     "seeders": int(nums[0].replace(",", "")) if nums else 0,
                     "leechers": int(nums[1].replace(",", "")) if len(nums) > 1 else 0})
    return rows


def fetch_torrents():
    """The configured listing, behind its session cookie. Never fatal."""
    cfg = CFG.get("torrents") or {}
    url, var = cfg.get("url"), cfg.get("cookie_env", "TORRENTS_COOKIE")
    if not url:
        return []
    cookie = os.environ.get(var, "").strip()
    if not cookie:
        log.warning("torrents: %s is unset, skipping the section", var)
        return []
    try:
        req = urllib.request.Request(url, headers={
            "Cookie": cookie, "User-Agent": cfg.get("user_agent", BROWSER_UA),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw, encoding = r.read(), (r.headers.get("Content-Encoding") or "").lower()
        # urllib never decompresses, and this server gzips whether or not you ask.
        if encoding == "gzip":
            raw = gzip.decompress(raw)
        body = raw.decode("utf-8", "replace")
    except Exception as ex:
        log.warning("torrents unavailable: %s", type(ex).__name__)
        return []
    picks = parse_picks(body, cfg.get("section", "Staff Picks"))
    if not picks:
        # An expired cookie returns a login page, which parses to nothing.
        log.warning("torrents: parsed no rows from %s (cookie expired, or the markup moved?)",
                    url)
    else:
        log.info("torrents: %d listed under %r", len(picks), cfg.get("section", "Staff Picks"))
    return picks


def unseen_torrents(picks, path, days):
    """Only what hasn't been listed before, so the section stays genuinely new."""
    if not picks:
        return []
    seen = set()
    cutoff = (datetime.now() - timedelta(days=days or 30)).strftime("%Y-%m-%d")
    try:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue          # one bad line must not reset the whole history
            if (rec.get("date") or "") >= cutoff and rec.get("id"):
                seen.add(rec["id"])
    except FileNotFoundError:
        pass
    fresh = [p for p in picks if p["id"] not in seen]
    log.info("torrents: %d new, %d already listed", len(fresh), len(picks) - len(fresh))
    return fresh


def remember_torrents(picks, path, stamp, days):
    """Record everything on the page, not just what was shown, and stay bounded."""
    if not picks:
        return
    cutoff = (datetime.now() - timedelta(days=(days or 30) * 2)).strftime("%Y-%m-%d")
    kept = []
    try:
        for line in path.read_text().splitlines():
            if line.strip() and (json.loads(line).get("date") or "") >= cutoff:
                kept.append(line)
    except (FileNotFoundError, json.JSONDecodeError):
        kept = []
    kept += [json.dumps({"date": stamp, "id": p["id"], "title": p["title"]}) for p in picks]
    path.write_text("\n".join(kept) + "\n")


def fetch_weather():
    """Today's outlook from the National Weather Service. No API key; US only.

    Never fatal: a missing forecast costs the weather section, not the briefing.
    """
    cfg = CFG.get("weather") or {}
    if not cfg.get("lat") or not cfg.get("lon"):
        return None

    def get(url):
        req = urllib.request.Request(url, headers={
            "User-Agent": cfg.get("user_agent", UA), "Accept": "application/geo+json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())

    try:
        pt = get(f"https://api.weather.gov/points/{cfg['lat']},{cfg['lon']}")["properties"]
        periods = get(pt["forecast"])["properties"]["periods"][: cfg.get("periods", 4)]
        where = pt["relativeLocation"]["properties"]
        out = {"label": cfg.get("label") or f"{where['city']}, {where['state']}",
               "periods": [{"name": x["name"], "temp": x["temperature"],
                            "unit": x["temperatureUnit"], "day": x["isDaytime"],
                            "wind": f"{x['windSpeed']} {x['windDirection']}".strip(),
                            "short": x["shortForecast"],
                            "precip": (x.get("probabilityOfPrecipitation") or {}).get("value") or 0,
                            "detail": x["detailedForecast"]} for x in periods]}
        now = out["periods"][0]
        log.info("weather: %s — %s, %s%s", out["label"], now["short"], now["temp"], now["unit"])
        return out
    except Exception as ex:
        log.warning("weather unavailable: %s", type(ex).__name__)
        return None


def weather_summary(w):
    """One line for the email subject area and the run log."""
    if not w:
        return ""
    p = w["periods"][0]
    bits = f"{p['short']}, {p['temp']}\u00b0{p['unit']}"
    if p["precip"]:
        bits += f", {p['precip']}% precip"
    return f"{w['label']} \u2014 {p['name'].lower()}: {bits}"


def build_digest(path, items, weather=None):
    lines = [f"# Briefing for {datetime.now():%A %d %B %Y}"]
    if weather:
        # First in the digest so the hosts open with it.
        lines.append(f"\n## Weather for {weather['label']}\n")
        for p in weather["periods"]:
            rain = f", {p['precip']}% chance of precipitation" if p["precip"] else ""
            wind = f" Wind {p['wind']}." if p["wind"] else ""
            lines.append(f"- {p['name']}: {p['short']}, {p['temp']}\u00b0{p['unit']}{rain}.{wind}")
        lines.append(f"\n{weather['periods'][0]['detail']}\n")
        lines.append("\n## News\n")
    by_feed = {}
    for i in items:
        by_feed.setdefault(i["feed"], []).append(i)
    for name, group in by_feed.items():
        lines.append(f"\n## {name}\n")
        for i in group:
            also = f" (also covered by {', '.join(i['feeds'][1:])})" if len(i["feeds"]) > 1 else ""
            body = i["text"] or i["summary"][:400]
            lines.append(f"### {i['title']}{also}\n")
            if body:
                lines.append(f"{body}\n")
    path.write_text("\n".join(lines))
    log.info("digest: %d stories, %d bytes", len(items), path.stat().st_size)


def rotate_digest(digest, prev):
    """Keep yesterday's digest so today's episode can lead with what changed."""
    prev.unlink(missing_ok=True)
    if not digest.exists():
        return None
    if time.time() - digest.stat().st_mtime > 3 * 86400:
        log.info("previous digest is stale, skipping the delta source")
        digest.unlink()
        return None
    digest.rename(prev)
    return prev


def episode_title(nb, stamp, points):
    """Headline for the episode; falls back to the first bullet, then to the bare date."""
    title = ""
    try:
        answer = jparse(run("ask", "A title for this episode: at most eight words, naming "
                            "the biggest stories. No quotes, no preamble.",
                            "-n", nb, "--json")).get("answer", "")
        title = next((ln.strip(" \"'*.#") for ln in answer.splitlines() if ln.strip()), "")
        title = CITE.sub("", title).strip(" \"'*.#")
    except Exception:
        log.warning("title ask failed", exc_info=True)
    if not title or len(title) > 80:
        first = re.sub(r"^([-*•]|\d+[.)])\s*", "", points.splitlines()[0] if points else "")
        title = first if len(first) <= 60 else first[:60].rsplit(" ", 1)[0] + "…"
    return f"{stamp} · {title}" if title else f"Briefing {stamp}"


def episode_quote(nb):
    """A wry one-liner for the email. Optional: no quote is better than a bad one.

    Steered at the absurd end of the news on purpose — a joke about the day's
    body count is not a joke, and the digest always has some of those in it.
    """
    try:
        answer = jparse(run("ask", "One dry, witty line about today's lighter or more absurd "
                            "stories - at most 25 words. Nothing about death, disaster, "
                            "violence, crime or illness. No preamble, no quotation marks.",
                            "-n", nb, "--json")).get("answer", "")
    except Exception:
        log.warning("quote ask failed", exc_info=True)
        return ""
    line = next((l.strip(" \"'*-#") for l in answer.splitlines() if l.strip()), "")
    line = CITE.sub("", line).strip()
    if not line or len(line) > 220:
        return ""
    log.info("quote: %s", line)
    return line


def make_episode(digest, prev, audio_path, stamp, weather=None):
    """Fresh notebook per run so there's never a stale audio artifact. Deleted after."""
    nb = jparse(run("create", f"briefing-{stamp}", "--json"))["notebook"]["id"]
    log.info("notebook %s", nb)
    try:
        run("source", "add", str(digest), "-n", nb)
        if prev:
            run("source", "add", str(prev), "-n", nb)
        if not jparse(run("metadata", "--json", "-n", nb)).get("sources"):
            raise RuntimeError("source add reported success but notebook has no sources")

        a = CFG["audio"]
        t0 = datetime.now()
        log.info("generating audio (format=%s length=%s)", a["format"], a["length"])
        prompt = (random.choice(a["prompts"]) + (WEATHER_NOTE if weather else "")
                  + (DELTA_NOTE if prev else ""))
        run("generate", "audio", prompt,
            "-n", nb, "--format", a["format"],
            "--length", a["length"], "--wait", "--timeout", "1500", "--retry", "3")
        log.info("audio generated in %ds", (datetime.now() - t0).seconds)

        run("download", "audio", str(audio_path), "-n", nb, "--force")
        size = audio_path.stat().st_size if audio_path.exists() else 0
        if size < 100_000:
            raise RuntimeError(f"audio missing or too small ({size} bytes): {audio_path}")
        log.info("audio %s (%.1f MB)", audio_path.name, size / 1e6)

        points = run("ask", f"{CFG.get('bullets', 12)} bullet points, one line each, covering "
                     "the most important stories. No preamble.", "-n", nb, "--json")
        points = clean(jparse(points).get("answer", ""))
        return points, episode_title(nb, stamp, points), episode_quote(nb)
    finally:
        try:
            run("delete", "-n", nb, "--yes")
        except Exception:
            log.exception("cleanup failed, notebook %s left behind", nb)


def prune():
    for f in sorted(OUT.glob("*.m4a"), reverse=True)[CFG["feed"]["keep_episodes"]:]:
        for ext in (".txt", ".title", ".sources", ".weather", ".torrents", ".quote"):
            f.with_suffix(ext).unlink(missing_ok=True)
        f.unlink()
        log.info("pruned %s", f.name)


def duration(path):
    """Seconds from the MP4 mvhd atom. Returns 0 if not found."""
    with path.open("rb") as fh:          # usually near the start; avoid slurping the whole file
        d = fh.read(2_000_000)
        i = d.find(b"mvhd")
        if i < 0:
            d = d + fh.read()
            i = d.find(b"mvhd")
    if i < 0:
        return 0
    if d[i + 4] == 0:
        scale, dur = int.from_bytes(d[i+16:i+20], "big"), int.from_bytes(d[i+20:i+24], "big")
    else:
        scale, dur = int.from_bytes(d[i+24:i+28], "big"), int.from_bytes(d[i+28:i+36], "big")
    return dur // scale if scale else 0


def episodes():
    """Newest first, capped at keep_episodes — the shared source for feed.xml and index.html."""
    out = []
    for f in sorted(OUT.glob("*.m4a"), reverse=True)[: CFG["feed"]["keep_episodes"]]:
        notes, titled, srcs = f.with_suffix(".txt"), f.with_suffix(".title"), f.with_suffix(".sources")
        wx, tor = f.with_suffix(".weather"), f.with_suffix(".torrents")
        qfile = f.with_suffix(".quote")
        secs = duration(f)
        try:
            sources = json.loads(srcs.read_text()) if srcs.exists() else []
        except json.JSONDecodeError:
            log.warning("unreadable sources sidecar: %s", srcs.name)
            sources = []
        try:
            weather = json.loads(wx.read_text()) if wx.exists() else None
        except json.JSONDecodeError:
            log.warning("unreadable weather sidecar: %s", wx.name)
            weather = None
        try:
            torrents = json.loads(tor.read_text()) if tor.exists() else []
        except json.JSONDecodeError:
            log.warning("unreadable torrents sidecar: %s", tor.name)
            torrents = []
        out.append({
            "sources": sources, "weather": weather, "torrents": torrents,
            "quote": qfile.read_text().strip() if qfile.exists() else "",
            "file": f, "size": f.stat().st_size, "secs": secs,
            "when": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc),
            # RSS wants UTC; the page should show the day the episode is named for.
            "local": datetime.fromtimestamp(f.stat().st_mtime),
            "title": titled.read_text().strip() if titled.exists() else f"Briefing {f.stem}",
            "notes": notes.read_text().strip() if notes.exists() else "",
            "clock": f"{secs//3600:02d}:{secs//60%60:02d}:{secs%60:02d}"})
    return out


def write_feed(eps):
    base = CFG["feed"]["base_url"].rstrip("/")
    items = [
        f"<item><title>{escape(e['title'])}</title>"
        f"<itunes:duration>{e['clock']}</itunes:duration>"
        f"<description>{escape(e['notes'])}</description>"
        f"<enclosure url='{base}/{e['file'].name}' length='{e['size']}' type='audio/mp4'/>"
        f"<guid isPermaLink='false'>{e['file'].stem}</guid>"
        f"<pubDate>{format_datetime(e['when'])}</pubDate></item>" for e in eps]
    title = escape(CFG["feed"]["title"])
    (OUT / "feed.xml").write_text(
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<rss version='2.0' xmlns:itunes='http://www.itunes.com/dtds/podcast-1.0.dtd'>"
        f"<channel><title>{title}</title><link>{base}/</link>"
        f"<description>{title}</description><language>en-us</language>"
        f"{''.join(items)}</channel></rss>")
    log.info("feed: %d episodes", len(eps))


def weather_block(w, esc):
    """The day's outlook, above the notes. Sidecar-driven, so old episodes keep theirs."""
    if not w:
        return ""
    now, rest = w["periods"][0], w["periods"][1:4]
    rain = f"<span class=pop>{now['precip']}% precip</span>" if now["precip"] else ""
    later = "".join(
        f"<div class=wx-next><b>{esc(p['name'])}</b>"
        f"<span>{esc(p['short'])}</span>"
        f"<em>{p['temp']}&deg;{'' if p['day'] else ' low'}</em></div>" for p in rest)
    return (f"<div class=wx><div class=wx-now>"
            f"<div class=wx-temp>{now['temp']}<sup>&deg;{esc(now['unit'])}</sup></div>"
            f"<div class=wx-what><b>{esc(now['short'])}</b>"
            f"<span>{esc(w['label'])} &middot; {esc(now['name'].lower())}"
            f"{' &middot; wind ' + esc(now['wind']) if now['wind'] else ''}</span>{rain}</div>"
            f"</div><div class=wx-strip>{later}</div></div>")


def torrents_block(picks, esc):
    """New listings only. Deliberately absent from the digest — the hosts never see this."""
    if not picks:
        return ""
    base = (CFG.get("torrents") or {}).get("link_base", "").rstrip("/")
    rows = []
    for t in picks:
        href = safe_link(base + t.get("path", "")) if base else ""
        name = esc(t["title"])
        label = f"<a href='{esc(href)}' target=_blank rel='noopener noreferrer'>{name}</a>" \
            if href else name
        meta = " &middot; ".join(x for x in (esc(t["age"]) if t["age"] else "",
                                             f"{t['seeders']} seeders" if t["seeders"] else "") if x)
        rows.append(f"<li>{label}<span class=tmeta>{meta}</span></li>")
    plural = "" if len(picks) == 1 else "s"
    return (f"<div class=torrents><h3>{len(picks)} new pick{plural}</h3>"
            f"<ul class=tlist>{''.join(rows)}</ul></div>")


def sources_block(sources, esc):
    """Every story that went into the episode, collapsed so it doesn't bury the notes."""
    if not sources:
        return ""
    by_feed = {}
    for s in sources:
        by_feed.setdefault(s.get("feed", "Other"), []).append(s)
    groups = []
    for feed, group in by_feed.items():
        rows = []
        for s in group:
            title, href = esc(s.get("title", "")), safe_link(s.get("link", ""))
            also = [f for f in s.get("feeds", [])[1:]]
            tag = f" <span class=also>also in {esc(', '.join(also))}</span>" if also else ""
            rows.append(f"<li><a href='{esc(href)}' target=_blank rel='noopener noreferrer'>"
                        f"{title}</a>{tag}</li>" if href else f"<li>{title}{tag}</li>")
        groups.append(f"<h3>{esc(feed)}</h3><ul class=src>{''.join(rows)}</ul>")
    plural = "" if len(by_feed) == 1 else "s"
    return (f"<details><summary>{len(sources)} stories from "
            f"{len(by_feed)} feed{plural}</summary>{''.join(groups)}</details>")


def write_index(eps):
    """A plain listening page served alongside feed.xml. No assets, no external requests."""
    esc = html.escape
    cards = []
    for n, e in enumerate(eps):
        points = "".join(f"<li>{esc(re.sub(r'^([-*•]|\d+[.)])\s*', '', ln))}</li>"
                         for ln in e["notes"].splitlines() if ln.strip())
        size = (f"{e['size'] / 1e6:.0f} MB" if e["size"] >= 1e6 else f"{e['size'] / 1e3:.0f} KB")
        cards.append(
            f"<article id='{esc(e['file'].stem)}'>"
            f"<h2>{esc(e['title'])}</h2>"
            f"<p class=meta>{e['local']:%A %d %B %Y} · {e['clock']} · {size}</p>"
            f"<audio controls preload=none src='{esc(e['file'].name)}'></audio>"
            + weather_block(e.get("weather"), esc)
            + (f"<p class=quote>{esc(e['quote'])}</p>" if e.get("quote") else "")
            + (f"<ul>{points}</ul>" if points else "")
            + sources_block(e["sources"], esc)
            + torrents_block(e.get("torrents"), esc)
            + f"<p class=dl><a href='{esc(e['file'].name)}'>Download m4a</a></p></article>")
    (OUT / "index.html").write_text(f"""<!doctype html>
<html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{esc(CFG['feed']['title'])}</title>
<style>
:root {{ color-scheme: light dark; --bg:#fbfaf8; --fg:#1c1b19; --dim:#6b6862;
        --card:#fff; --line:#e6e2db; --accent:#8a5a2b; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#161513; --fg:#eceae6; --dim:#9a958c; --card:#1f1e1b;
           --line:#302e2a; --accent:#d9a45b; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0 auto; padding:2.5rem 1.25rem 4rem; max-width:44rem; background:var(--bg);
       color:var(--fg); font:16px/1.6 ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif; }}
header {{ border-bottom:1px solid var(--line); padding-bottom:1.25rem; margin-bottom:2rem; }}
h1 {{ font-size:1.6rem; margin:0 0 .35rem; letter-spacing:-.01em; }}
header p {{ margin:0; color:var(--dim); font-size:.9rem; }}
a {{ color:var(--accent); }}
article {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:1.25rem 1.35rem; margin-bottom:1.1rem; scroll-margin-top:1rem; }}
article:target {{ border-color:var(--accent); }}
h2 {{ font-size:1.08rem; margin:0 0 .3rem; letter-spacing:-.01em; }}
.meta {{ margin:0 0 .9rem; color:var(--dim); font-size:.82rem; }}
.quote {{ margin:1.1rem 0 0; padding:.15rem 0 .15rem 1rem;
         border-left:3px solid var(--accent); font-style:italic; color:var(--fg);
         font-size:.95rem; line-height:1.5; }}
.torrents {{ margin-top:1.1rem; padding-top:.9rem; border-top:1px solid var(--line); }}
.torrents h3 {{ margin:0 0 .5rem; font-size:.72rem; letter-spacing:.09em;
               text-transform:uppercase; color:var(--dim); font-weight:600; }}
.tlist {{ list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:.4rem; }}
.tlist li {{ display:flex; flex-direction:column; gap:.1rem; font-size:.86rem;
            word-break:break-word; }}
.tmeta {{ font-size:.72rem; color:var(--dim); }}
.wx {{ margin:1rem 0 0; border:1px solid var(--line); border-radius:8px; overflow:hidden; }}
.wx-now {{ display:flex; align-items:center; gap:1rem; padding:.9rem 1.1rem; }}
.wx-temp {{ font-size:2.1rem; font-weight:600; line-height:1; letter-spacing:-.02em; }}
.wx-temp sup {{ font-size:.9rem; font-weight:500; top:-.7em; }}
.wx-what {{ display:flex; flex-direction:column; gap:.15rem; }}
.wx-what b {{ font-size:1rem; }}
.wx-what span {{ font-size:.8rem; color:var(--dim); }}
.pop {{ font-size:.75rem; color:var(--accent); }}
.wx-strip {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(90px,1fr));
            border-top:1px solid var(--line); }}
.wx-next {{ padding:.6rem .8rem; display:flex; flex-direction:column; gap:.1rem;
           border-right:1px solid var(--line); }}
.wx-next:last-child {{ border-right:0; }}
.wx-next b {{ font-size:.76rem; }}
.wx-next span {{ font-size:.72rem; color:var(--dim); }}
.wx-next em {{ font-size:.78rem; font-style:normal; color:var(--fg); }}
audio {{ width:100%; height:38px; }}
ul {{ margin:1rem 0 0; padding-left:1.15rem; }}
li {{ margin:.3rem 0; }}
.dl {{ margin:.9rem 0 0; font-size:.82rem; }}
.dl a {{ color:var(--dim); }}
details {{ margin-top:1rem; border-top:1px solid var(--line); padding-top:.85rem; }}
summary {{ cursor:pointer; color:var(--dim); font-size:.82rem; }}
summary:hover {{ color:var(--fg); }}
details h3 {{ font-size:.78rem; text-transform:uppercase; letter-spacing:.06em;
             color:var(--dim); margin:1rem 0 .4rem; font-weight:600; }}
ul.src {{ margin:0; padding-left:1.15rem; font-size:.9rem; }}
ul.src li {{ margin:.25rem 0; }}
ul.src a {{ text-decoration:none; }}
ul.src a:hover {{ text-decoration:underline; }}
.also {{ color:var(--dim); font-size:.8rem; }}
</style>
<header>
<h1>{esc(CFG['feed']['title'])}</h1>
<p>{len(eps)} episode{'s' if len(eps) != 1 else ''} ·
<a href="feed.xml">Subscribe via RSS</a> — paste this page's URL + <code>/feed.xml</code>
into any podcast app.</p>
</header>
{''.join(cards) or '<p>No episodes yet.</p>'}
</html>
""")
    log.info("index: %d episodes", len(eps))


def email_html(title, points, weather, picks, link, quote=""):
    """HTML email. Inline styles and tables only — mail clients ignore stylesheets,
    and several strip <style> outright. No images, so nothing is blocked or tracked.
    """
    esc = html.escape
    ink, dim, line = "#1c1b19", "#6b6862", "#e6e2db"
    accent, card, page = "#8a5a2b", "#ffffff", "#f4f2ee"

    wx = ""
    if weather:
        now = weather["periods"][0]
        rain = (f'<span style="color:{accent};font-size:13px"> &middot; '
                f'{now["precip"]}% precip</span>' if now["precip"] else "")
        nxt = "".join(
            f'<td width="33%" style="width:33.3%;padding:8px 10px;border-left:1px solid {line};'
            f'vertical-align:top">'
            f'<div style="font-size:12px;font-weight:600;color:{ink}">{esc(p["name"])}</div>'
            f'<div style="font-size:12px;color:{dim};padding-top:2px">{esc(p["short"])}</div>'
            f'<div style="font-size:12px;color:{ink};padding-top:2px">{p["temp"]}&deg;</div>'
            f'</td>' for p in weather["periods"][1:4])
        wx = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
              f'style="border:1px solid {line};border-radius:8px;margin:0 0 22px">'
              f'<tr><td style="padding:14px 16px">'
              f'<span style="font-size:30px;font-weight:600;color:{ink}">{now["temp"]}&deg;</span>'
              f'<span style="font-size:15px;font-weight:600;color:{ink};padding-left:10px">'
              f'{esc(now["short"])}</span>'
              f'<div style="font-size:13px;color:{dim};padding-top:3px">'
              f'{esc(weather["label"])} &middot; {esc(now["name"].lower())}'
              f'{" &middot; wind " + esc(now["wind"]) if now["wind"] else ""}{rain}</div>'
              f'</td></tr><tr><td style="padding:0"><table role="presentation" width="100%" '
              f'cellpadding="0" cellspacing="0" style="border-top:1px solid {line}">'
              f'<tr>{nxt}</tr></table></td></tr></table>')

    bullets = "".join(
        f'<li style="margin:0 0 9px;line-height:1.55">{esc(re.sub(r"^-\s*", "", ln))}</li>'
        for ln in points.splitlines() if ln.strip())

    tor = ""
    if picks:
        base = (CFG.get("torrents") or {}).get("link_base", "").rstrip("/")
        rows = []
        for t in picks:
            href = safe_link(base + t.get("path", "")) if base else ""
            name = esc(t["title"])
            label = (f'<a href="{esc(href)}" style="color:{ink};text-decoration:none">{name}</a>'
                     if href else name)
            meta = " &middot; ".join(x for x in (esc(t["age"]),
                                    f'{t["seeders"]} seeders' if t["seeders"] else "") if x)
            rows.append(f'<li style="margin:0 0 8px;line-height:1.4">{label}'
                        f'<div style="font-size:12px;color:{dim}">{meta}</div></li>')
        tor = (f'<div style="border:1px solid {line};border-radius:8px;padding:14px 16px;'
               f'margin:0 0 22px">'
               f'<div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;'
               f'color:{dim};font-weight:600;padding-bottom:10px">'
               f'{len(picks)} new pick{"" if len(picks) == 1 else "s"}</div>'
               f'<ul style="margin:0;padding-left:18px;font-size:14px;color:{ink}">'
               f'{"".join(rows)}</ul></div>')

    quoted = ""
    if quote:
        quoted = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                  f'style="margin:0 0 22px"><tr>'
                  f'<td style="border-left:3px solid {accent};padding:2px 0 2px 14px;'
                  f'font-size:15px;font-style:italic;line-height:1.5;color:{ink}">'
                  f'{esc(quote)}</td></tr></table>')

    return (f'<!doctype html><html><body style="margin:0;padding:0;background:{page}">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="background:{page};padding:22px 12px">'
            f'<tr><td align="center">'
            f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
            f'style="max-width:600px;width:100%;background:{card};border:1px solid {line};'
            f'border-radius:10px">'
            f'<tr><td style="padding:26px 26px 22px;'
            f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,'
            f'sans-serif;color:{ink}">'
            f'<div style="font-size:19px;font-weight:600;line-height:1.3;padding-bottom:18px">'
            f'{esc(title)}</div>'
            f'{tor}'
            f'{wx}'
            f'{quoted}'
            f'<ul style="margin:0;padding-left:19px;font-size:15px;color:{ink}">{bullets}</ul>'
            f'<div style="padding-top:24px">'
            f'<a href="{esc(link)}" style="background:{accent};color:#fff;text-decoration:none;'
            f'font-size:14px;font-weight:600;padding:11px 22px;border-radius:6px;'
            f'display:inline-block">Listen to the episode</a></div>'
            f'</td></tr></table></td></tr></table></body></html>')


def send_mail(subject, body, html_body=None):
    pw = os.environ.get("SMTP_PASSWORD")
    if not pw:
        log.warning("SMTP_PASSWORD unset, skipping email")
        return
    e = CFG["email"]
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = subject, e["from"], e["to"]
    m.set_content(body)
    if html_body:
        m.add_alternative(html_body, subtype="html")
    try:
        with smtplib.SMTP(e["smtp_host"], e["smtp_port"], timeout=30) as s:
            s.starttls()
            s.login(e["from"], pw.strip())
            s.send_message(m)
        log.info("email sent to %s", e["to"])
    except Exception:
        log.exception("email failed")


def banner():
    """What is actually running. First thing to check when a run misbehaves.

    The md5 is of this very file, so it can be compared directly against what
    deploy.sh recorded on the host — the question "is the box running the code I
    think it is?" should never need an investigation.
    """
    try:
        version = hashlib.md5(Path(__file__).read_bytes()).hexdigest()
    except OSError:
        version = "unknown"
    log.info("briefing.py md5 %s | python %s", version, sys.version.split()[0])
    try:
        p = subprocess.run(["notebooklm", "--version"], capture_output=True,
                           text=True, timeout=60)
        log.info("notebooklm cli: %s", ((p.stdout or p.stderr).strip() or "no version")[:120])
    except Exception as ex:
        log.warning("notebooklm cli not runnable: %s", type(ex).__name__)
    a = CFG["audio"]
    log.info("config %s: %d feeds, max_per_feed=%s, window=%sh, ledger=%sd, bullets=%s, "
             "audio=%s/%s, keep=%s", CFG_PATH, len(CFG["feeds"]), CFG["max_per_feed"],
             CFG.get("max_age_hours", 48), CFG.get("ledger_days", 7), CFG.get("bullets", 12),
             a["format"], a["length"], CFG["feed"]["keep_episodes"])
    # Degraded modes are silent by design elsewhere; say them out loud here.
    log.info("full_text=%s | smtp=%s | healthcheck=%s | syslog=%s",
             f"on (trafilatura {trafilatura.__version__})" if trafilatura else "OFF (trafilatura missing)",
             "on" if os.environ.get("SMTP_PASSWORD") else "OFF (no email will be sent)",
             "on" if os.environ.get("HEALTHCHECK_URL") else "off",
             os.environ.get("SYSLOG_HOST") or "off")


def log_tail(lines=50, per_line=300, cap=12_000):
    """Recent log lines for the failure email, so a 6am post-mortem needs no SSH."""
    try:
        text = LOG_FILE.read_text(errors="replace").splitlines()
    except OSError as ex:
        return f"(could not read {LOG_FILE}: {type(ex).__name__})"
    out = [ln[:per_line] + ("…" if len(ln) > per_line else "") for ln in text[-lines:]]
    joined = "\n".join(out)
    return joined[-cap:] if len(joined) > cap else joined


def torrents_mail(picks):
    """Plain-text tail for the email; empty when nothing new turned up."""
    if not picks:
        return ""
    base = (CFG.get("torrents") or {}).get("link_base", "").rstrip("/")
    lines = [f"\n\nNew picks ({len(picks)}):"]
    for t in picks:
        meta = " / ".join(x for x in (t["age"], f"{t['seeders']} seeders" if t["seeders"] else "") if x)
        lines.append(f"  {t['title']}" + (f"  [{meta}]" if meta else ""))
        if base and t.get("path"):
            lines.append(f"    {base}{t['path']}")
    return "\n".join(lines)


def episode_link(stamp):
    """The listening page, anchored at this episode — not a 60MB download link."""
    return f"{CFG['feed']['base_url'].rstrip('/')}/#{stamp}"


def ping_healthcheck(ok):
    """Dead-man's switch (e.g. healthchecks.io): the monitor alerts when pings stop."""
    url = os.environ.get("HEALTHCHECK_URL")
    if not url:
        return
    try:
        urllib.request.urlopen(url if ok else url.rstrip("/") + "/fail", timeout=10)
        log.info("healthcheck pinged (%s)", "ok" if ok else "fail")
    except Exception as ex:
        # No traceback: it would print the ping URL, and this log is web-served.
        log.warning("healthcheck ping failed: %s", type(ex).__name__)


def main():
    stamp = f"{datetime.now():%Y-%m-%d}"
    audio, digest = OUT / f"{stamp}.m4a", OUT / "digest.md"
    started = datetime.now()
    log.info("=== run start %s ===", stamp)
    banner()
    ledger, days = OUT / "aired.jsonl", CFG.get("ledger_days", 7)
    try:
        prev = rotate_digest(digest, OUT / "digest-yesterday.md")
        blocked = recent_keys(ledger, days)
        log.info("ledger: %d stories blocked from the last %sd", len(blocked), days)
        weather = fetch_weather()
        tcfg = CFG.get("torrents") or {}
        tledger, tdays = OUT / "torrents-seen.jsonl", tcfg.get("history_days", 30)
        picks = fetch_torrents()
        fresh_picks = unseen_torrents(picks, tledger, tdays)[: tcfg.get("max_shown", 15)]
        items = collect_items(blocked)
        add_full_text(items)
        build_digest(digest, items, weather)
        points, title, quote = make_episode(digest, prev, audio, stamp, weather)
        (OUT / f"{stamp}.txt").write_text(points)
        (OUT / f"{stamp}.title").write_text(title)
        if quote:
            (OUT / f"{stamp}.quote").write_text(quote)
        if weather:
            (OUT / f"{stamp}.weather").write_text(json.dumps(weather))
        if fresh_picks:
            (OUT / f"{stamp}.torrents").write_text(json.dumps(fresh_picks))
        remember_torrents(picks, tledger, stamp, tdays)
        (OUT / f"{stamp}.sources").write_text(json.dumps(
            [{"title": i["title"], "feed": i["feed"], "feeds": i["feeds"], "link": i["link"]}
             for i in items]))
        # Only a finished episode counts as aired, so a failed run doesn't burn stories.
        commit_aired(ledger, items, stamp, days)
        prune()
        eps = episodes()
        write_feed(eps)
        write_index(eps)
        wx_line, link = weather_summary(weather), episode_link(stamp)
        send_mail(title,
                  torrents_mail(fresh_picks).lstrip("\n")
                  + (f"\n\n{wx_line}" if wx_line else "")
                  + (f"\n\n{quote}" if quote else "")
                  + f"\n\n{points}\n\nListen: {link}",
                  email_html(title, points, weather, fresh_picks, link, quote))
        mins = (datetime.now() - started).total_seconds() / 60
        log.info("=== run ok in %.1f min: %d stories, %d with article text, "
                 "%d bullets, %s (%.1f MB, %s) ===",
                 mins, len(items), sum(1 for i in items if i["text"]),
                 len(points.splitlines()), audio.name, audio.stat().st_size / 1e6,
                 next((e["clock"] for e in eps if e["file"] == audio), "?"))
        ping_healthcheck(ok=True)
    except Exception as ex:
        mins = (datetime.now() - started).total_seconds() / 60
        log.exception("=== run FAILED after %.1f min ===", mins)
        # Carry the evidence into the email: the point of failure at 6am is that
        # nobody is going to SSH in to read a log.
        send_mail(f"Briefing {stamp} FAILED",
                  f"{type(ex).__name__}: {ex}\n\n"
                  f"Failed after {mins:.1f} minutes. Last lines of {LOG_FILE}:\n\n"
                  f"{log_tail()}\n")
        ping_healthcheck(ok=False)
        sys.exit(1)


if __name__ == "__main__":
    main()
