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
  adapters are direct **Gemini REST** callers (`generateContent`) over stdlib
  `urllib` with an injectable `urlopen` seam — the same no-third-party-HTTP
  pattern as the Phase 1 transport. The shared REST plumbing (endpoint build,
  POST, failure normalisation, candidate extraction, schema validation,
  inline-part decoding and the **single** API-key redaction implementation)
  lives once in `gemini.py` (`GeminiJsonClient` — `generate_json` for the JSON
  output of `vision.py`/`interviewer.py`/`scripter.py`, `generate_image` for the
  image output of Phase 4's `converter.py`, `generate_audio` for the audio
  output of Phase 6's `narrator.py`) and is reused by `vision.py`,
  `interviewer.py`, `converter.py`, `scripter.py` and `narrator.py`. The
  Telegram transport owns a multipart `sendPhoto` (`transport.py`) for
  delivering the generated portrait and a multipart `sendVoice` for delivering
  the OGG voice note.
  **Google Agent Development Kit (ADK) is deferred**; if a later phase adopts
  it, it will wrap these same ports.
- **Models:**
  - The Bouncer — vision gate — **Gemini 3.1 Flash Lite**.
  - The Interviewer — sequential Q&A + orchestrator — **Gemini 3.1 Flash Lite**.
  - The Converter — hybrid portrait — **Gemini 3.1 Flash Image**.
  - The Scripter — narration prose — **Gemini 3.1 Flash Lite**.
  - The Narrator — **not an agent** — Gemini TTS
    (`gemini-3.1-flash-tts-preview`), which returns **OGG (Opus)** natively —
    the format Telegram `sendVoice` accepts.
- **Secrets:** `TELEGRAM_BOT_TOKEN` and `GEMINI_API_KEY` from `.env`. The
  `.env` file is gitignored and must never be committed.

## Architecture

- **Hub-and-spoke (module level):** the **Bouncer is the router** across
  every stage — it dispatches on phase: a passing portrait hands off to the
  Interviewer, a `done` chat without a portrait runs the Converter, and when an
  Interviewer turn *completes* the interview the Bouncer runs the same-update
  Converter handoff itself (hub-and-spoke: neither the Interviewer nor the
  Converter knows the other). The handoff chain is **dossier → portrait →
  script → voice note in one update**: once the converted portrait lands, the
  same update continues into the Scripter, and once the script lands it
  continues into the Narrator, so the narration script and the playable voice
  note follow the portrait with **no user input** in between; the raw script
  and the raw OGG audio are stored on the session. Each pipeline stage is a
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
  stored OGG bytes LLM-free, so one script always yields one voice note).
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

## Session state

- Versioned, per-`chat_id` schema with an explicit phase field. **Version 2**
  (Phase 3) adds the interview log (`list[QaPair]`, question-then-answer in
  order), the outstanding `pending_question` and the synthesised `Dossier`.
  **Version 3** (Phase 4) adds the `portrait_sent` flag — set only after the
  generated hybrid portrait actually reached the chat. **Version 4** (Phase 5)
  adds the raw narration `script` (stored for the Phase 6 Narrator) and the
  `script_sent` flag — set only after the script actually reached the chat.
  **Version 5** (Phase 6) adds the raw narration audio `narration` (the OGG
  payload, kept like the photo bytes so a failed delivery re-sends it) and the
  `narrator_sent` flag — set only after the voice note actually reached the
  chat.
  In-memory only — the version bump is the schema contract, not a migration.
- Phases: `awaiting_photo` → `gate_passed` → `interviewing` → `done`. A
  `gate_passed` chat only survives a degraded interview start and is retried on
  the next update. A `done` chat with `portrait_sent == False` is "interview
  finished, portrait pending" — the Converter retries on the next update; a
  `done` chat with `portrait_sent == True` and `script_sent == False` is
  "portrait delivered, narration pending" — the Scripter retries on the next
  update; a `done` chat with `script_sent == True` and `narrator_sent == False`
  is "script delivered, voice note pending" — the Narrator retries (or re-sends
  the stored audio) on the next update; `portrait_sent`, `script_sent` and
  `narrator_sent` are stored, never inferred.
- One shared state driver (single module owns read/write/reset).
- Reset semantics: `/start` and `/restart` clear state, the interview log, the
  pending question, the dossier, the `portrait_sent`, `script_sent` and
  `narrator_sent` flags, the stored `script` and `narration`, and temp media
  without restarting the process.

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