# The Telegram Documentaries

A Telegram bot that turns a portrait photo into a narrated, comedy-wildlife
documentary about the person in it (see `SPECS/MISSION.md`).

**Status: Phase 3 — the Interviewer.** The bot long-polls the Telegram Bot API,
gates incoming portraits with a Gemini vision check, then takes a passing
portrait straight into a one-question-at-a-time behavioural interview and
delivers a dossier with a suggested animal. Anything that is not a portrait is
rejected with a reset. Phase 1's unconditional echo (`hey mate!`) is gone.

## What the bot does (Phase 3: the Bouncer + the Interviewer)

The gateway routes every incoming message with one rule set:

- **`/start` / `/restart`** → reset the whole session (photo, interview log,
  pending question, dossier) and ask for a portrait photo. No vision call is
  made.
- **A photo** → the largest resolution is downloaded and sent to Gemini's
  vision model with a structured prompt ("is a human clearly present?").
  - Human → a short success message, the photo bytes are kept, and the chat
    immediately **hands off to the Interviewer**, which asks question 1.
  - No human (animal, pet, food, landscape, empty image, ...) → a cheeky
    rejection message and the conversation resets (phase back to
    `awaiting_photo`, photo purged).
- **The interview** (phase `interviewing`) → Gemini generates **exactly one
  question at a time** from the researcher persona and the transcript so far.
  - Each plain-text message is one **answer**; it is recorded as a
    question-then-answer pair and the next question follows.
  - Exactly **5** answers are collected, then a synthesis step produces the
    behavioural **dossier** (`summary`, `suggested_animal`, optional
    `animal_reason`), which is **sent to the chat and stored on the session**
    (phase `done`).
  - Anything that is **not** an answer (photo / sticker / voice / document /
    empty text) **re-asks the current question** and advances nothing. Re-asks
    use the stored pending question, so no model call is made and the wording
    never changes.
  - A failed dispatch never advances the interview: the answer is only
    committed once the next question (or the report) has actually been sent.
- **After `done`** → non-command text gets a light "observation complete"
  reply; no further model call, no state change.
- **Plain text** or any non-photo content (document / sticker / voice) while a
  portrait is expected → the photo prompt again; the session is never lost or
  corrupted.
- **Albums / media groups** → only the **first** photo is processed; the
  siblings that Telegram delivers as separate updates sharing a
  `media_group_id` are ignored (and logged as skipped), at any phase.
- **Everything degrades gracefully**: a failed download, a Gemini/vision
  error, an interview `InterviewError`, or a `sendMessage` failure is logged
  loudly with a traceback, the user gets an apology/re-prompt, and nothing
  crashes the polling loop.
- Updates that carry no `message` at all (edited messages, callback queries,
  ...) are acknowledged and ignored without crashing.

A photo message with a caption is still treated as a photo (the caption is
ignored).

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

> `pydantic` is the only runtime dependency — for Phases 1–3.
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
  gate **and** the Interviewer. A missing/blank key is a hard startup error
  (exit code `2`) — the bot refuses to run keyless.

Both are read by a minimal built-in `.env` loader
(`telegram_documentaries/config.py`): the process environment wins over the
file, and a missing/empty value is a hard error — the bot never falls back to
a hardcoded value.

Two optional variables:

- `GEMINI_VISION_MODEL` — overrides the Bouncer's vision model; defaults to
  `gemini-3.1-flash-lite`.
- `GEMINI_INTERVIEW_MODEL` — overrides the Interviewer's model; defaults to
  `gemini-3.1-flash-lite`. Both follow the same env-beats-file precedence, and
  a blank value falls back to the default.

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
(`bouncer_album_skipped`). The Interviewer adds `interview_started`,
`question_asked`, `answer_recorded`, `answer_truncated`, `dossier_created`,
`interview_failed` and `interview_skipped` (a dossier logs only the suggested
animal, never the user-derived summary). Secrets never appear in any log line —
the Telegram transport redacts the token and the shared Gemini client
(`gemini.py`) redacts the API key for **both** adapters, from every message,
log line and formatted traceback.

## Test

```bash
scripts/test                 # full suite (pytest tests/)
scripts/test -k offset       # extra pytest args are forwarded
```

The suite is Red/Green TDD and uses fake transports / fake `urlopen` /
`FakeGate` / `FakeLLM` throughout — **no test contacts Telegram or Gemini**
(the full suite passes with every socket entry point patched to raise). The
Gemini adapter tests (`test_gemini`, `test_vision`, `test_interviewer`) assert
on the outgoing request body (base64 `inline_data`, JSON `responseSchema`,
model id, transcript role alternation) without a socket. New in Phase 3:
`test_gemini.py` (24), `test_interviewer.py` (65) and the component
`test_interviewer_flow.py` (4).

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
  bouncer.py          # message routing: /start, prompt, gate, reject, handoff
  cli.py              # argparse wiring, exit codes
  config.py           # minimal .env loader + secret resolution + model defaults
  defaults.py         # shared constant home (models, question count, limits)
  gemini.py           # shared Gemini generateContent JSON client + redaction
  interviewer.py      # typed InterviewLLM port + Gemini adapter + interview routing
  logging_config.py   # JSON-lines formatter + log_lifecycle decorator
  media.py            # photo intake: largest PhotoSize + safe getFile/download
  models.py           # Pydantic boundary models (Update/Message/PhotoSize/FileRef)
  polling.py          # getUpdates loop + offset acking
  session.py          # per-chat_id state driver (phases, photo, interview, dossier)
  transport.py        # stdlib urllib transport (injectable urlopen)
  vision.py           # typed VisionGate port + Gemini REST adapter (shared client)
tests/
  conftest.py         # FakeTransport + FakeGate + FakeLLM + payload builders
  unit/               # isolated logic, no I/O (session, media, gemini, vision, ...)
  component/          # whole gateway over faked seams (transport + gate + LLM)
scripts/
  test                # run the test suite
  hooks               # pre-commit checks
pyproject.toml         # ruff + mypy configuration
SPECS/                # MISSION.md, TECH.md, ROADMAP.md (the contract)
```

## Roadmap

Phase 3 of `SPECS/ROADMAP.md` (the Interviewer) is implemented on branch
`feature/2026-10-10-interviewer` and verified. Phases 1–2 (gateway, Bouncer)
are on `main`. Next: the Phase 4 Converter, then Scripter → Narrator.
