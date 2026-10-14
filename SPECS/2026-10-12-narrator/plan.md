# Plan — The Narrator (Phase 6)

> **Revision R1 (2026-10-14, user-approved).** This plan implements the reworked
> Narrator (MP3 audio via `sendAudio`, Gemini **Interactions API**,
> `gemini-3.8-flash-lite-tts`, voice `en-gb-storyteller-4`). It **supersedes**
> the original Phase 6 plan, which targeted an OGG `sendVoice` path that live
> probing proved impossible (see `requirements.md` → Revision R1). The Gateway,
> Bouncer, Interviewer, Converter and Scripter are unchanged.
>
> **Prerequisite unwinding:** the pre-revision implementation lives on this same
> branch (commit `007aac7`). The implementer revises it in place — no revert; the
> task groups below describe the *target* state, and any pre-revision test that
> asserted OGG/`sendVoice`/`generateContent`-audio is rewritten, not deleted.

Red/Green TDD: every task group writes its failing tests **first**, then the
code, then `scripts/test` until green, with `scripts/hooks` at the end of the
group. Tests never touch Telegram or Gemini — the sole exception is the opt-in
live smoke in `validation.md` (verifier only). **All 560 pre-existing tests
must stay green throughout** (only the narrator/transport/config/CLI
signatures they assert may change).

Modified: `gemini.py`, `narrator.py`, `transport.py`, `config.py`,
`defaults.py`, `bouncer.py`, `polling.py`, `cli.py`, `tests/conftest.py` + the
narrator/transport/config/CLI/bouncer/gemini tests, README, SPECS.
**Unchanged:** `session.py` (v5 shape), `tests/unit/test_session.py` (the two
fields and stores still hold identical types — only their docstrings mention
"OGG"; update the wording, not the behaviour). No new runtime dependency.

---

## 1. Interactions audio extraction (`gemini.py`)

Red (`tests/unit/test_gemini.py`):
- `AudioPayload` stays pydantic-typed (`data: bytes`, `mime_type: str`).
- `generate_interaction_audio` POSTs to `<base>/v1beta/interactions?key=…`
  (the **model is in the body**, never the URL) and decodes the **first**
  `steps[0].content[]` block whose `type == "audio"` (strict base64
  `validate=True`); later blocks and non-audio blocks are skipped.
- Response `mime_type` is passed through verbatim (`audio/mp3; rate=24000;
  channels=1`).
- Same redaction guarantee as `generate_image`: HTTP / network / protocol /
  malformed / missing-`steps` / no-audio-block failures all raise redacted
  `GeminiError` `from None`; the API key (plain + escaped/control-char form)
  never appears in the message. An Interactions-style error payload
  (`{"error": {"message": ..., "code": ...}}`) surfaces as the redacted detail.

Green:
- Parameterise the POST endpoint (e.g. extract `_post_to(endpoint, body)` and
  have `_post` call it with the `generateContent` URL; add
  `_interactions_endpoint()` returning `<base>/v1beta/interactions?key=…`), and
  add `generate_interaction_audio(request_body) -> AudioPayload` parsing the
  `steps` shape, so the single redaction implementation is reused.

Run `scripts/test` + `scripts/hooks`.

## 2. Narrator port + adapter (`narrator.py`)

Red (`tests/unit/test_narrator.py`, fake `urlopen`):
- Request body (exact): `model == DEFAULT_NARRATOR_MODEL`; `input ==
  [{"type": "user_input", "content": [{"type": "text", "text": script,
  "annotations": [{"type": "speech_metadata", "style": style}]}]}]`;
  `response_format == {"type": "audio", "mime_type": "audio/mp3"}`;
  `generation_config == {"speech_config": [{"voice": voice}]}`. The raw script
  is **only** in the content text — never in an annotation, never duplicated.
- `synthesize(script)` with a valid reply → returns the `AudioPayload`
  (decoded `data`, model-declared `mime_type`).
