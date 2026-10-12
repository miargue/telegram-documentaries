# Plan — The Narrator (Phase 6)

Red/Green TDD: every task group writes its failing tests **first**, then the
code, then `scripts/test` until green, with `scripts/hooks` at the end of the
group. Tests never touch Telegram or Gemini — the sole exception is the opt-in
live smoke in `validation.md` (verifier only, and only with a working key).
**All 560 pre-existing tests must stay green throughout.**

New module: `narrator.py` (port + Gemini TTS adapter + narrating routing).
Modified: `gemini.py`, `transport.py`, `session.py`, `bouncer.py`,
`config.py`, `defaults.py`, `polling.py`, `cli.py` + their tests. No new
runtime dependency.

---

## 1. Gemini audio extraction (`gemini.py`)

Red (`tests/unit/test_gemini.py`):
- `AudioPayload` is pydantic-typed (`data: bytes`, `mime_type: str`).
- `generate_audio` extracts the **first audio `inlineData` part** of the first
  candidate (strict base64 `validate=True`); ignores text/image parts; body
  passes `responseModalities: ["AUDIO"]` through untouched.
- Same redaction guarantee as `generate_image`: HTTP / network / protocol /
  malformed / missing-inlineData failures all raise redacted `GeminiError`
  `from None`; the API key (plain + escaped/control-char form) never appears
  in the message.

Green:
- `AudioPayload(BaseModel)`; `GeminiJsonClient.generate_audio(request_body)
  -> AudioPayload`, sharing `_post` and the error normalisation (the single
  redaction implementation stays in `gemini.py`).

Run `scripts/test` + `scripts/hooks`.

## 2. Narrator port + Gemini TTS adapter (`narrator.py`)

Red (`tests/unit/test_narrator.py`, fake `urlopen`):
- Request body: exactly ONE `user` content with **two text parts** — voice
  guidance (from the voice constant) and the **raw script verbatim** — and
  `generationConfig.responseModalities == ["AUDIO"]`; nothing else is added.
- `synthesize(script)` with a valid reply → returns the `AudioPayload`
  (decoded `data`, model-declared `mime_type`).
- Post-validation in `synthesize`: `mime_type` must start with `audio/`;
  `data` must be non-empty; `data` ≤ `MAX_NARRATION_BYTES` — each violation →
  `NarratorError` `from None`; a `narration.ogg`-shaped payload (e.g.
  `audio/ogg; codecs=opus`) passes.
- Every failure path → redacted `NarratorError` `from None`; key canary tests
  (plain + control-character form) prove no leak.

Green:
- `NarratorError(Exception)`; `NarratorLLM(Protocol)`: `synthesize(script: str)
  -> AudioPayload`; `GeminiNarrator(api_key, *, model=DEFAULT_NARRATOR_MODEL,
  voice=DEFAULT_NARRATOR_VOICE, base_url=…, timeout=…, urlopen=…)` wrapping
  `GeminiJsonClient.generate_audio`.

Run `scripts/test` + `scripts/hooks`.

## 3. Transport `sendVoice` (`transport.py`)

Red (`tests/unit/test_transport.py`):
- `send_voice(chat_id, audio, *, media_type, filename="narration.ogg")` POSTs a
  multipart body with `chat_id`, a single file part named `voice` with the
  declared `media_type`, through the same `_read`/`_decode` normalisation as
  `send_photo` (`ok: false`, HTTP codes and token redaction all behave
  identically).
- The `Transport` protocol gains `send_voice`.

Green:
- `UrllibTransport.send_voice` using `_multipart` (reuse; no new helpers).

Run `scripts/test` + `scripts/hooks`.

## 4. Session v5 (`session.py`)

Red (`tests/unit/test_session.py`):
- Defaults: `version == 5`, `narration is None`, `narrator_sent is False`; a
  reset clears both.
- `store_narration(chat_id, audio: bytes)` sets `narration` and leaves
  `narrator_sent` alone; `mark_narrator_sent(chat_id)` sets the flag without
  touching `narration`, the script, the portrait flag or the dossier.
- Wrong types (`narration` non-bytes, `narrator_sent` non-bool) →
  `ValidationError`; per-chat isolation holds for bytes.

Green:
- `SESSION_VERSION = 5`; the two fields + two store methods; docstring chain
  updated (Phase 6 narrator).

Run `scripts/test` + `scripts/hooks`.

## 5. Narrating routing (`narrator.py`)

