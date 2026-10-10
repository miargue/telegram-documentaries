# Tech — Technical Contract

This file is the technical contract for **The Telegram Documentaries**. Every
agent and every review is judged against it.

## Stack

- **Language:** Python 3.
- **Transport:** Telegram Bot API over **long polling** (`getUpdates`). No
  webhooks, no public URL.
- **Framework:** **No vendor agent framework as of Phase 3.** Each pipeline
  stage is a discrete module behind a narrow typed port. The Bouncer exposes a
  typed `VisionGate` port and the Interviewer a typed `InterviewLLM` port
  (`next_question` / `summarize`); both Phase 3 adapters are direct **Gemini
  REST** callers (`generateContent` with a JSON `responseSchema`) over stdlib
  `urllib` with an injectable `urlopen` seam — the same no-third-party-HTTP
  pattern as the Phase 1 transport. The shared REST plumbing (endpoint build,
  POST, failure normalisation, candidate extraction, schema validation and the
  **single** API-key redaction implementation) lives once in
  `gemini.py` (`GeminiJsonClient`), reused by `vision.py` and `interviewer.py`.
  **Google Agent Development Kit (ADK) is deferred**; if a later phase adopts
  it, it will wrap these same ports.
- **Models:**
  - The Bouncer — vision gate — **Gemini 3.1 Flash Lite**.
  - The Interviewer — sequential Q&A + orchestrator — **Gemini 3.1 Flash Lite**.
  - The Converter — hybrid portrait — **Gemini 3.1 Flash Image**.
  - The Scripter — narration prose — **Gemini 3.1 Flash Lite**.
  - The Narrator — **not an agent** — Gemini TTS
    (`gemini-3.1-flash-tts-preview`), output rendered to a
    Telegram-compatible audio format (OGG/MP3).
- **Secrets:** `TELEGRAM_BOT_TOKEN` and `GEMINI_API_KEY` from `.env`. The
  `.env` file is gitignored and must never be committed.

## Architecture

- **Hub-and-spoke (module level):** the Interviewer is the **orchestrator**.
  Each pipeline stage is a discrete module (Bouncer, Interviewer, Converter,
  Scripter) plus the non-agent TTS renderer. Stage boundaries are **typed
  ports**, not vendor SDKs: the Bouncer is `VisionGate.classify(image) ->
  HumanVerdict` and the Interviewer is `InterviewLLM.next_question(...) ->
  str` / `summarize(...) -> Dossier`, so the Gemini REST adapters can later be
  swapped for ADK agents without touching the routing code. The Interviewer
  owns its own turn-taking (the phase machine, the exactly-5 cap and the
  one-question-per-message rule); `InterviewLLM` merely supplies one validated
  question or the final dossier at a time.
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
  In-memory only — the version bump is the schema contract, not a migration.
- Phases: `awaiting_photo` → `gate_passed` → `interviewing` → `done`. A
  `gate_passed` chat only survives a degraded interview start and is retried on
  the next update.
- One shared state driver (single module owns read/write/reset).
- Reset semantics: `/start` and `/restart` clear state, the interview log, the
  pending question, the dossier and temp media without restarting the process.

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