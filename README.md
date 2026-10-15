# The Telegram Documentaries

A Telegram bot that turns a portrait photo into a narrated, comedy-wildlife
documentary about the person in it (see `SPECS/MISSION.md`).

**Status: Phase 6 — the Narrator.** The bot long-polls the Telegram Bot API,
gates incoming portraits with a Gemini vision check, takes a passing portrait
straight into a one-question-at-a-time behavioural interview, and — as soon as
the interview finishes — fuses the original photo with the behavioural dossier
in a single Gemini image call, delivers the hybrid animal portrait straight to
the chat, follows it immediately with a dramatic one-paragraph narration
script, and then renders that script as a **playable MP3 audio message** via
the Gemini **Interactions API** TTS (`gemini-3.8-flash-lite-tts`) in the same
update, with no user input in between. The raw script and raw audio are stored
on the session, ready for re-delivery. Anything that is not a portrait is
rejected with a reset. Phase 1's unconditional echo (`hey mate!`) is gone.

## What the bot does (Phase 6: the Bouncer + the Interviewer + the Converter + the Scripter + the Narrator)

The gateway routes every incoming message with one rule set:

- **`/start` / `/restart`** → reset the whole session (photo, interview log,
  pending question, dossier, portrait + script + narrator deliver flags, stored
  script and narration audio) and ask for a portrait photo. No vision call is
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
- **The hybrid portrait (phase `done`)** → the moment the dossier report is
  sent, the Converter runs **in the same update** (no user input): a short
  teaser ("Painting your portrait…") is sent first, then the original photo and
  the dossier are fused by Gemini 3.1 Flash Image in **one multimodal call**
  (no intermediate text step), and the returned image — validated before any
  send (non-empty, `image/*`, ≤ 10 MB) — is delivered via a multipart
  `sendPhoto` with a tiny caption naming the suggested animal. The original
  portrait is **never** sent to Telegram, only the generated hybrid.
  - A failed generation or upload logs `conversion_failed` loudly and sends an
    apology; `portrait_sent` is only set after a successful upload, so the chat
    stays retryable and the next update completes the delivery.
- **After the portrait is delivered** (`portrait_sent`) → the Scripter runs
  **in the same update** (no user input): it turns the stored behavioural
  dossier into one dramatic, comedic **narration script** and sends it to the
  chat right after the portrait. The persona (*revered, dramatic British
  wildlife documentary narrator*) is enforced via the Gemini request's
  `systemInstruction`; the output contract is enforced **mechanically** on the
  model's text: non-empty, a single paragraph (no blank-line breaks), and at
  most **90 words** — plain prose, no markdown, no titles, ready for a voice
  synthesiser. The raw script string is **stored on the session**
  (`SessionState.script`) for the Phase 6 Narrator, and `script_sent` is set
  only once the script actually reached the chat.
  - A failed generation logs `script_failed` loudly, sends an apology and stays
    retryable; the next update retries.
  - A failed *send* re-sends the **stored** script on the next update — the
    Scripter is never called twice for the same subject (the Interviewer's
    re-ask pattern).
- **After the script is delivered** (`script_sent`) → the Narrator runs **in the
  same update** (no user input): the stored script is sent to the Gemini
  **Interactions API** (`POST /v1beta/interactions`,
  `gemini-3.8-flash-lite-tts` by default) as a single `user_input` text content
  — the raw script **verbatim** — with the fixed delivery **style**
  (`DEFAULT_NARRATOR_STYLE`) carried as a `speech_metadata` annotation (never
  prepended text, which the TTS would read aloud) and the prebuilt voice id
  (`en-gb-storyteller-4`, overridable via `GEMINI_NARRATOR_VOICE`) in
  `generation_config.speech_config`. The model returns an **MP3** audio payload
  (`response_format: {"type": "audio", "mime_type": "audio/mp3"}`). The payload
  is validated mechanically before any send (non-empty, `audio/mp3*` or
  `audio/mpeg*`, ≤ 5 MB — `MAX_NARRATION_BYTES`) and delivered as a Telegram
  **`sendAudio`** audio message (`multipart/form-data`, file field `audio`,
  filename `narration.mp3`). The raw audio bytes are **stored on the session**
  (`SessionState.narration`), and `narrator_sent` is set only once the audio
  message actually reached the chat — so the narration is synthesised once and
  delivered **exactly once**.
  - A failed synthesis (or an unusable payload) logs `narrator_failed` loudly
    and sends an apology; the next update retries.
  - A failed *send* re-sends the **stored** audio bytes on the next update — the
    Narrator is never called a second time (no re-synthesis).
