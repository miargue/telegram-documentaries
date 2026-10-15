# Roadmap — Build Order

One phase per pipeline capability. A phase is **complete** only when verified
through `scripts/test` / `scripts/hooks` and its acceptance criteria are met.

## 1. Repository & gateway

- Project skeleton (Python package layout, `.env` support, pinning).
- Long-polling loop via `getUpdates` that echoes a hardcoded reply.
- `scripts/test` and `scripts/hooks` exist and run green.

**Acceptance:** starting the bot shows it online; any message is echoed back;
tests pass. *Serves: whole pipeline groundwork.*

## 2. The Bouncer

- Gemini 3.1 Flash Lite vision gate: is a human present?
- Non-human / no-human images get a cheeky rejection and state reset.
- Text arriving while waiting for a photo is re-prompted, not crashed.

**Acceptance:** portrait passes; animal/landscape/empty image is rejected with
a reset; a human appears with text-only input handled gracefully. *Serves: gate
before any interview work.*

**Status: complete (verified 2026-10-10, branch `feature/2026-10-09-bouncer`).**
Delivered: typed `VisionGate` port + Gemini REST adapter; `session.py` state
driver; `media.py` photo intake; `bouncer.py` routing (replaces `echo.py`);
`GEMINI_API_KEY` / optional `GEMINI_VISION_MODEL` config; graceful degradation.
`scripts/test` (244 passed) and `scripts/hooks` green; no test touches the
network. Full evidence in `SPECS/2026-10-09-bouncer/validation.md`. Framework
note: the shipped integration is a typed port over stdlib `urllib`, **not**
Google ADK (deferred — see `SPECS/TECH.md`).

## 3. The Interviewer

- Sequential stateful Q&A: **exactly 5** questions, one at a time, state keyed
  by `chat_id`. (Reconciled from "5–7" to the user-locked exactly-5 decision of
  2026-10-10 — see `SPECS/2026-10-10-interviewer/requirements.md` decision 3.)
- Builds a behavioural dossier; outputs a suggested animal.

**Acceptance:** full questionnaire completes in order with a dossier summary
and animal suggestion; answers out of order do not corrupt the session.
*Serves: the interview experience and dossier for Converter + Scripter.*

**Status: complete (verified 2026-10-10, branch
`feature/2026-10-10-interviewer`).** Delivered: typed `InterviewLLM` port +
`GeminiInterviewer` Gemini REST adapter over a shared `GeminiJsonClient`
(extracted from `vision.py`; single API-key redaction); the Interviewer owns
its turn-taking (exactly-5 cap, one question per message, stored pending
question); `SessionState` v2 carries the interview log, pending question and
`Dossier`; automatic handoff after the Bouncer pass (success line then Q1);
optional `GEMINI_INTERVIEW_MODEL`. `scripts/test` (367 passed) and
`scripts/hooks` green; no test touches the network. Full evidence in
`SPECS/2026-10-10-interviewer/validation.md`. Framework note: the shipped
integration is a typed port over stdlib `urllib` sharing one Gemini JSON
client, **not** Google ADK (still deferred — see `SPECS/TECH.md`).

## 4. The Converter

- Gemini 3.1 Flash Image: native multimodal fusion of original photo +
  interview dossier into a hybrid animal portrait.
- Result delivered directly as a Telegram photo.

**Acceptance:** a new hybrid portrait image arrives in chat, derived from the
user's photo and dossier. *Serves: keeps the single-photo in-memory contract.*

