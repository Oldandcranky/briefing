# Daily briefing

A self-hosted daily news podcast: pulls headlines from RSS feeds, has
[NotebookLM](https://notebooklm.google.com) generate an audio overview of them,
and publishes the result as a podcast feed plus an email digest.

Each run:

1. Pulls the configured RSS feeds and folds near-duplicate headlines together,
   so a story three outlets ran gets covered once — and is treated as one of
   the day's big ones.
2. Fetches the full article text for the top stories (not just the RSS blurb)
   so the hosts have something to actually talk about.
3. Builds a markdown digest, keeping yesterday's alongside it as a second
   source so the episode leads with what has *changed*.
4. Creates a fresh NotebookLM notebook, uploads both, and generates an audio
   overview (the notebook is deleted afterwards, even on failure).
5. Downloads the episode, asks NotebookLM for six bullet-point show notes and
   a headline title naming the day's biggest stories.
6. Prunes old episodes and rewrites `feed.xml` (RSS with iTunes duration tags),
   ready to be served by any static web server.
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
  notes and a `.title` sidecar), `digest.md` and `digest-yesterday.md`,
  `feed.xml`, `briefing.log`

Serve the output dir over HTTPS (e.g. `tailscale serve`, nginx, or the NAS's
web station) at `feed.base_url`, and point your podcast app at
`<base_url>/feed.xml`.

## Scheduling

Any cron will do. On Synology, a DSM Task Scheduler task runs the container
daily:

```bash
docker compose -f /path/to/docker-compose.yml run --rm briefing
```

Check `briefing.log` in the output dir (or `docker logs briefing`) if an
episode doesn't show up — failures also trigger the email with the error.