- `validate_audio`: accepts `audio/mp3`, `audio/mp3; rate=24000; channels=1`,
  `audio/mpeg`, `AUDIO/MP3`; rejects `audio/ogg`, `audio/wav`, `text/plain`,
  `""`, empty bytes, and > `MAX_NARRATION_BYTES` — each violation →
  `NarratorError` `from None`.
- Every failure path → redacted `NarratorError` `from None`; key canary tests
  (plain + control-character form) prove no leak.

Green:
- `GeminiNarrator(api_key, *, model=DEFAULT_NARRATOR_MODEL,
  voice=DEFAULT_NARRATOR_VOICE, style=DEFAULT_NARRATOR_STYLE, base_url=…,
  timeout=…, urlopen=…)` wrapping `generate_interaction_audio`; the revised
  `_request_body`; `validate_audio` accepting the MP3/M4A MIME family; a module
  constant `AUDIO_MEDIA_TYPE = "audio/mpeg"` used for every outbound/log mime.

Run `scripts/test` + `scripts/hooks`.

## 3. Transport `sendAudio` (`transport.py`)

Red (`tests/unit/test_transport.py`):
- `send_audio(chat_id, audio, *, media_type, filename="narration.mp3")` POSTs a
  multipart body with `chat_id`, a single file part named **`audio`** with the
  declared `media_type`, to **`sendAudio`**, through the same
  `_read`/`_decode` normalisation as `send_photo` (`ok: false`, HTTP codes and
  token redaction all behave identically).
- The `Transport` protocol exposes `send_audio` (and no longer `send_voice`).

Green:
- `UrllibTransport.send_audio` using `_multipart` (reuse; no new helpers);
  `AUDIO_FILENAME = "narration.mp3"`.

Run `scripts/test` + `scripts/hooks`.

## 4. Session (`session.py`) — wording only

No behaviour change: `SESSION_VERSION == 5`, `narration: bytes | None`,
`narrator_sent: bool`, `store_narration`/`mark_narrator_sent`. Update the
docstrings that say "OGG" to "audio payload (MP3)". Existing session tests stay
green unchanged (keep them, adjust only wording assertions if any).

Run `scripts/test` + `scripts/hooks`.

## 5. Narrating routing (`narrator.py`)

Red (`tests/unit/test_narrator.py` + `tests/conftest.py` — `FakeNarrator`
returns scripted `AudioPayload`s / raises; `FakeTransport` records
`send_audio` calls):
- `handle_narrating(transport, chat_id, *, session, narrator, logger)`:
  - not `done` / `!script_sent` / already `narrator_sent` → `narration_skipped`,
    `False` (guards).
  - stored narration, not yet sent → **re-send the stored bytes** via
    `send_audio` (narrator NOT called), then `mark_narrator_sent`.
  - no stored narration → script required (missing → loud `narrator_failed` +
    reset-and-reprompt) → `narration_started` → `synthesize` →
    `store_narration` → `send_audio` → `mark_narrator_sent`; exact order
    asserted.
  - `synthesize` raises / returns an invalid payload → apology,
    `narrator_failed`, no flag, returns `False`, nothing raises
    (`KeyboardInterrupt`/`SystemExit` exempt); send failure → apology, no flag
    → next call re-sends the stored bytes (no re-synthesis).
  - every send uses `media_type="audio/mpeg"`, filename `narration.mp3`.

Green:
- `handle_narrating` + helpers calling `send_audio`; events unchanged
  (`narration_started`, `narrator_ready` byte count + mime only,
  `narration_sent`, `narrator_failed`, `narration_skipped`); the audio and the
  script are never logged.

Run `scripts/test` + `scripts/hooks`.

## 6. Bouncer dispatch + wiring (`bouncer.py`, `polling.py`, `cli.py`, `conftest.py`)

