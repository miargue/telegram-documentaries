# The Telegram Documentaries

A Telegram bot that turns a portrait photo into a narrated, comedy-wildlife
documentary about the person in it (see `SPECS/MISSION.md`).

**Status: Phase 2 — the Bouncer.** The bot long-polls the Telegram Bot API and
gates incoming portraits with a Gemini vision check: a human present → the
session advances ready for the interview; anything else → a cheeky rejection
and a reset. Phase 1's unconditional echo (`hey mate!`) is gone.

## What the bot does (Phase 2: the Bouncer)

The gateway routes every incoming message with one rule set:

- **`/start` / `/restart`** → reset the session and ask for a portrait photo.
  No vision call is made.
- **A photo** → the largest resolution is downloaded and sent to Gemini's
  vision model with a structured prompt ("is a human clearly present?").
  - Human → a short success message, the photo bytes are kept, and the chat
    moves to the `gate_passed` phase (ready for the Phase 3 Interviewer).
  - No human (animal, pet, food, landscape, empty image, ...) → a cheeky
    rejection message and the conversation resets (phase back to
    `awaiting_photo`, photo purged).
- **Plain text** or any non-photo content (document / sticker / voice) while a
  portrait is expected → the photo prompt again; the session is never lost or
  corrupted.
- **Albums / media groups** → only the **first** photo is gated; the siblings
  that Telegram delivers as separate updates sharing a `media_group_id` are
  ignored (and logged as skipped).
- **Everything degrades gracefully**: a failed download, a Gemini error, or a
  `sendMessage` failure is logged loudly with a traceback, the user gets an
  apology/re-prompt, and nothing crashes the polling loop.
- Updates that carry no `message` at all (edited messages, callback queries,
  ...) are acknowledged and ignored without crashing.

A photo message with a caption is still treated as a photo (the caption is
ignored for now).

## How it works: long polling

The gateway never exposes a URL. It repeatedly calls `getUpdates` with a
server-side `timeout` of 10 seconds (Telegram holds the request open until a
message arrives or the window expires — "long polling") and handles every
update in the batch.

The **first** poll uses `offset = -1`, which tells Telegram to forget
everything queued while the bot was down — a restart therefore does not
re-answer the backlog, it only reacts to messages that arrive after boot.
Every later poll **acks the batch** by sending the next call with
`offset = max(update_id) + 1`, so Telegram does not redeliver what was
already handled.

Acking is per-update: an update that fails validation but carries an
**integer** `update_id` is acked past and skipped, so a single poison payload
cannot wedge the loop. Updates whose `update_id` cannot be parsed as an int
are skipped and **never acked** (there is no id to ack with), so Telegram
will redeliver them on the next poll until they can be handled.

All external input is parsed at the edge into Pydantic models
(`telegram_documentaries/models.py`); the rest of the code never touches raw
dicts. Both the Telegram HTTP layer and the Gemini REST adapter are stdlib
`urllib` behind an injectable seam (`urlopen`), so tests never need a live
bot, a live Gemini API, or a socket.

## Install

Python 3.11+ (3.x with `venv`).

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # runtime + test/lint/type deps
```

Runtime-only install: `pip install -r requirements.txt`.

> `pydantic` is the only runtime dependency — for both Phase 1 and Phase 2.
> The Telegram transport *and* the Gemini adapter speak plain stdlib
> `urllib`, so no third-party HTTP client library is pinned.

## Configure

Secrets live in `.env` only (gitignored — never commit them):

```bash
cp .env.example .env
# then edit .env and set TELEGRAM_BOT_TOKEN and GEMINI_API_KEY
```

Two secrets are **required** to start the bot:

- `TELEGRAM_BOT_TOKEN` — the Bot API token.
- `GEMINI_API_KEY` — the Google AI Studio key used by the Bouncer's vision
  gate. A missing/blank key is a hard startup error (exit code `2`) — the bot
  refuses to run keyless.

Both are read by a minimal built-in `.env` loader
(`telegram_documentaries/config.py`): the process environment wins over the
file, and a missing/empty value is a hard error — the bot never falls back to
a hardcoded value.

One optional variable:

- `GEMINI_VISION_MODEL` — overrides the vision model; defaults to
  `gemini-3.1-flash-lite`.

## Run

```bash
python -m telegram_documentaries
```

Options:

| Flag | Default | Meaning |
| --- | --- | --- |
| `--max-polls N` | run forever | Stop after N `getUpdates` polls. `N` must be `>= 1` (useful for smoke runs) |
| `--log-level LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |

