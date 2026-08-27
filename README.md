# Daily briefing

[![CI](https://github.com/Oldandcranky/briefing/actions/workflows/ci.yml/badge.svg)](https://github.com/Oldandcranky/briefing/actions/workflows/ci.yml)

A self-hosted daily news podcast: pulls headlines from RSS feeds, has
[NotebookLM](https://notebooklm.google.com) generate an audio overview of them,
and publishes the result as a podcast feed plus an email digest.

Each run:

1. Pulls the configured RSS feeds, drops anything older than `max_age_hours`
   (and anything undated), skips stories already aired in the last
   `ledger_days`, and folds near-duplicate headlines together — so a story
   three outlets ran gets covered once, and is treated as one of the day's big
   ones.
2. Fetches the full article text for the top stories (not just the RSS blurb)
   so the hosts have something to actually talk about.
3. Builds a markdown digest, keeping yesterday's alongside it as a second
   source so the episode leads with what has *changed*.
4. Creates a fresh NotebookLM notebook, uploads both, and generates an audio
   overview (the notebook is deleted afterwards, even on failure).
5. Downloads the episode, asks NotebookLM for six bullet-point show notes and
   a headline title naming the day's biggest stories.
6. Prunes old episodes, then rewrites `feed.xml` (RSS with iTunes duration
   tags) and `index.html` — a listening page with a player, show notes, and a
   collapsed list of every source story linked back to the original article,
   grouped by outlet. Both are plain files for any static web server; the page
   loads no external assets, and story links are escaped and restricted to
   http(s), since feed contents are untrusted.
7. Emails the bullet points and episode link — or the error, if the run failed.

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
modest hardware), the audio prompts/format/length, the podcast title and public
`base_url`, how many episodes to keep, and the email addresses/SMTP host. It is
read at runtime from the mounted volume, so edits take effect on the next run
without a rebuild.

`audio.prompts` is a **list** — one is picked at random each run, which keeps
the show from opening the same way every morning.

Secrets and host settings come from the environment — put them in `.env` next
to `docker-compose.yml` (gitignored):

| Variable        | Required | Purpose                                          |
|-----------------|----------|--------------------------------------------------|
| `SMTP_PASSWORD` | yes*     | Gmail app password for the `email.from` account  |
| `TZ`            | no       | Container timezone (default `UTC`)               |
| `SYSLOG_HOST`   | no       | Mirror logs to this syslog server (UDP 514)      |
| `SYSLOG_TAG`    | no       | Syslog line tag (default `briefing`)             |
| `HEALTHCHECK_URL` | no     | Ping URL (e.g. [healthchecks.io](https://healthchecks.io)) hit after each run — `/fail` appended on failure. The monitor alerts you when pings stop arriving, catching a dead scheduler that would otherwise fail silently. |

\* without it the run still works, it just skips the email.

## Running

```bash
docker compose run --rm briefing
```

The compose file mounts two host paths — adjust them to your layout:

- the repo/deploy dir (auth + `briefing.py`, which is bind-mounted over the
  baked-in copy so script edits don't need a rebuild)
- the output dir → `/data`: `config.yaml`, episodes (`.m4a` plus `.txt` show
  notes, `.title` and `.sources` sidecars), `digest.md`,
  `digest-yesterday.md`, `aired.jsonl`, `feed.xml`, `index.html`,
  `briefing.log`

Serve the output dir over HTTPS (e.g. `tailscale serve`, nginx, or the NAS's
web station) at `feed.base_url`. Point your podcast app at
`<base_url>/feed.xml`, or just open `<base_url>/` to listen in a browser.

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
prompts and settings, and a renamed key would otherwise surface at 6am as a
failed run. Each deploy backs up the previous `briefing.py` and prints its
rollback command; a failed rebuild rolls back on its own.

It fetches the tarball for a resolved commit SHA rather than the branch —
branch tarballs and `raw.githubusercontent.com` are CDN-cached and will hand
back the previous commit for several minutes after a push.

`docker-compose.yml` is reported but never overwritten: the volume paths belong
to the host, not the repo.

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
to the notebook as a second source, which *asks* the hosts to lead with what
changed — a soft nudge, and an LLM given a fresh context each morning will
cheerfully re-run yesterday's headline anyway. So `aired.jsonl` in the output
dir is the hard gate: every story that made it into an episode is recorded, and
for `ledger_days` afterwards it can't come back.

Identity is the article's canonical URL — query strings and `www.` stripped —
rather than its headline. That is the point: a developing story publishes a new
article each day at a new URL, so follow-ups still air, while the same piece
sitting in a feed for three days is only ever covered once.

## Tests

```bash
python tests/test_briefing.py
```

Runs offline in about a second: feeds are stubbed, NotebookLM is stubbed, and
everything else — freshness, dedup, the ledger, MP4 duration parsing, feed and
page generation, pruning — runs for real against a temporary directory. Add
`--live` to also fetch the real feeds and extract article text over the
network.

CI runs the offline suite on every push, then builds the image and imports it
against `config.yaml.example`. That last step is the one that matters in
practice: it catches the example config drifting out of step with the code, and
it catches a dependency that installs but won't import.