- **After the audio message is delivered** (`portrait_sent` + `script_sent` +
  `narrator_sent`) → non-command text gets the Interviewer's light "observation
  complete" reply; no further model call, no re-conversion, no re-scripting, no
  re-narration, no state change.
- **Plain text** or any non-photo content (document / sticker / voice) while a
  portrait is expected → the photo prompt again; the session is never lost or
  corrupted.
- **Albums / media groups** → only the **first** photo is processed; the
  siblings that Telegram delivers as separate updates sharing a
  `media_group_id` are ignored (and logged as skipped), at any phase.
- **Everything degrades gracefully**: a failed download, a Gemini/vision
  error, an interview `InterviewError`, a conversion `ConverterError` (image
  generation or `sendPhoto` upload), a scripting `ScriptError` (generation or
  delivery), a narration `NarratorError` (TTS generation or `sendAudio` upload),
  or a `sendMessage` failure is logged loudly with a traceback, the user gets an
  apology/re-prompt, and nothing crashes the polling loop.
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

> `pydantic` is the only runtime dependency — for Phases 1–6.
> The Telegram transport *and* the Gemini adapters speak plain stdlib
> `urllib`, so no third-party HTTP client library is pinned. Phase 6's audio
> message needs no audio encoder: the Gemini **Interactions API** TTS returns
> **MP3** directly for `gemini-3.8-flash-lite-tts`, a format Telegram's
> `sendAudio` accepts natively.

## Configure

Secrets live in `.env` only (gitignored — never commit them):

```bash
cp .env.example .env
# then edit .env and set TELEGRAM_BOT_TOKEN and GEMINI_API_KEY
```

Two secrets are **required** to start the bot:

- `TELEGRAM_BOT_TOKEN` — the Bot API token.
- `GEMINI_API_KEY` — the Google AI Studio key used by the Bouncer's vision
  gate, the Interviewer, the Converter, the Scripter **and** the Narrator. A
  missing/blank key is a hard startup error (exit code `2`) — the bot refuses to
  run keyless.

Both are read by a minimal built-in `.env` loader
(`telegram_documentaries/config.py`): the process environment wins over the
file, and a missing/empty value is a hard error — the bot never falls back to
a hardcoded value.

Eight optional variables, all following the same env-beats-file precedence (a
blank value falls back to the default):

- `GEMINI_VISION_MODEL` — overrides the Bouncer's vision model; defaults to
  `gemini-3.1-flash-lite`.
- `GEMINI_INTERVIEW_MODEL` — overrides the Interviewer's model; defaults to
  `gemini-3.1-flash-lite`.
- `GEMINI_CONVERTER_MODEL` — overrides the Converter's image model; defaults to
  `gemini-3.1-flash-image`.
- `GEMINI_SCRIPT_MODEL` — overrides the Scripter's narration model; defaults to
  `gemini-3.1-flash-lite`.
- `GEMINI_CONVERTER_TIMEOUT` — the Converter's image-call timeout in seconds;
  defaults to `120.0` (Flash Image routinely exceeds the shared 30 s default).
  A value that is not a positive, finite number of seconds is a hard startup
  error.