**Status: complete (verified 2026-10-11, branch `feature/2026-10-10-converter`).**
Delivered: typed `ConverterLLM` port + `GeminiConverter` Gemini REST image
adapter over the shared `GeminiJsonClient` (new `generate_image`: one
multimodal `generateContent` call — photo bytes + dossier text in a single
`user` turn, persona in `systemInstruction`, `responseModalities: ["IMAGE"]`,
no intermediate text step); multipart `sendPhoto` in the transport (pure
`_multipart` helper, stable filename, shared redaction); `SESSION_VERSION` 3
with the stored `portrait_sent` flag; the Bouncer routes `done && !portrait_sent`
to the Converter and runs the same-update handoff after the interview
completes; optional `GEMINI_CONVERTER_MODEL` / `GEMINI_CONVERTER_TIMEOUT` (120 s
default). `scripts/test` (483 passed) and `scripts/hooks` green; no test
touches the network (full suite re-run with every socket entry point patched to
raise, patch sanity-checked). Live Gemini smoke: **not reached** — the
configured `GEMINI_API_KEY` is blocked provider-side ("API key was reported as
leaked", HTTP 403, reproduced with a bare list-models call), so no real image
bytes were observed; not an adapter failure (per the approved classification
only HTTP-400/malformed output is a code failure). Full evidence in
`SPECS/2026-10-10-converter/validation.md`. Framework note: the shipped
integration is a typed port over stdlib `urllib`, **not** Google ADK (still
deferred — see `SPECS/TECH.md`).

## 5. The Scripter

- Gemini 3.1 Flash Lite: one-paragraph (~60–90 words) dramatic
  British-documentary narration in English, built from the dossier.

**Acceptance:** narration reads as a documentary voice-over, uses the dossier,
lands within 60–90 words. *Serves: the "documentary" voice before audio.*

**Status: complete (verified 2026-10-12, branch `feature/2026-10-11-scripter`).**
Delivered: typed `ScripterLLM` port + `GeminiScripter` Gemini REST text adapter
over the shared `GeminiJsonClient` (persona only in `systemInstruction`, one
`user` content citing the dossier, `responseSchema` `{"script": str}`,
mechanical output contract — non-empty / single paragraph / ≤ 90 words);
`SESSION_VERSION` 4 with stored `script` (raw, ready for Phase 6) and the
`script_sent` flag; the Bouncer runs the Scripter in the **same update** after
the portrait lands (fresh completion and retried-portrait paths), re-sends the
stored script LLM-free on a failed delivery, and keeps the light reply after
`script_sent`; optional `GEMINI_SCRIPT_MODEL`. `scripts/test` (**560 passed**,
up from 483) and `scripts/hooks` green; no test touches the network (full suite
re-run with every socket entry point patched to raise, patch sanity-checked).
Live Gemini smoke: **not reached — the configured `GEMINI_API_KEY` is
UUID-shaped (not an `AIza…` key) and provider-blocked, so no real narration was
observed; not an adapter failure.** Full evidence in
`SPECS/2026-10-11-scripter/validation.md`. Framework note: the shipped
integration is a typed port over stdlib `urllib`, **not** Google ADK (still
deferred — see `SPECS/TECH.md`).

## 6. The Narrator

- Script routed directly to Gemini TTS via the **Interactions API**
  (`gemini-3.8-flash-lite-tts`; prebuilt voice `en-gb-storyteller-4`).
- Render to a Telegram-compatible audio format (**MP3**) and send to chat as an
  audio message.

**Acceptance:** a playable audio message arrives in chat after the portrait,
speaking the Scripter's words. *Serves: the final narrated deliverable.*

**Status: complete (verified 2026-10-15, Revision R1, branch
`feature/2026-10-12-narrator`).** Delivered: typed `NarratorLLM` port +
`GeminiNarrator` Interactions-API TTS adapter over the shared
`GeminiJsonClient` (new `generate_interaction_audio`: `POST /v1beta/interactions`
with the model in the body, one `user_input` text content holding the raw script
**verbatim**, the fixed delivery style as a `speech_metadata` annotation — never
prepended — and the voice in `generation_config.speech_config`; response is
`{"type": "audio", "mime_type": "audio/mp3"}` decoded from `steps[0].content[]`;
the payload is validated mechanically — non-empty, `audio/mp3*`/`audio/mpeg*`,
≤ `MAX_NARRATION_BYTES` (5 MiB)); multipart `sendAudio` in the transport (pure
`_multipart` helper, file field `audio`, filename `narration.mp3`, shared token
redaction); `SESSION_VERSION` 5 with the stored `narration` bytes and the
`narrator_sent` flag; the Bouncer runs the Narrator in the **same update** after
the script lands (fresh completion and retried path), re-sends the stored audio
LLM-free on a failed delivery, and keeps the light reply after `narrator_sent`;
optional `GEMINI_NARRATOR_MODEL` / `GEMINI_NARRATOR_VOICE` /
`GEMINI_NARRATOR_TIMEOUT` (90 s default). `scripts/test` (**697 passed**, up from
560) and `scripts/hooks` green; no test touches the network (full suite re-run
with every socket entry point patched to raise, patch sanity-checked). Live
Gemini smoke: **PASS** — `audio/mp3; rate=24000; channels=1`, 339235 bytes,
valid ID3v2 header, written to `/tmp/opencode/narration_smoke.mp3` (key never
printed). Full evidence in `SPECS/2026-10-12-narrator/validation.md`.

**Revision R1 (2026-10-14).** The original pre-revision design (OGG/Opus via
`gemini-3.1-flash-tts-preview` + `sendVoice`) was proven **impossible against
the live API** — no Gemini TTS endpoint returns OGG/Opus without an encoder —
and was replaced by the MP3/`sendAudio` design above. The `generateContent`
audio path and `sendVoice` are gone. Framework note: the shipped integration is
a typed port over stdlib `urllib`, **not** Google ADK (still deferred — see
`SPECS/TECH.md`).

## 7. Resilience

- `/start` and `/restart`: run an **explicit reset routine** that performs three
  named actions — (1) **cancel active sessions** for the requesting `chat_id`,
  (2) **purge ephemeral assets**, and (3) **re-initialize agent memory** back to
  the greeting stage — without restarting the process. It must fire in any case,
  with or without an `@botname` suffix, and whether the command arrives as a
  message or a **photo caption**. The routine logs exactly what it discarded.
- Wrong-payload-at-wrong-stage guards across **all** phases (a phase ×
  content-type matrix), not just the reported case.
- API timeout / transient-failure fallbacks that degrade gracefully: a transient
  `getUpdates` failure must never kill the bot.

**Decisions locked with the user (2026-10-15):**

- **"Purge ephemeral assets" = clear the in-memory buffers.** All media (the
  photo and the MP3 narration) lives in memory only — there is no temp media on
  disk (see `MISSION.md` "No long-term storage"). The reset drops those buffers;
  it does not add on-disk files.
- **"Cancel active sessions" = reset the per-chat state.** The gateway is one
  synchronous long-polling loop with no per-chat threads, so a `/start` is
  handled atomically and no in-flight interview turn survives it.
- **Keep the gateway single-threaded.** The slow Converter (~120 s) stays on the
  polling loop; every stage call is bounded by its own timeout instead of moving
  work off-loop.
- **Transient `getUpdates` failures retry with capped exponential backoff and
  the bot stays alive; permanent coded errors surface.** An `OSError` or a
  transient `TelegramApiError` (no code, `429` or `>= 500`) is retried forever;
  a permanent coded 4xx (e.g. `401` revoked token, `409` duplicate poller)
  propagates so the process exits `1`. The startup `getMe` probe still surfaces
  a bad token.

**Acceptance:** reset mid-interview clears everything; misplaced photos/text at
any stage are re-prompted; Gemini/TTS failures never kill the conversation.
*Serves: the reset and graceful-handling rubric criteria.*

**Status: complete (verified 2026-10-15, branch
`feature/2026-10-15-resilience`; spec `SPECS/2026-10-15-resilience/`).
Most of the surface already existed (the design already purges on reset and
degrades on every stage failure), so the phase is a formalize/harden/verify
pass that delivers:

- **The explicit reset routine.** `bouncer.reset_chat(session, chat_id, *,
  update_id, log) -> PurgeReport` is the single named routine, called by
  **both** reset sites (the start-command path and the gate-rejection path).
  The three named actions map onto this architecture as: (1) *cancel active
  sessions* -> the per-chat state is replaced with a fresh `awaiting_photo`
  default (single-threaded loop, so the command is handled atomically and no
  in-flight interview turn survives); (2) *purge ephemeral assets* ->
  `SessionStore.purge(chat_id)` drops the in-memory `photo` / `narration`
  byte buffers (there is no temp media on disk); (3) *re-initialize agent
  memory* -> the interview log, `pending_question`, `dossier`, stored
  `script`, the `portrait_sent` / `script_sent` / `narrator_sent` flags and
  the media-group dedupe are cleared. Every call emits exactly one structured
  `session_reset` INFO event built from the `PurgeReport` (flags and counts
  only — never media bytes or user text). `SessionStore.reset` was **removed**
  in favour of `purge(chat_id) -> PurgeReport` (a rename, no compatibility
  alias); the three invariant-break `purge` calls in converter/scripter/
  narrator log their own `conversion_failed` / `script_failed` /
  `narrator_failed` ERROR instead (they cannot import `bouncer.reset_chat`
  without an import cycle).
- **Command recognition.** `_command()` reads `text` **or** `caption`, takes
  the first whitespace-delimited token, strips `@botname` and lowercases — so
  `/Start`, `/START`, `/ReStart`, `/start@MiargueBot`, `/restart now` and a
  photo captioned `/start` all reset at **every** phase, and the captioned
  photo is never gated. A photo with a non-command caption is gated as
  normal; a non-command slash text (e.g. `/help`) just re-prompts.
- **A phase × content-type guard matrix** (61 parametrized cells): text /
  empty text / sticker / voice / document / photo / non-command caption /
  album sibling / messageless / command / command-caption across
  `awaiting_photo`, `gate_passed`, `interviewing` and all four `done`
  variants — each asserting the narrow documented outcome (re-prompt, re-ask
  the stored question, benign `SKIPPED`, the documented retry path, or the
  light-completion reply), with no exception ever escaping `handle_update`.
- **Transient `getUpdates` retry.** `_get_updates()` retries **only
  transient failures** — an `OSError`, or a `TelegramApiError` with no code,
  `429` or `>= 500` — forever with capped exponential backoff (`1.0` doubling
  to a `30.0 s` cap; one `get_updates_retry` record and one injected `sleep`
  per attempt), while a **permanent** coded 4xx (`400`/`401`/`403`/`404`/
  `409`) propagates out of `poll_once` so `cli.main` logs `gateway_failed`
  and exits `1`. `run_polling` forwards the injectable `sleep`, and the CLI
  now rides a transient blip out (exit `0`) instead of dying.

Verification: `scripts/test` — **802 passed** (up from the 697 baseline; 117
new tests, and 12 pre-existing tests deliberately renamed/replaced for the
`reset -> purge` and retry contracts) — and `scripts/hooks` green; no test
touches the network (full suite re-run with every socket entry point patched
to raise, patch sanity-checked). Full evidence, including the deviations
list, in `SPECS/2026-10-15-resilience/validation.md`.