On startup the gateway resolves both secrets, calls `getMe` once, and logs the
bot's username in the `gateway_starting` event. A bad or revoked
`TELEGRAM_BOT_TOKEN` therefore fails immediately with a `get_me_failed` error
and exit code `1`, instead of surfacing only on the first `getUpdates`.

Exit codes: `0` clean stop, `1` runtime/transport failure (including a failed
startup `getMe` probe), `2` configuration problem or invalid command-line
argument (e.g. missing token, missing `GEMINI_API_KEY`, `--max-polls 0`),
`130` deliberate Ctrl-C stop.

Logs are structured: one JSON object per line on stderr, with `ts`, `level`,
`logger`, `message` plus event fields such as `event`, `chat_id`,
`update_id`. When a line is logged with `exc_info=True`, the formatted
traceback is carried in an `exc` field:

```json
{"ts": "2026-10-06T10:13:04.445Z", "level": "ERROR", "logger": "telegram_documentaries.cli", "message": "configuration error", "event": "config_error", "error": "TELEGRAM_BOT_TOKEN is not set; ...", "exc": "Traceback (most recent call last):\n  ...\nConfigError: TELEGRAM_BOT_TOKEN is not set; ..."}
```

The Bouncer adds structured events for the gate (`bouncer_verdict`,
`bouncer_passed`, `bouncer_rejected`, `bouncer_prompted`), failures
(`photo_fetch_failed`, `vision_failed`, `reply_failed`) and album skips
(`bouncer_album_skipped`). Secrets never appear in any log line — both the
transport and the vision adapter redact the token/key from every error message
and traceback.

## Test

```bash
scripts/test                 # full suite (pytest tests/)
scripts/test -k offset       # extra pytest args are forwarded
```

The suite is Red/Green TDD and uses fake transports / fake `urlopen` /
`FakeGate` throughout — **no test contacts Telegram or Gemini**. The vision
gate tests assert on the outgoing request body (base64 `inline_data`, JSON
`responseSchema`, model id) without a socket.

## Pre-commit checks

```bash
scripts/hooks                # compileall + smoke import + ruff + mypy
```

`scripts/hooks` runs, in order:

1. `python -m compileall -q telegram_documentaries tests`
2. `python -c "import telegram_documentaries"`
3. `python -m ruff check .` (includes the no-bare-`except` rule, E722)
4. `python -m mypy telegram_documentaries tests`

Both ruff and mypy read their configuration from `pyproject.toml`.

## Project layout

```
telegram_documentaries/
  __main__.py         # python -m entrypoint
  bouncer.py          # Phase 2 message routing: /start, prompt, gate, reject
  cli.py              # argparse wiring, exit codes
  config.py           # minimal .env loader + secret resolution + model default
  defaults.py         # shared constant home (e.g. DEFAULT_VISION_MODEL)
  logging_config.py   # JSON-lines formatter + log_lifecycle decorator
  media.py            # photo intake: largest PhotoSize + safe getFile/download
  models.py           # Pydantic boundary models (Update/Message/PhotoSize/FileRef)
  polling.py          # getUpdates loop + offset acking
  session.py          # per-chat_id session state driver (phases, photo, dedupe)
  transport.py        # stdlib urllib transport (injectable urlopen)
  vision.py           # typed VisionGate port + Gemini REST adapter
tests/
  conftest.py         # FakeTransport + FakeGate + raw Telegram payload builders
  unit/               # isolated logic, no I/O (session, media, vision, bouncer, ...)
  component/          # whole gateway over faked seams (transport + gate)
scripts/
  test                # run the test suite
  hooks               # pre-commit checks
pyproject.toml         # ruff + mypy configuration
SPECS/                # MISSION.md, TECH.md, ROADMAP.md (the contract)
```

## Roadmap

Phase 2 of `SPECS/ROADMAP.md` (the Bouncer) is implemented on branch
`feature/2026-10-09-bouncer` and verified. Next: the Phase 3 Interviewer, then
Converter → Scripter → Narrator.