Red (`tests/unit/test_narrator.py` + `tests/conftest.py` — add `FakeNarrator`
returning scripted `AudioPayload`s / raising, recording scripts):
- `handle_narrating(transport, chat_id, *, session, narrator, logger)`:
  - not `done` / `!script_sent` / already `narrator_sent` → `narration_skipped`,
    `False` (guards).
  - stored narration, not yet sent → **re-send the stored bytes** (narrator
    NOT called), then `mark_narrator_sent` — the re-ask pattern.
  - no stored narration → script required (missing → loud `narrator_failed` +
    reset-and-reprompt) → `narration_started` → `synthesize` →
    `store_narration` → `send_voice` → `mark_narrator_sent`; exact order
    asserted.
  - `synthesize` raises / sends an invalid payload → apology, `narrator_failed`,
    no flag, returns `False`, nothing raises (KeyboardInterrupt/SystemExit
    exempt); send failure → apology, no flag → next call re-sends the stored
    bytes (no re-synthesis).

Green:
- `handle_narrating` + helpers; events `narration_started`, `narrator_ready`
  (byte count + mime only), `narration_sent`, `narrator_failed`,
  `narration_skipped`; the audio and the script are never logged.

Run `scripts/test` + `scripts/hooks`.

## 6. Bouncer dispatch + wiring (bouncer, polling, config, CLI, conftest)

Red:
- `tests/unit/test_bouncer.py` (extend): after scripting completes (the
  same-update chain), when the chat is `done && script_sent && !narrator_sent`
  the **same** update continues into narrating; an update arriving in that
  state re-drives narrating (stored-bytes re-send); `done && narrator_sent`
  keeps the light reply; `/start` resets `narration`/`narrator_sent` too.
- `tests/unit/test_polling.py`: `narrator` threaded to the handler.
- `tests/unit/test_config.py`: `get_narrator_model()` / `get_narrator_voice()`
  / `get_narrator_timeout()` — env-beats-file; blank model/voice → defaults;
  invalid timeout (non-numeric / ≤ 0 / non-finite) → `ValueError`.
- `tests/unit/test_cli.py`: narrator built from the key, voice and model,
  threaded into polling.
- `tests/conftest.py`: `FakeNarrator` added.

Green:
- `bouncer.py`: accept `narrator`; extend the same-update handoff to run
  narrating after the script lands; route `done && script_sent &&
  !narrator_sent` to `narrator.handle_narrating`.
- `polling.py`/`cli.py`: thread `narrator`.
- `config.py`: the three getters; `defaults.py`:
  `DEFAULT_NARRATOR_MODEL = "gemini-3.1-flash-tts-preview"`,
  `DEFAULT_NARRATOR_VOICE` (posh-British deep-voice documentary persona),
  `DEFAULT_NARRATOR_TIMEOUT = 90.0`, `MAX_NARRATION_BYTES = 5 * 1024 * 1024`.

Run `scripts/test` + `scripts/hooks`.

## 7. Component flow + docs + verification

Red/Green:
- Extend the component suite (new `tests/component/test_narrator_flow.py` or
  extend `test_scripter_flow.py`): over the real `poll_once` with fakes, a
  full pass: photo gated → Q1..Q5 → dossier → teaser → `sendPhoto` → script
  message → **voice note** in the same pass with **no user input**; the
  payload is stored (`state.narration == audio bytes`), `narrator_sent` set;
  a failed synthesis apologises and the next update completes; a failed
  `sendVoice` re-sends the **stored** bytes (FakeNarrator not re-called);
  text after everything → light reply; `/start` clears `narration` and
  `narrator_sent`. No network.
- Re-run the full suite with every socket entry point patched to raise
  (throwaway plugin) — must stay green and fire.
- Docs (validated at verification): `README.md` (Phase 6 status, the narrator,
  `GEMINI_NARRATOR_MODEL` / `GEMINI_NARRATOR_VOICE` / `GEMINI_NARRATOR_TIMEOUT`,
  OGG-voice-note contract), `SPECS/TECH.md` (final stage: NarratorLLM port,
  handoff chain ends with sendVoice; NativeAudio note; session v5),
  `SPECS/ROADMAP.md` (Phase 6 complete). Walk `validation.md`.

Finish: `scripts/test` + `scripts/hooks` green; commit; push; open PR to
`main`.

## Verification notes (filled at verification)

_To be completed by the verifier._