- `GEMINI_NARRATOR_MODEL` — overrides the Narrator's TTS model; defaults to
  `gemini-3.8-flash-lite-tts` (the Interactions API's lite TTS tier).
- `GEMINI_NARRATOR_VOICE` — overrides the Narrator's prebuilt voice id; defaults
  to `en-gb-storyteller-4` (the built-in "Nature Documentary Narrator": a
  56-year-old male, low/deep pitch, British). The delivery *style* is a fixed
  constant (`DEFAULT_NARRATOR_STYLE`), sent as `speech_metadata.style` rather
  than prepended to the script.
- `GEMINI_NARRATOR_TIMEOUT` — the Narrator's TTS-call timeout in seconds;
  defaults to `90.0` (TTS is slow, like image generation). A value that is not a
  positive, finite number of seconds is a hard startup error.

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
animal, never the user-derived summary). The Converter adds
`conversion_started`, `conversion_succeeded`, `portrait_sent`,
`conversion_failed` and `conversion_skipped` (only ids and the image's mime
type — never photo bytes, base64 payloads or dossier text). The Scripter adds
`script_started`, `script_ready` (word count and the suggested animal — never
the script text), `script_sent`, `script_failed`, `script_skipped` and a
`script_resend` debug marker; **the raw script is never written to a log**
(user-derived content that can echo the dossier's secrets). The Narrator adds
`narration_started`, `narrator_ready` (byte count and mime type **only** — never
the audio bytes, never the script text), `narration_sent`, `narrator_failed`
and `narration_skipped`. Secrets never appear in any log line — the Telegram
transport redacts the token and the shared Gemini client (`gemini.py`) redacts
the API key for **all five** adapters (vision, interview, converter, scripter,
narrator), from every message, log line and formatted traceback.

## Test

```bash
scripts/test                 # full suite (pytest tests/)
scripts/test -k offset       # extra pytest args are forwarded
```

The suite is Red/Green TDD and uses fake transports / fake `urlopen` /
`FakeGate` / `FakeLLM` / `FakeConverter` / `FakeScripter` / `FakeNarrator`
throughout — **no test contacts Telegram or Gemini** (the full suite passes with
every socket entry point patched to raise). The Gemini adapter tests
(`test_gemini`, `test_vision`, `test_interviewer`, `test_converter`,
`test_scripter`, `test_narrator`) assert on the outgoing request body (base64
`inline_data`, JSON `responseSchema` / `responseModalities`, the Interactions
`input` / `response_format` / `generation_config` body, model id, transcript
role alternation) without a socket. New in Phase 6: `test_narrator.py` (58) and
the component `test_narrator_flow.py` (4), plus narrator cases across
`test_gemini.py`, `test_transport.py`, `test_session.py`, `test_bouncer.py`,
`test_config.py`, `test_cli.py` and `test_polling.py` — the shared client's
Interactions audio path (`generate_interaction_audio`) and the multipart
`sendAudio` upload are covered by `test_gemini.py` and `test_transport.py`.

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
  bouncer.py          # router across stages: /start, gate, interview, handoff
  cli.py              # argparse wiring, exit codes
  config.py           # minimal .env loader + secret resolution + model defaults
  converter.py        # typed ConverterLLM port + Gemini image adapter + routing
  defaults.py         # shared constant home (models, question count, limits)
  gemini.py           # shared Gemini generateContent/Interactions client + redaction
  interviewer.py      # typed InterviewLLM port + Gemini adapter + interview routing
  logging_config.py   # JSON-lines formatter + log_lifecycle decorator
  media.py            # photo intake: largest PhotoSize + safe getFile/download
  models.py           # Pydantic boundary models (Update/Message/PhotoSize/FileRef)
  narrator.py         # typed NarratorLLM port + Gemini TTS adapter + routing
  polling.py          # getUpdates loop + offset acking
  scripter.py         # typed ScripterLLM port + Gemini narration adapter + routing
  session.py          # per-chat_id state driver (v5: phases, photo, dossier, script, narration)
  transport.py        # stdlib urllib transport (injectable urlopen)
  vision.py           # typed VisionGate port + Gemini REST adapter (shared client)
tests/
  conftest.py         # FakeTransport + FakeGate + FakeLLM + FakeConverter + FakeScripter + FakeNarrator
  unit/               # isolated logic, no I/O (session, media, gemini, vision, ...)
  component/          # whole gateway over faked seams (transport + gate + LLM)
scripts/
  test                # run the test suite
  hooks               # pre-commit checks
pyproject.toml         # ruff + mypy configuration
SPECS/                # MISSION.md, TECH.md, ROADMAP.md (the contract)
```

## Roadmap

Phase 6 of `SPECS/ROADMAP.md` (the Narrator) is implemented on branch
`feature/2026-10-12-narrator` and verified. Phases 1–5 (gateway, Bouncer,
Interviewer, Converter, Scripter) are on `main`. The five-stage pipeline is
complete: a playable **MP3 audio message** now follows the narration script in
the same update, speaking the Scripter's words. Revision R1 (2026-10-14)
replaced the impossible OGG/`sendVoice` design (no Gemini endpoint produces
OGG/Opus without an encoder) with an MP3 audio file via `sendAudio`, synthesised
through the Gemini Interactions API.
