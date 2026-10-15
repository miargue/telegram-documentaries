# Tech — Technical Contract

This file is the technical contract for **The Telegram Documentaries**. Every
agent and every review is judged against it.

## Stack

- **Language:** Python 3.
- **Transport:** Telegram Bot API over **long polling** (`getUpdates`). No
  webhooks, no public URL.
- **Framework:** **No vendor agent framework as of Phase 6.** Each pipeline
  stage is a discrete module behind a narrow typed port. The Bouncer exposes a
  typed `VisionGate` port, the Interviewer a typed `InterviewLLM` port
  (`next_question` / `summarize`), the Converter a typed `ConverterLLM`
  port (`hybridize(photo, dossier) -> GeneratedImage`), the Scripter a
  typed `ScripterLLM` port (`write_script(dossier) -> str`), and the Narrator a
  typed `NarratorLLM` port (`synthesize(script) -> AudioPayload`); all five
  adapters are direct **Gemini REST** callers over stdlib `urllib` with an
  injectable `urlopen` seam — the same no-third-party-HTTP pattern as the
  Phase 1 transport. Four are `generateContent` callers; the Narrator is a
  **Gemini Interactions API** caller (`POST /v1beta/interactions`, model named
  in the body, audio block decoded from `steps[0].content[]`). The shared REST
  plumbing (endpoint build, POST, failure normalisation, candidate extraction,
  schema validation, inline-part decoding and the **single** API-key redaction
  implementation) lives once in `gemini.py` (`GeminiJsonClient` — `generate_json`
  for the JSON output of `vision.py`/`interviewer.py`/`scripter.py`,
  `generate_image` for the image output of Phase 4's `converter.py`,
  `generate_interaction_audio` for the audio output of Phase 6's `narrator.py`)
  and is reused by `vision.py`, `interviewer.py`, `converter.py`, `scripter.py`
  and `narrator.py`. The Telegram transport owns a multipart `sendPhoto`
  (`transport.py`) for delivering the generated portrait and a multipart
  `sendAudio` for delivering the MP3 audio message (file field `audio`,
  filename `narration.mp3`).
  **Google Agent Development Kit (ADK) is deferred**; if a later phase adopts
  it, it will wrap these same ports.
- **Models:**
  - The Bouncer — vision gate — **Gemini 3.1 Flash Lite**.
  - The Interviewer — sequential Q&A + orchestrator — **Gemini 3.1 Flash Lite**.
  - The Converter — hybrid portrait — **Gemini 3.1 Flash Image**.
  - The Scripter — narration prose — **Gemini 3.1 Flash Lite**.
  - The Narrator — **not an agent** — Gemini TTS via the **Interactions API**
    (`gemini-3.8-flash-lite-tts`), which returns **MP3** natively — the format
    Telegram `sendAudio` accepts. Voice is a prebuilt id
    (`en-gb-storyteller-4`); delivery style is sent as `speech_metadata.style`,
    never prepended to the script.
- **Secrets:** `TELEGRAM_BOT_TOKEN` and `GEMINI_API_KEY` from `.env`. The
  `.env` file is gitignored and must never be committed.

## Architecture

- **Hub-and-spoke (module level):** the **Bouncer is the router** across
  every stage — it dispatches on phase: a passing portrait hands off to the
  Interviewer, a `done` chat without a portrait runs the Converter, and when an
  Interviewer turn *completes* the interview the Bouncer runs the same-update
  Converter handoff itself (hub-and-spoke: neither the Interviewer nor the
  Converter knows the other). The handoff chain is **dossier → portrait →
  script → audio message in one update**: once the converted portrait lands, the
  same update continues into the Scripter, and once the script lands it
  continues into the Narrator, so the narration script and the playable audio
  message follow the portrait with **no user input** in between; the raw script
  and the raw MP3 audio are stored on the session. Each pipeline stage is a
  discrete module (Bouncer, Interviewer, Converter, Scripter, Narrator).
  Stage boundaries are **typed ports**, not vendor SDKs: the Bouncer is
  `VisionGate.classify(image) -> HumanVerdict`, the Interviewer is
  `InterviewLLM.next_question(...) -> str` / `summarize(...) -> Dossier`, the
  Converter is `ConverterLLM.hybridize(photo, dossier) -> GeneratedImage`, the
  Scripter is `ScripterLLM.write_script(dossier) -> str`, and the Narrator is
  `NarratorLLM.synthesize(script) -> AudioPayload`, so the Gemini
  REST adapters can later be swapped for ADK agents without touching the
  routing code. The Interviewer owns its own turn-taking (the
  phase machine, the exactly-5 cap and the one-question-per-message rule);
  `InterviewLLM` merely supplies one validated question or the final dossier at
  a time, the Converter's routing owns its teaser, validation guards and
  caption, the Scripter's routing owns the apology/retry and stored-script
  re-send, and the Narrator's routing owns its output validation, apology/retry
  and stored-audio re-send (the re-ask pattern: a failed *delivery* re-sends the
  stored MP3 bytes LLM-free, so one script always yields one audio message).
- **Explicit state machine:** one well-defined phase per pipeline stage, with
  explicit transitions and a single shared state driver. Phase is stored in the
  session state, never inferred implicitly.
- **One state driver:** all session state flows through a single shared,
  versioned, per-`chat_id` schema. `/start` and `/restart` reset it to a known
  initial phase and purge temporary media.

## Contracts at boundaries

- Parse **all** external input at the edge into typed models (Pydantic):
  Telegram updates, Gemini responses, and TTS results.
- Never pass raw dicts or unvalidated payloads between modules.
- Treat every external input as **untrusted and arbitrary**: wrong content
  types, huge payloads, missing fields, out-of-order messages.

## Logging & error policy

- **Comprehensive structured logging**; prefer decorators over scattering logs
  through business logic.
- **Fail loudly and log** for non-critical, user-invisible work (background
  steps that can be retried).
- **Degrade gracefully** on a validated user's conversation path: catch errors,
  log loudly, and keep the conversation going — never raise into the user's
  flow.
- No `except: pass`, no swallowed exceptions, no un-logged fallbacks.

## Resilience (Phase 7)

- **Command recognition.** `_command(message)` reads `message.text` first, then
  `message.caption`, takes the first whitespace-delimited token, strips an
  `@botname` suffix and lowercases it — so `/start`, `/restart`, `/Start`,
  `/START`, `/ReStart`, `/start@MiargueBot`, `/restart now` and a **photo
  captioned** `/start` are all the same command. `_is_start_command` compares
  against `{"/start", "/restart"}`. A matched command always resets (through
  `reset_chat`) and re-prompts, at **every** phase, before any gate, album or
  phase routing — a captioned command's photo is never gated. A non-command
  caption is still a photo; a non-command slash text (e.g. `/help`) just
  re-prompts.
- **Wrong-payload guard matrix.** The phase × content contract is locked by
  parametrized tests (`tests/unit/test_bouncer.py::test_guard_matrix_*`):
  text, empty text and unsupported content (sticker / voice / document)
  re-prompt at `awaiting_photo` and re-ask the stored pending question
  mid-interview without advancing it; album siblings and messageless updates
  are benign skips (`SKIPPED`, no reply, still acked by the loop); the `done`
  variants route to the documented retry path (Converter / Scripter /
  Narrator) or the light-completion reply; a command resets at every phase.
  Nothing raises out of `handle_update`.
- **`getUpdates` retry policy.** `_get_updates(transport, offset, *, log,
  sleep=time.sleep)` retries **only transient** failures — forever, with capped
  exponential backoff (`RETRY_BASE_DELAY = 1.0`, `RETRY_FACTOR = 2.0`,
  `RETRY_MAX_DELAY = 30.0`, i.e. delays `1.0, 2.0, 4.0, … → 30.0`), emitting
  one structured `get_updates_retry` ERROR per attempt (`offset`, `attempt`,
  `delay`, redacted error) and one `sleep(delay)`. *Transient* = a raw
  `OSError`, or a `TelegramApiError` with no code (the transport's
  network/protocol normalisation), code `429`, or code `>= 500` —
  `status_code` takes precedence over `error_code` when both are set.
  *Permanent* = any other coded error (`400`, `401`, `403`, `404`, `409`, …):
  it is **not** retried — it propagates out of `poll_once`, `cli.main` catches
  it (`gateway_failed`) and the process exits `1`. `KeyboardInterrupt` /
  `SystemExit` and non-transport errors (programming bugs) also escape
  untouched. The startup `getMe` probe still surfaces a bad token before
  polling starts, and `run_polling` accepts/forwards `sleep` so the retry path
  is testable end-to-end without waiting.
- **Single-threaded by decision.** No worker threads, queues or per-chat
  loops: slow stage calls (the Converter, up to ~120 s) run synchronously on
  the polling loop, each bounded by its own configured timeout
  (`GEMINI_CONVERTER_TIMEOUT` 120 s default, `GEMINI_NARRATOR_TIMEOUT` 90 s,
  the shared 30 s client timeout elsewhere). A bounded synchronous call briefly
  pauses polling — acceptable for a single-user bot — and it is what makes a
  `/start` atomic.
- **Timeout invariant.** The transport client timeout
  (`transport.DEFAULT_TIMEOUT = 30.0 s`) strictly exceeds the server-side long
  poll window (`polling.LONG_POLL_TIMEOUT = 10 s`), so a long poll is never
  cut short by the client mid-window (asserted in
  `tests/unit/test_transport.py`).

## Session state

- Versioned, per-`chat_id` schema with an explicit phase field. **Version 2**
  (Phase 3) adds the interview log (`list[QaPair]`, question-then-answer in
  order), the outstanding `pending_question` and the synthesised `Dossier`.
  **Version 3** (Phase 4) adds the `portrait_sent` flag — set only after the
  generated hybrid portrait actually reached the chat. **Version 4** (Phase 5)
  adds the raw narration `script` (stored for the Phase 6 Narrator) and the
  `script_sent` flag — set only after the script actually reached the chat.
  **Version 5** (Phase 6) adds the raw narration audio `narration` (the MP3
  payload, kept like the photo bytes so a failed delivery re-sends it) and the
  `narrator_sent` flag — set only after the audio message actually reached the
  chat.
  In-memory only — the version bump is the schema contract, not a migration.
- Phases: `awaiting_photo` → `gate_passed` → `interviewing` → `done`. A
  `gate_passed` chat only survives a degraded interview start and is retried on
  the next update. A `done` chat with `portrait_sent == False` is "interview
  finished, portrait pending" — the Converter retries on the next update; a
  `done` chat with `portrait_sent == True` and `script_sent == False` is
  "portrait delivered, narration pending" — the Scripter retries on the next
  update; a `done` chat with `script_sent == True` and `narrator_sent == False`
  is "script delivered, audio message pending" — the Narrator retries (or
  re-sends the stored audio) on the next update; `portrait_sent`, `script_sent`
  and `narrator_sent` are stored, never inferred.
- One shared state driver (single module owns read/write/purge).
- **Reset semantics (Phase 7).** `/start` and `/restart` run one explicit
  routine — `bouncer.reset_chat(session, chat_id, *, update_id, log) ->
  PurgeReport` — that performs the three named actions without restarting the
  process:
  1. **Cancel active sessions.** The chat's state is replaced with a fresh
     default (`awaiting_photo`). The gateway is single-threaded, so the
     command is handled atomically and no interview turn can be in flight.
  2. **Purge ephemeral assets.** The in-memory `photo` and `narration` byte
     buffers are dropped (all media lives in memory only — there is no temp
     media on disk).
  3. **Re-initialize agent memory.** The interview log, `pending_question`,
     `dossier`, stored `script`, the `portrait_sent`/`script_sent`/
     `narrator_sent` flags and the media-group dedupe are cleared.

  The store method is `SessionStore.purge(chat_id) -> PurgeReport` (renamed
  from `reset`; no compatibility alias). `PurgeReport` is a pydantic model of
  flags and counts only — `chat_id`, `previous_phase`, `discarded_photo`,
  `discarded_narration`, `discarded_script`, `discarded_interview_answers`,
  `discarded_dossier`, `previous_media_group_id` — never media bytes or user
  text. Every `reset_chat` call emits exactly one `session_reset` INFO record
  built from the report (event + `update_id` + report fields), so every reset
  is auditable; the three stage modules' invariant-break `purge` calls (a
  `done` chat missing its artifacts) log their own `conversion_failed` /
  `script_failed` / `narrator_failed` ERROR instead, because they cannot
  import `bouncer.reset_chat` without an import cycle. Both reset sites — the
  start-command path and the gate-rejection path — call `reset_chat`; nothing
  else resets a chat.

## Testing

- **Red/Green TDD** — tests are written before code for every behaviour.
- Dev scripts live in `scripts/` (`scripts/test`, `scripts/hooks`) and are the
  ground truth for tests, lint, and type checks. Their exact usage is
  documented in the README.

## Repo hygiene

- `.env` is in `.gitignore` (verified) — secrets never enter the repo.
- Reproducible environment and pinned dependencies.
- The README documents developer-facing behaviour (how to run, test, and the
  long-polling model) and stays in sync with reality.