# Requirements — The Bouncer (Phase 2)

## Context

- **Phase 1 is merged to `main`** (PR #1): a Telegram long-polling gateway that
  replies `hey mate!` to every incoming message (`telegram_documentaries/echo.py`).
- **This is Phase 2** from `SPECS/ROADMAP.md`: the first AI stage and the first
  real session state. It replaces the unconditional echo with a vision gate.
- **Branch:** `feature/2026-10-09-bouncer` (created off `main`).
- Model per `SPECS/TECH.md`: **Gemini 3.1 Flash Lite**.

## Goal

When a user sends a portrait, confirm a **human is present** using Gemini vision.

- Human present → accept, keep the photo for later stages, acknowledge, and move
  the session to a `gate_passed` phase (ready for the Phase 3 Interviewer).
- No human (car, pet, food, landscape, empty image, …) → send a **cheeky
  rejection** and **reset** the conversation.
- Text or any other content arriving while a photo is expected → **re-prompt**,
  never crash, never lose the session.

## Decisions locked with the user

1. **Gemini integration** — define our **own typed `VisionGate` port** with a
   Gemini-backed adapter sitting behind an **injectable `urlopen` seam** (stdlib
   `urllib`, matching Phase 1's no-third-party-HTTP pattern). Tests use a fake.
   *Full Google ADK adoption is deferred.* → **Spec drift:** `TECH.md` currently
   says "Framework: Google ADK". This phase does **not** use ADK; reconciliation
   of `TECH.md` is handled at verification with user approval.
2. **Session state** — introduce a **minimal versioned shared driver**
   (`session.py`) with an explicit phase enum containing only the phases Phase 2
   needs. Later phases extend it.
3. **Photo input** — accept Telegram **compressed photos** (`message.photo`),
   download the **largest** size via `getFile`. An **album/media group proceeds
   with the first photo**. Image documents and stickers are not accepted
   (they get the re-prompt).
4. **Echo removed** — `/start` and any text prompt for a portrait; a passing
   portrait gets a short success message and advances the phase.

## Scope (in)

- **`VisionGate` port** + **`GeminiVisionGate` adapter** returning a typed
  `HumanVerdict` (`is_human: bool`, `reason`), using Gemini **structured output**
  (`responseSchema`/`responseMimeType=application/json`) — schema, not regex.
- **Photo intake**: pick the largest `PhotoSize`, `getFile`, download bytes
  through the transport; validate the Telegram-returned `file_path` as a safe
  relative path before building a URL.
- **Message routing** (replaces `echo.py`): `/start` (and `/restart` treated the
  same for now) resets and prompts; text re-prompts; photo runs the gate; any
  other content re-prompts.
- **Session driver**: `SessionState` (`version`, `phase`, `photo` bytes) keyed by
  `chat_id`, with `get` / `reset` / `record_pass`; reset purges the stored photo.
- **Graceful degradation** on gate/download/send failure: log loudly with
  traceback, send an apology/re-prompt, leave the session usable, never raise
  into the polling loop.
- **Config**: resolve `GEMINI_API_KEY` (env wins over `.env`, like the token);
  missing key is a startup `ConfigError` (exit `2`). Optional
  `GEMINI_VISION_MODEL` override with default `gemini-3.1-flash-lite`.
- Comprehensive **structured logging** via decorators/boundaries: gate start /
  verdict / rejection / pass, photo fetch failures, vision failures. Never log
  raw image bytes or secrets.

## Out of scope (YAGNI)

- Interviewer, Converter, Scripter, TTS (Phases 3–6).
- Full `/restart` semantics and cross-phase temporary-media purge (Phase 7) —
  only the reset needed for rejection/`/start` is in scope here.
- Retry/backoff/timeout policy for Gemini (Phase 7) — graceful single-attempt
  degradation is in scope, resilience is not.
- Image **documents**, stickers, voice, video; album uploads beyond "first photo".
- Any long-term storage, webhooks, or public URL.

## Contracts at boundaries (typed, untrusted input)

| Boundary | Type | Failure behaviour |
| --- | --- | --- |
| Telegram `PhotoSize` | `PhotoSize` (Pydantic) | missing/typed-wrong → re-prompt, log |
| Telegram `getFile` result | `FileRef` (Pydantic) | invalid/missing `file_path` → degrade, log |
| Gemini response | `HumanVerdict` (Pydantic, structured output) | malformed/`ok=false` → `VisionError`, degrade |
| Session | `SessionState` per `chat_id` | unknown chat → fresh default state |
| Downloaded bytes | `bytes` | empty/non-image → treat as gate failure, degrade |

`chat_id` stays an `int` end-to-end (Phase 1 model) — the session key must not
silently stringify.

## Non-negotiables

- **Per-`chat_id` isolation** — never leak one user's session/photo to another.
- **Never log or commit secrets** (`TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`).
- **No test contacts Telegram or Gemini** — fakes only.

## Acceptance criteria (from ROADMAP Phase 2)

1. A portrait **passes** the gate and the session reaches `gate_passed` with the
   photo retained.
2. An animal / landscape / empty image is **rejected** with a cheeky message and
   the session is **reset** (phase back to `awaiting_photo`, photo purged).
3. Text-only input while a photo is expected is handled gracefully (re-prompt),
   with no crash and no lost session.
4. `scripts/test` and `scripts/hooks` are green.
