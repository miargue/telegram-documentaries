# Requirements — The Narrator (Phase 6)

## Context

- **Phase 5 (The Scripter) is merged to `main`** (PR #6): the bot now ends a
  run with a dramatic 60–90-word narration script, sent to the chat right
  after the hybrid portrait and stored on the session (`SessionState.script`)
  — "a documentary script sitting in a text bubble," exactly as this phase
  says. `scripts/test` = 560 passed, `scripts/hooks` green, no test touches
  the network.
- **This is Phase 6** from `SPECS/ROADMAP.md`: "*The Narrator* — Script
  routed directly to Gemini TTS (`gemini-3.1-flash-tts-preview`). Render to
  a Telegram-compatible audio format (OGG/MP3) and send to chat. Acceptance:
  a playable voice note arrives in chat after the portrait, speaking the
  Scripter's words."
- **Branch:** `feature/2026-10-12-narrator` (created off `main` at
  `a57d863`).
- This phase is **purely transformational** (the user's own framing): the
  Scripter already chose tone, vocabulary and comedic pacing; the Narrator is
  **not another reasoning agent** — text in, audio stream out, a direct
  synthesis call to `gemini-3.1-flash-tts-preview`.

## Goal

Turn the stored narration script into a **playable voice note in the chat**:
route the raw script to Gemini TTS (a direct REST call — no ADK, no Gemini
Python SDK, no new dependencies), receive the binary audio payload, keep it
briefly in the session (the in-memory equivalent of "persist to a temporary
audio file" — the same handling as the portrait photo bytes), and deliver it
via Telegram `sendVoice` as a Telegram-compatible audio format. The user
hears the documentary.

## Decisions locked with the user (2026-10-12)

1. **Format & delivery — OGG voice note via `sendVoice`.** `gemini-3.1-flash-tts-preview`
   returns audio natively as **OGG (Opus codec)** (`audio/ogg; codecs=opus`),
   the format Telegram accepts natively for `sendVoice` — so **no converter
   dependency** is needed and "`pydantic` is the only runtime dependency"
   stays true (MP3 would force an encoder library in). A `sendVoice` voice
   note matches ROADMAP wording and the documentary vibe.
2. **Voice-over instruction — fixed constant + optional env override.** The
   persona ("deep male voice, posh British accent, revered wildlife
   documentary narrator") is a constant default, prepended to the script
   inside the TTS request text so the model reads in that voice; an optional
   `GEMINI_NARRATOR_VOICE` override (env-beats-file, like
   `GEMINI_SCRIPT_MODEL`) lets the user experiment with voices without
   editing code.
3. **Timing & retry — same-update, stored, once-only.** The voice note
   follows the script message in the **same update** (no user input), exactly
   once: the raw audio bytes are stored on the session (v5) so a failed
   delivery re-sends the **stored bytes** with **no re-synthesis** (the
   re-ask pattern); a failed synthesis apologises and retries on the next
   update. Mirrors Phase 5 exactly.
4. **Verification — component + conditional live smoke.** Same established
   pattern: in-process component tests prove portrait → script → voice note
   with fakes; a live Gemini smoke runs only if a working `GEMINI_API_KEY`
   exists (the configured key is currently UUID-shaped and provider-blocked,
   so "not reached" is recorded honestly). The "test the sequence on your
   phone" step remains a documented manual run.

## Scope (in)

- **`gemini.py`** — `AudioPayload` model (`data: bytes`, `mime_type: str`)
  and `GeminiJsonClient.generate_audio(request_body) -> AudioPayload`:
  identical POST/failure-normalisation/redaction path as `generate_image`;
  the candidate extraction differs only in reading the first **audio**
  `inlineData` part (strict base64 decode like the image, model-declared
  `mimeType`). Requirement on the payload: `mime_type` starts with
  `audio/` and `data` is non-empty and ≤ `MAX_NARRATION_BYTES` — otherwise
  `NarratorError` (unusable output).
- **`narrator.py` (new; mirrors `scripter.py` structure):**
  - `NarratorError(Exception)`; `NarratorLLM` (Protocol):
    `synthesize(script: str) -> AudioPayload`.
  - `GeminiNarrator(api_key, *, model=DEFAULT_NARRATOR_MODEL, voice=…,
    base_url=…, timeout=…, urlopen=…)` over `GeminiJsonClient.generate_audio`.
  - Request body: **one** `user` content holding **two text parts** — the
    voice-over guidance (the posh-British deep-voice persona) and the **raw
    script verbatim** (`parts: [{"text": voice}, {"text": script}]`) — and
    `generationConfig: {"responseModalities": ["AUDIO"]}`. The guidance is a
    prompt instruction, not a new system-agent; the script is the payload.
  - `NARRATOR_VOICE` constant in `defaults.py` (the "have fun" persona:
    deep, confident male voice; posh British accent; measured, warm
    wildlife-documentary delivery).
- **Session** (`session.py`): `SESSION_VERSION = 5`; `SessionState.narration:
  bytes | None = None` (the raw OGG audio payload, kept like the photo bytes)
  and `narrator_sent: bool = False`; `store_narration(chat_id, audio)` +
  `mark_narrator_sent(chat_id)`; `reset` clears both.
- **Transport** (`transport.py`): `send_voice(chat_id, audio, *,
  media_type, filename="narration.ogg")` — multipart upload to `sendVoice`
  through the existing pure `_multipart` helper (file field `voice`), same
  `_read`/`_decode`/token-redaction path. `Transport` protocol gains
  `send_voice`.
- **Routing** (`narrator.py`): `handle_narrating(transport, chat_id, *,
  session, narrator, logger)`:
  - Already-stored narration not yet sent → **re-send the stored bytes**
    (no `synthesize` call), then `mark_narrator_sent` on success.
  - Otherwise: require the stored script (missing → fail loud + reset-and-
    reprompt) → `narration_started` → `synthesize` → validate payload →
    `store_narration` → `send_voice` → `mark_narrator_sent` (delayed commit:
    the flag moves only after the note actually reached the chat).
  - Failures (synthesis or send) → apology, `narrator_failed`, no flag,
    retryable next update; nothing raises into the polling loop
    (`KeyboardInterrupt`/`SystemExit` exempt).
- **Bouncer dispatch** (`bouncer.py`): after scripting completes, run the
  Narrator **in the same update**; an update arriving in `done &&
  script_sent && !narrator_sent` re-drives narrating (retry/re-send);
  `done && narrator_sent` keeps the Interviewer's light reply.
- **Config** (`config.py` + `defaults.py`): `DEFAULT_NARRATOR_MODEL =
  "gemini-3.1-flash-tts-preview"`, `DEFAULT_NARRATOR_VOICE` (the persona
  string), `DEFAULT_NARRATOR_TIMEOUT = 90.0` (TTS is slow, like image
  generation — same rationale as the Converter), `MAX_NARRATION_BYTES =
  5 * 1024 * 1024` (oversized model output degrades instead of a rejected
  upload); `get_narrator_model()`, `get_narrator_voice()` and
  `get_narrator_timeout()` all env-beats-file with validation (model: blank →
  default; timeout: must be a positive, finite number — hard startup error
  otherwise, mirroring the Converter).
- **Wiring** (`polling.py`, `cli.py`): build one `GeminiNarrator` and thread
  `narrator` through the poll loop and handler.
- **Structured logging**: `narration_started`, `narrator_ready` (byte count +
  mime only — never the script, never the audio), `narration_sent`,
  `narrator_failed`, `narration_skipped`. The audio bytes are never logged.

## Out of scope (YAGNI)

- No duration/file-length estimation (we do not parse OGG); the 90-word
  script cap already bounds the note to a comfortable length.
- No caption on the voice note (`sendVoice` supports captions; the script
  message right before it is already the readable version — YAGNI).
- No `sendAudio`/MP3 path (locked — OGG via `sendVoice` only).
- No new TTS agent/framework — a direct REST synthesis call by design.

## Contracts at boundaries (typed, untrusted input)

| Boundary | Type | Failure behaviour |
| --- | --- | --- |
| Gemini `synthesize` output | `{"inlineData": {mimeType, data}}` pydantic-validated | HTTP/network/protocol/malformed → redacted `NarratorError` from None, apology, retry next update |
| Audio payload validation | `mime_type.startswith("audio/")`, non-empty, ≤ `MAX_NARRATION_BYTES` | violation → `NarratorError` (unusable output), retry next update |
| `sendVoice` upload | multipart via `_multipart`, filename `narration.ogg` | failure → apology, stored bytes re-sent next update (no re-synthesis) |
| Session | `SessionState` v5 (`narration: bytes`, `narrator_sent`) | unknown chat → fresh default; wrong type → `ValidationError` |
| Script input | stored v4 `script` (validated prose) | missing → fail loud + reset-and-reprompt |

`chat_id` stays `int` end-to-end. The narration is **synthesised once and
sent once** (`narrator_sent` set only after a successful `sendVoice`).

## Non-negotiables

- **Per-`chat_id` isolation** — one user's audio/script never leaks into
  another's session.
- **Never log or commit secrets** — the single-redaction guarantee extends to
  a fifth adapter; audio bytes and the script are never written to logs.
- **No test contacts Telegram or Gemini** — fakes only (the opt-in live smoke
  in `validation.md` is the sole exception, and only with a working key).
- **The narration is delivered exactly once** and is **Telegram-playable
  audio** (OGG/Opus as produced by the model — the format the platform
  accepts for `sendVoice`).
- **Fail loud, degrade gracefully, never wedge** — a `done` chat missing its
  voice note is always retryable.

## Acceptance criteria (from ROADMAP Phase 6)

1. A playable voice note arrives in the chat **after the script**, speaking
   the Scripter's words.
2. The narration is delivered **exactly once**, generated from the **stored**
   script (never improvised), and a failed delivery re-sends the stored
   audio.
3. `scripts/test` and `scripts/hooks` are green, including all 560 pre-
   existing tests.

## Delivered (filled at verification)

**Delivered (verified 2026-10-14, branch `feature/2026-10-12-narrator`).**

- `gemini.py`: `AudioPayload` (`data: bytes`, `mime_type: str`) and
  `GeminiJsonClient.generate_audio` — same POST/normalisation/redaction path as
  `generate_image`, reading the first audio `inlineData` part; strict base64
  decode, model-declared mime. `NarratorError` on unusable output.
- `narrator.py`: `NarratorError`, `NarratorLLM.synthesize(script) -> AudioPayload`,
  `GeminiNarrator(api_key, *, model, voice, base_url, timeout, urlopen)` over
  `generate_audio`; `validate_audio` enforcing non-empty `audio/ogg` ≤
  `MAX_NARRATION_BYTES`; `handle_narrating` with store-once / send-once,
  stored-audio re-send (no re-synthesis), missing-script reset, apology + retry.
  **Deviation:** validation requires the OGG subtype (`audio/ogg`), stricter
  than the `audio/*` wording in Scope — see `validation.md`.
- `defaults.py`: `DEFAULT_NARRATOR_MODEL = "gemini-3.1-flash-tts-preview"`,
  `DEFAULT_NARRATOR_VOICE` (posh-British deep-voice persona),
  `DEFAULT_NARRATOR_TIMEOUT = 90.0`, `MAX_NARRATION_BYTES = 5 * 1024 * 1024`.
- `session.py`: `SESSION_VERSION = 5`; `narration: bytes | None`,
  `narrator_sent: bool`; `store_narration` / `mark_narrator_sent`; `reset`
  clears both.
- `transport.py`: `Transport.send_voice` + `UrllibTransport.send_voice`
  (multipart, file field `voice`, filename `narration.ogg`, shared token
  redaction).
- `bouncer.py` / `polling.py` / `cli.py` / `config.py`: Narrator runs in the
  same update after the script lands; `done && script_sent && !narrator_sent`
  re-drives narration; `GEMINI_NARRATOR_MODEL` / `GEMINI_NARRATOR_VOICE` /
  `GEMINI_NARRATOR_TIMEOUT` getters (env-beats-file; timeout validated).
- Structured logging: `narration_started`, `narrator_ready` (byte count + mime
  only), `narration_sent`, `narrator_failed`, `narration_skipped`.
- `scripts/test`: **691 passed** (was 560); `scripts/hooks` green; full suite
  green with every socket entry point patched to raise. Live Gemini smoke: not
  reached — key rejected by Google (HTTP 400). Full evidence in
  `validation.md`.