# The Telegram Documentaries

A Telegram bot that turns a portrait photo into a narrated, comedy-wildlife
documentary about the person in it (see `SPECS/MISSION.md`).

**Status: Phase 1 — repository & gateway.** The bot long-polls the Telegram
Bot API and echoes every incoming message. No Gemini/ADK pipeline yet.

## Echo rule (Phase 1)

> Every incoming Telegram **message** — text, photo, or any other content
> type — gets **exactly one** reply: the hardcoded string `hey mate!`.
> Updates that carry no `message` at all (edited messages, callback queries,
> ...) are acknowledged and ignored without crashing.

## How it works: long polling

The gateway never exposes a URL. It repeatedly calls `getUpdates` with a
server-side `timeout` of 10 seconds (Telegram holds the request open until a
message arrives or the window expires — "long polling"), handles every
update in the batch, then **acks the batch** by sending the next call with
`offset = max(update_id) + 1`. Telegram therefore never redelivers what was
already handled — including malformed updates, which are acked and skipped
so a single poison payload cannot wedge the loop.

All external input is parsed at the edge into Pydantic models
(`telegram_documentaries/models.py`); the rest of the code never touches raw
dicts. The HTTP layer is stdlib `urllib` behind an injectable seam, so tests
never need a live bot or a socket.

## Install

Python 3.11+ (3.x with `venv`).

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # runtime + test/lint/type deps
```

Runtime-only install: `pip install -r requirements.txt`.

> `pydantic` is the runtime dependency used by Phase 1. `requests` is pinned
> for later phases but is not used by the gateway (transport is stdlib
> `urllib`).

## Configure

Secrets live in `.env` only (gitignored — never commit them):

```bash
cp .env.example .env
# then edit .env and set TELEGRAM_BOT_TOKEN
```

`TELEGRAM_BOT_TOKEN` is read by a minimal built-in `.env` loader
(`telegram_documentaries/config.py`): the process environment wins over the
file, and a missing/empty token is a hard error — the bot never falls back
to a hardcoded value.

## Run

```bash
python -m telegram_documentaries
```

Options:

| Flag | Default | Meaning |
| --- | --- | --- |
| `--max-polls N` | run forever | Stop after N `getUpdates` polls (useful for smoke runs) |
| `--log-level LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |

Exit codes: `0` clean stop, `1` runtime/transport failure, `2`
configuration problem (e.g. missing token).

Logs are structured: one JSON object per line on stderr, with `ts`, `level`,
`logger`, `message` plus event fields such as `event`, `chat_id`,
`update_id`:

```json
{"ts": "2026-10-06T10:13:04.445Z", "level": "ERROR", "logger": "telegram_documentaries.cli", "message": "configuration error", "event": "config_error", "error": "TELEGRAM_BOT_TOKEN is not set; ..."}
```

## Test

```bash
scripts/test                 # full suite (pytest tests/)
scripts/test -k offset       # extra pytest args are forwarded
```

The suite is Red/Green TDD and uses fake transports/fake `urlopen`
throughout — **no test contacts Telegram or Gemini**.

## Pre-commit checks

```bash
scripts/hooks                # compileall + smoke import + ruff + mypy
```

`scripts/hooks` runs, in order:

1. `python -m compileall -q telegram_documentaries tests`
2. `python -c "import telegram_documentaries"`
3. `python -m ruff check .` (includes the no-bare-`except` rule, E722)
4. `python -m mypy telegram_documentaries`

## Project layout

```
telegram_documentaries/
  __main__.py         # python -m entrypoint
  cli.py              # argparse wiring, exit codes
  config.py           # minimal .env loader + token resolution
  echo.py             # the echo rule: 'hey mate!' per message
  logging_config.py   # JSON-lines formatter + log_lifecycle decorator
  models.py           # Pydantic boundary models (Update/Message/Chat/User)
  polling.py          # getUpdates loop + offset acking
  transport.py        # stdlib urllib transport (injectable urlopen)
tests/
  conftest.py         # FakeTransport + raw Telegram payload builders
  unit/               # isolated logic, no I/O
  component/          # whole gateway over one faked seam (urlopen)
scripts/
  test                # run the test suite
  hooks               # pre-commit checks
SPECS/                # MISSION.md, TECH.md, ROADMAP.md (the contract)
```

## Roadmap

Phase 1 of `SPECS/ROADMAP.md` (repository & gateway). Next: the Bouncer
vision gate, then Interviewer → Converter → Scripter → Narrator.
