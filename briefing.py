#!/usr/bin/env python3
"""Daily news briefing: RSS -> NotebookLM audio overview -> podcast feed + email."""
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

OUT = Path(os.environ.get("BRIEFING_OUT", "/data"))
CFG_PATH = Path(os.environ.get("BRIEFING_CONFIG", "/data/config.yaml"))
CFG = yaml.safe_load(CFG_PATH.read_text())
UA = "Mozilla/5.0 (compatible; briefing/1.0)"

OUT.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(OUT / "briefing.log")])
log = logging.getLogger("briefing")

# Optional: mirror logs to a syslog server (e.g. the NAS's log center).
if os.environ.get("SYSLOG_HOST"):
    _h = logging.handlers.SysLogHandler(address=(os.environ["SYSLOG_HOST"], 514))
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
        if not re.match(r"^([-*\u2022]|\d+[.)])\s", s):
            continue
        s = re.sub(r"\s*\[\d+\]", "", s).replace("\\$", "$").replace("**", "")
        lines.append(s)
    return "\n".join(lines)


def build_digest(path):
    lines, count = [], 0
    for name, url in CFG["feeds"].items():
        d = feedparser.parse(url, agent=UA)
        n = len(d.entries)
        log.info("feed %s: %d entries (http %s)", name, n, d.get("status"))
        if not n:
            continue
        lines.append(f"\n## {name}\n")
        for e in d.entries[: CFG["max_per_feed"]]:
            summary = (e.get("summary") or "").strip().replace("\n", " ")[:400]
            lines.append(f"- {e.title.strip()}" + (f" — {summary}" if summary else ""))
            count += 1
    if count == 0:
        raise RuntimeError("no headlines from any feed")
    path.write_text(f"# News digest {datetime.now():%A %d %B %Y}\n" + "\n".join(lines))
    log.info("digest: %d headlines, %d bytes", count, path.stat().st_size)
    return count


def make_episode(digest, audio_path):
    """Fresh notebook per run so there's never a stale audio artifact. Deleted after."""
    nb = jparse(run("create", f"briefing-{datetime.now():%Y-%m-%d}", "--json"))["notebook"]["id"]
    log.info("notebook %s", nb)
    try:
        run("source", "add", str(digest), "-n", nb)
        if not jparse(run("metadata", "--json", "-n", nb)).get("sources"):
            raise RuntimeError("source add reported success but notebook has no sources")

        a = CFG["audio"]
        t0 = datetime.now()
        log.info("generating audio (format=%s length=%s)", a["format"], a["length"])
        run("generate", "audio", random.choice(a["prompts"]), "-n", nb, "--format", a["format"],
            "--length", a["length"], "--wait", "--timeout", "1500", "--retry", "3")
        log.info("audio generated in %ds", (datetime.now() - t0).seconds)

        run("download", "audio", str(audio_path), "-n", nb, "--force")
        size = audio_path.stat().st_size if audio_path.exists() else 0
        if size < 100_000:
            raise RuntimeError(f"audio missing or too small ({size} bytes): {audio_path}")
        log.info("audio %s (%.1f MB)", audio_path.name, size / 1e6)

        points = run("ask", "Six bullet points, one line each, covering the most "
                     "important stories. No preamble.", "-n", nb, "--json")
        return clean(jparse(points).get("answer", ""))
    finally:
        try:
            run("delete", "-n", nb, "--yes")
        except Exception:
            log.exception("cleanup failed, notebook %s left behind", nb)


def prune():
    for f in sorted(OUT.glob("*.m4a"), reverse=True)[CFG["feed"]["keep_episodes"]:]:
        f.with_suffix(".txt").unlink(missing_ok=True)
        f.unlink()
        log.info("pruned %s", f.name)


def duration(path):
    """Seconds from the MP4 mvhd atom. Returns 0 if not found."""
    d = path.read_bytes()
    i = d.find(b"mvhd")
    if i < 0:
        return 0
    if d[i + 4] == 0:
        scale, dur = int.from_bytes(d[i+16:i+20], "big"), int.from_bytes(d[i+20:i+24], "big")
    else:
        scale, dur = int.from_bytes(d[i+24:i+28], "big"), int.from_bytes(d[i+28:i+36], "big")
    return dur // scale if scale else 0


def write_feed():
    eps = sorted(OUT.glob("*.m4a"), reverse=True)[: CFG["feed"]["keep_episodes"]]
    base = CFG["feed"]["base_url"].rstrip("/")
    items = []
    for f in eps:
        when = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
        notes = f.with_suffix(".txt")
        secs = duration(f)
        items.append(
            f"<item><title>Briefing {f.stem}</title>"
            f"<itunes:duration>{secs//3600:02d}:{secs//60%60:02d}:{secs%60:02d}</itunes:duration>"
            f"<description>{escape(notes.read_text() if notes.exists() else '')}</description>"
            f"<enclosure url='{base}/{f.name}' length='{f.stat().st_size}' type='audio/mp4'/>"
            f"<guid isPermaLink='false'>{f.stem}</guid>"
            f"<pubDate>{format_datetime(when)}</pubDate></item>")
    title = escape(CFG["feed"]["title"])
    (OUT / "feed.xml").write_text(
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<rss version='2.0' xmlns:itunes='http://www.itunes.com/dtds/podcast-1.0.dtd'>"
        f"<channel><title>{title}</title><link>{base}/</link>"
        f"<description>{title}</description><language>en-us</language>"
        f"{''.join(items)}</channel></rss>")
    log.info("feed: %d episodes", len(eps))


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
        build_digest(digest)
        points = make_episode(digest, audio)
        (OUT / f"{stamp}.txt").write_text(points)
        prune()
        write_feed()
        send_mail(f"Briefing {stamp}", f"{points}\n\n{CFG['feed']['base_url']}/{audio.name}")
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
