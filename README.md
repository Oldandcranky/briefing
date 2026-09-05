# Daily briefing

[![CI](https://github.com/Oldandcranky/briefing/actions/workflows/ci.yml/badge.svg)](https://github.com/Oldandcranky/briefing/actions/workflows/ci.yml)

A self-hosted daily news briefing: pulls headlines from RSS feeds, fetches the
articles behind them, has [NotebookLM](https://notebooklm.google.com) read the lot
and write it up, and emails it to you. One email a morning; nothing to host, nothing
to log in to.

Each run:

1. Fetches the local forecast, which opens the email.
2. Pulls the configured RSS feeds, drops anything older than `max_age_hours`
   (and anything undated), skips stories already aired in the last
   `ledger_days`, and folds near-duplicate headlines together — so a story
   three outlets ran gets covered once, and is treated as one of the day's big
   ones.
3. Fetches the full article text for the top stories (not just the RSS blurb)
   so the write-up has something to work from. Pages that extract to
   nothing — a Reddit comment thread has no article body — are replaced by
   reaching further down the ranking, up to `full_text.max_attempts`.
4. Builds a markdown digest, keeping yesterday's alongside it as a second
   source so the write-up leads with what has *changed*.
5. Creates a fresh NotebookLM notebook, uploads both, and asks it for
   `bullets` show notes, a headline title naming the day's biggest stories,
   and one wry line for the
   email. That last prompt is steered at the lighter end of the news
   on purpose — a joke about the day's body count is not a joke — and an empty
   or over-long answer is dropped rather than printed.
6. Prunes old briefings from the archive, then emails an HTML briefing — new
   picks, the forecast, the quote, the notes — with the plain-text version
   carried alongside it. Each note carries a small ↗ to the article it most
   likely came from; NotebookLM reports no provenance, so that link is inferred
   from shared distinctive words and omitted rather than guessed when the match
   is weak. Nothing links back to a server: the email is the whole product.
   Or the error, if the run failed.

An optional `torrents` section fetches a listing page behind a session cookie,
shows only entries missing from `torrents-seen.jsonl`, and adds them to the
email. It is deliberately kept out of the digest, so it never reaches NotebookLM
and is never summarised.

Designed to run on a schedule in Docker on a Synology NAS, but nothing about it
is Synology-specific.

## Dependencies

Everything is baked into the image (see `Dockerfile`):
[`notebooklm-py`](https://pypi.org/project/notebooklm-py/) (the `notebooklm`
CLI that drives NotebookLM headlessly), `feedparser`, `PyYAML`, and
[`trafilatura`](https://trafilatura.readthedocs.io/) for article extraction.
Note trafilatura 1.x pulls a `justext`/`lxml.html.clean` combination that
breaks on modern lxml — stay on 2.x, which the Dockerfile pins.

**NotebookLM auth must be set up separately** — there is no API key; the CLI
replays a browser session. On a machine with Chrome, log in and mint a master
token:

```bash
notebooklm login --master-token --account you@gmail.com --cdp-url http://127.0.0.1:9222
```

then copy `~/.notebooklm/` to the host running the container (it is mounted at
`/root/.notebooklm`, see `docker-compose.yml`). Keep it out of git — it holds
live Google session credentials (`auth/` is gitignored here).

## Configuration

Copy `config.yaml.example` to `config.yaml` in the output directory (the volume
mounted at `/data`; override with `BRIEFING_CONFIG`). It sets the feeds, how
many articles to fetch in full (`full_text.count`, with `workers` kept low for
modest hardware), how many briefings to keep, and the email addresses/SMTP host. It is
read at runtime from the mounted volume, so edits take effect on the next run
without a rebuild.

Secrets and host settings come from the environment — put them in `.env` next
to `docker-compose.yml` (gitignored):

| Variable        | Required | Purpose                                          |
|-----------------|----------|--------------------------------------------------|
| `SMTP_PASSWORD` | yes*     | Gmail app password for the `email.from` account  |
| `TZ`            | no       | Container timezone (default `UTC`)               |
| `SYSLOG_HOST`   | no       | Mirror logs to this syslog server (UDP 514)      |
| `SYSLOG_TAG`    | no       | Syslog line tag (default `briefing`)             |
| `TRACKER_COOKIE` | no | Cookie header for the optional `torrents` listing (name it via `torrents.cookie_env`). A session credential — keep it in `.env`; it is never logged. |
| `HEALTHCHECK_URL` | no     | Ping URL (e.g. [healthchecks.io](https://healthchecks.io)) hit after each run — `/fail` appended on failure. The monitor alerts you when pings stop arriving, catching a dead scheduler that would otherwise fail silently. |

\* without it the run still works, it just skips the email.

## Running

```bash
docker compose run --rm briefing
```

The compose file mounts two host paths — adjust them to your layout:

- the repo/deploy dir (auth + `briefing.py`, which is bind-mounted over the
  baked-in copy so script edits don't need a rebuild)
- the output dir → `/data`: `config.yaml`, the archive of past briefings
  (`.txt` notes plus `.title`, `.sources`, `.weather`, `.torrents`, `.quote`,
  `.extras` and `.horoscope` sidecars), `digest.md`, `digest-yesterday.md`,
  `aired.jsonl`, `torrents-seen.jsonl`, `briefing.log`

The output dir needs no web server. It is an archive: the email is delivered, and
the files are the record of what was sent.

## Deploying

`deploy.sh` lives next to `docker-compose.yml` on the host and updates it from
this repo:

```bash
./deploy.sh --check
```

That reports what's live, what's upstream, and whether anyone edited
`briefing.py` on the host — then builds a candidate image, runs the suite
inside it, and checks the machine's own `config.yaml` against the new code,
all without touching anything. Drop `--check` to actually swap and rebuild.

Nothing is replaced until all three pass. The config check is the one CI can't
do: CI only ever sees `config.yaml.example`, while the host has its own feeds,
feeds and settings, and a renamed key would otherwise surface at 6am as a
failed run. Each deploy backs up the previous `briefing.py` and prints its
rollback command; a failed rebuild rolls back on its own.

It fetches the tarball for a resolved commit SHA rather than the branch —
branch tarballs and `raw.githubusercontent.com` are CDN-cached and will hand
back the previous commit for several minutes after a push.

`docker-compose.yml` is reported but never overwritten: the volume paths belong
to the host, not the repo.

Nor does the script deploy itself — bash reads a script as it executes, so
overwriting the running file can make it resume at the wrong byte. Instead it
reports its own drift and stages the new copy as `deploy.sh.new`, to be swapped
in once the run has finished:

```bash
mv deploy.sh.new deploy.sh
```

This matters more than it sounds: a stale `deploy.sh` checks the host's config
with yesterday's rules, and will happily block a deploy over a key the code no
longer uses.

## Scheduling

Any cron will do. On Synology, a DSM Task Scheduler task runs the container
daily:

```bash
docker compose -f /path/to/docker-compose.yml run --rm briefing
```

`run --rm` builds a throwaway container from the current image each time, so it
always picks up the latest `deploy.sh` rebuild. There is no long-lived
container to inspect afterwards — `briefing.log` in the output dir is the
record, along with the scheduler's own captured output and, if `SYSLOG_HOST` is
set, your syslog server. Failures also trigger the email with the error.

## Not repeating yourself

Two mechanisms, deliberately different in kind. Yesterday's digest is attached
to the notebook as a second source, which *asks* the write-up to lead with what
changed — a soft nudge, and an LLM given a fresh context each morning will
cheerfully re-run yesterday's headline anyway. So `aired.jsonl` in the output
dir is the hard gate: every story that made it into a briefing is recorded, and
for `ledger_days` afterwards it can't come back.

Identity is the article's canonical URL — query strings and `www.` stripped —
rather than its headline. That is the point: a developing story publishes a new
article each day at a new URL, so follow-ups still run, while the same piece
sitting in a feed for three days is only ever covered once.

## Tests

```bash
python tests/test_briefing.py
```

Runs offline in about a second: feeds are stubbed, NotebookLM is stubbed, and
everything else — freshness, dedup, the ledger, MP4 duration parsing, feed and
email rendering, pruning — runs for real against a temporary directory. Add
`--live` to also fetch the real feeds and extract article text over the
network.

CI runs the offline suite on every push, then builds the image and imports it
against `config.yaml.example`. That last step is the one that matters in
practice: it catches the example config drifting out of step with the code, and
it catches a dependency that installs but won't import.