Red:
- `tests/unit/test_bouncer.py` (extend/adjust): after scripting completes, a
  `done && script_sent && !narrator_sent` chat continues into narrating in the
  **same** update; a later update in that state re-drives narrating
  (stored-bytes re-send via `send_audio`); `done && narrator_sent` keeps the
  light reply; `/start` resets `narration`/`narrator_sent`.
- `tests/unit/test_polling.py`: `narrator` still threaded to the handler.
- `tests/unit/test_config.py`: `get_narrator_model()` default ==
  `"gemini-3.8-flash-lite-tts"`; `get_narrator_voice()` default ==
  `"en-gb-storyteller-4"` (env-beats-file, blank → default);
  `get_narrator_timeout()` unchanged validation.
- `tests/unit/test_cli.py`: narrator built from key, model, voice, timeout and
  threaded into polling (signature unchanged).
- `tests/conftest.py`: `FakeTransport.send_audio`; `FakeNarrator` default
  payload becomes an MP3-shaped `AudioPayload` (e.g. `mime_type="audio/mpeg"`).

Green:
- `bouncer.py`: accept `narrator`; same-update handoff runs narrating after the
  script lands; route `done && script_sent && !narrator_sent`
  to `narrator.handle_narrating`.
- `polling.py`/`cli.py`: unchanged threading.
- `config.py`: getters (names unchanged); `defaults.py`:
  `DEFAULT_NARRATOR_MODEL = "gemini-3.8-flash-lite-tts"`,
  `DEFAULT_NARRATOR_VOICE = "en-gb-storyteller-4"`,
  `DEFAULT_NARRATOR_STYLE` (fixed style string),
  `DEFAULT_NARRATOR_TIMEOUT = 90.0`, `MAX_NARRATION_BYTES = 5 * 1024 * 1024`.

Run `scripts/test` + `scripts/hooks`.

## 7. Component flow + docs + verification

Red/Green:
- Extend `tests/component/test_narrator_flow.py`: over the real `poll_once`
  with fakes, a full pass: photo gated → Q1..Q5 → dossier → teaser → `sendPhoto`
  → script message → **audio message** in the same pass with **no user input**
  (`send_audio` called once with `audio/mpeg`, filename `narration.mp3`); the
  payload is stored (`state.narration == audio bytes`), `narrator_sent` set; a
  failed synthesis apologises and the next update completes; a failed
  `send_audio` re-sends the **stored** bytes (`FakeNarrator` not re-called);
  text after everything → light reply; `/start` clears `narration` and
  `narrator_sent`. No network.
- Re-run the full suite with every socket entry point patched to raise
  (throwaway plugin) — must stay green and fire.
- **Live smoke (verifier):** `/tmp/opencode/live_narrator_smoke.py` (or
  equivalent) calls `GeminiNarrator.synthesize(<sample script>)` with the real
  key and asserts a non-empty `audio/mp3*`/`audio/mpeg` payload ≤
  `MAX_NARRATION_BYTES`, writing the note to `/tmp/opencode/narration_smoke.mp3`
  for the user to play. Must **pass** with the configured key.
- Docs (validated at verification): `README.md` (Phase 6 status; the narrator
  now sends an **MP3 audio message via `sendAudio`** using the Interactions API
  and `gemini-3.8-flash-lite-tts`; `GEMINI_NARRATOR_MODEL` /
  `GEMINI_NARRATOR_VOICE` (voice id) / `GEMINI_NARRATOR_TIMEOUT`; the
  dependency-free MP3 contract), `SPECS/TECH.md` (final stage: `NarratorLLM`
  port, Interactions audio path, handoff chain ends with `sendAudio`, MP3; no
  OGG/encoder), `SPECS/ROADMAP.md` (Phase 6 complete, format MP3).
  `SPECS/MISSION.md` step 6 already says "OGG/MP3 voice note" — reconcile the
  wording to "MP3 audio message" if needed. Walk `validation.md`.

Finish: `scripts/test` + `scripts/hooks` green; commit; push; update PR #7.

## Verification notes (filled at verification)

_To be completed by the verifier for Revision R1._
