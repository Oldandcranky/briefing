#!/usr/bin/env python3
"""Daily news briefing: RSS -> NotebookLM audio overview -> podcast feed + email."""
import concurrent.futures as futures
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
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
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

# Appended to the audio prompt when yesterday's digest is attached as a second source.
DELTA_NOTE = (" Yesterday's digest is attached as a second source: lead with what is new or "
              "has moved since then, and don't re-tell stories that haven't changed.")

# NotebookLM's source citations: [1], [1, 2], [1-3].
CITE = re.compile(r"\s*\[\d+(?:\s*[-–,]\s*\d+)*\]")

# Words too common to help decide whether two headlines are the same story.
STOP = {"the", "a", "an", "of", "to", "in", "for", "on", "and", "as", "at", "by", "with",
        "from", "after", "over", "into", "its", "is", "are", "was", "were", "be", "has",
        "have", "will", "says", "say", "said", "new", "amid", "how", "why", "what"}

OUT.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(OUT / "briefing.log")])
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
    """Two headlines about one story share most of their distinctive words."""
    ka, kb = keywords(a), keywords(b)
    if not ka or not kb:
        return False
    return len(ka & kb) / min(len(ka), len(kb)) >= 0.6


def collect_items():
    """Pull every feed, folding near-duplicate stories into the first feed that ran them."""
    items = []
    for fidx, (name, url) in enumerate(CFG["feeds"].items()):
        d = feedparser.parse(url, agent=UA)
        log.info("feed %s: %d entries (http %s)", name, len(d.entries), d.get("status"))
        for pos, e in enumerate(d.entries[: CFG["max_per_feed"]]):
            title = plain(e.get("title"))
            if not title:
                continue
            dup = next((i for i in items if same_story(i["title"], title)), None)
            if dup:
                if name not in dup["feeds"]:
                    dup["feeds"].append(name)
                continue
            items.append({"title": title, "feed": name, "feeds": [name],
                          "link": e.get("link") or "", "summary": plain(e.get("summary")),
                          "text": "", "pos": pos, "fidx": fidx})
    if not items:
        raise RuntimeError("no headlines from any feed")
    dupes = sum(len(i["feeds"]) - 1 for i in items)
    log.info("collected %d stories (%d duplicate headlines folded in)", len(items), dupes)
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
    targets = [i for i in ranked if i["link"]][:count]

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

    t0 = datetime.now()
    with futures.ThreadPoolExecutor(max_workers=cfg.get("workers", 4)) as pool:
        for item, text in zip(targets, pool.map(grab, targets)):
            item["text"] = " ".join(text.split())[:cap]
    log.info("full text: %d/%d articles in %ds", sum(1 for i in targets if i["text"]),
             len(targets), (datetime.now() - t0).seconds)


def build_digest(path, items):
    lines = [f"# News digest {datetime.now():%A %d %B %Y}"]
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


def make_episode(digest, prev, audio_path, stamp):
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
        run("generate", "audio", random.choice(a["prompts"]) + (DELTA_NOTE if prev else ""),
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
        return points, episode_title(nb, stamp, points)
    finally:
        try:
            run("delete", "-n", nb, "--yes")
        except Exception:
            log.exception("cleanup failed, notebook %s left behind", nb)


def prune():
    for f in sorted(OUT.glob("*.m4a"), reverse=True)[CFG["feed"]["keep_episodes"]:]:
        f.with_suffix(".txt").unlink(missing_ok=True)
        f.with_suffix(".title").unlink(missing_ok=True)
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
        notes, titled = f.with_suffix(".txt"), f.with_suffix(".title")
        secs = duration(f)
        out.append({
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
            + (f"<ul>{points}</ul>" if points else "")
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
audio {{ width:100%; height:38px; }}
ul {{ margin:1rem 0 0; padding-left:1.15rem; }}
li {{ margin:.3rem 0; }}
.dl {{ margin:.9rem 0 0; font-size:.82rem; }}
.dl a {{ color:var(--dim); }}
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


def send_mail(subject, body):
    pw = os.environ.get("SMTP_PASSWORD")
    if not pw:
        log.warning("SMTP_PASSWORD unset, skipping email")
        return
    e = CFG["email"]
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = subject, e["from"], e["to"]
    m.set_content(body)
    try:
        with smtplib.SMTP(e["smtp_host"], e["smtp_port"], timeout=30) as s:
            s.starttls()
            s.login(e["from"], pw.strip())
            s.send_message(m)
        log.info("email sent to %s", e["to"])
    except Exception:
        log.exception("email failed")


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
    except Exception:
        log.warning("healthcheck ping failed", exc_info=True)


def main():
    stamp = f"{datetime.now():%Y-%m-%d}"
    audio, digest = OUT / f"{stamp}.m4a", OUT / "digest.md"
    log.info("=== run start %s (config %s) ===", stamp, CFG_PATH)
    try:
        prev = rotate_digest(digest, OUT / "digest-yesterday.md")
        items = collect_items()
        add_full_text(items)
        build_digest(digest, items)
        points, title = make_episode(digest, prev, audio, stamp)
        (OUT / f"{stamp}.txt").write_text(points)
        (OUT / f"{stamp}.title").write_text(title)
        prune()
        eps = episodes()
        write_feed(eps)
        write_index(eps)
        send_mail(title, f"{points}\n\nListen: {episode_link(stamp)}")
        log.info("=== run ok ===")
        ping_healthcheck(ok=True)
    except Exception as ex:
        log.exception("=== run FAILED ===")
        send_mail(f"Briefing {stamp} FAILED", f"{type(ex).__name__}: {ex}\n\n"
                  f"Full log: {OUT / 'briefing.log'}")
        ping_healthcheck(ok=False)
        sys.exit(1)


if __name__ == "__main__":
    main()
