# Tech — Technical Contract

This file is the technical contract for **The Telegram Documentaries**. Every
agent and every review is judged against it.

## Stack

- **Language:** Python 3.
- **Transport:** Telegram Bot API over **long polling** (`getUpdates`). No
  webhooks, no public URL.
- **Framework:** **No vendor agent framework as of Phase 2.** Each pipeline
  stage is a discrete module behind a narrow typed port. The Bouncer exposes a
  typed `VisionGate` port whose Phase 2 adapter is a direct **Gemini REST**
  caller (`generateContent` with a JSON `responseSchema`) over stdlib
  `urllib` with an injectable `urlopen` seam — the same no-third-party-HTTP
  pattern as the Phase 1 transport. **Google Agent Development Kit (ADK) is
  deferred**; if a later phase adopts it, it will wrap these same ports.
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
  ports**, not vendor SDKs: e.g. the Bouncer is `VisionGate.classify(image) ->
  HumanVerdict`, so the Gemini REST adapter (Phase 2) can later be swapped for
  an ADK agent without touching the routing code.
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

- Versioned, per-`chat_id` schema with an explicit phase field.
- One shared state driver (single module owns read/write/reset).
- Reset semantics: `/start` and `/restart` clear state and temp media without
  restarting the process.

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