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

## 5. The Scripter

- Gemini 3.1 Flash Lite: one-paragraph (~60–90 words) dramatic
  British-documentary narration in English, built from the dossier.

**Acceptance:** narration reads as a documentary voice-over, uses the dossier,
lands within 60–90 words. *Serves: the "documentary" voice before audio.*

## 6. The Narrator

- Script routed directly to Gemini TTS (`gemini-3.1-flash-tts-preview`).
- Render to a Telegram-compatible audio format (OGG/MP3) and send to chat.

**Acceptance:** a playable voice note arrives in chat after the portrait,
speaking the Scripter's words. *Serves: the final narrated deliverable.*

## 7. Resilience

- `/start` and `/restart`: purge session state **and** temporary media without
  restarting the process.
- Wrong-payload-at-wrong-stage guards across all phases.
- API timeout / transient-failure fallbacks that degrade gracefully.

**Acceptance:** reset mid-interview clears everything; misplaced photos/text at
any stage are re-prompted; Gemini/TTS failures never kill the conversation.
*Serves: the reset and graceful-handling rubric criteria.*