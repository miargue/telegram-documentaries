# Requirements — The Narrator (Phase 6)

## Context

- **Phase 5 (The Scripter) is merged to `main`** (PR #6): the bot now ends a
  run with a dramatic 60–90-word narration script, sent to the chat right
  after the hybrid portrait and stored on the session (`SessionState.script`)
  — "a documentary script sitting in a text bubble," exactly as this phase
  says. `scripts/test` = 560 passed, `scripts/hooks` green, no test touches
  the network.
- **This is Phase 6** from `SPECS/ROADMAP.md`: "*The Narrator* — Script
  routed directly to Gemini TTS. Render to a Telegram-compatible audio format
  (OGG/MP3) and send to chat. Acceptance: a playable voice note arrives in
  chat after the portrait, speaking the Scripter's words."
- **Branch:** `feature/2026-10-12-narrator` (created off `main` at
  `a57d863`).
- This phase is **purely transformational** (the user's own framing): the
  Scripter already chose tone, vocabulary and comedic pacing; the Narrator is
  **not another reasoning agent** — text in, audio stream out.

### Revision R1 (2026-10-14, user-approved) — supersedes the original Phase 6 design

The first cut of Phase 6 (committed as `007aac7`, PR #7) assumed
`gemini-3.1-flash-tts-preview` returns **OGG/Opus** and delivered it via
`sendVoice` with **no encoder**. Live probing once a working `GEMINI_API_KEY`
was supplied (2026-10-14) **proved that design cannot work**:

- The shipped request body omits the required
  `generationConfig.speechConfig.voiceConfig.prebuiltVoiceConfig.voiceName`
  → **HTTP 400 `INVALID_ARGUMENT`** on every call. (The whole PR is dead on
  arrival live.)
- `gemini-3.1-flash-tts-preview` (now labelled *legacy* by Google) returns
  **headerless raw PCM** (`audio/l16; rate=24000; channels=1`) on unary
  `generateContent` — **not OGG**. `sendVoice` requires OGG/Opus, so the bytes
  are undeliverable, and `validate_audio` (OGG-only) would reject them.
- **No** Gemini endpoint returns OGG/Opus for these TTS models: the newer
  **Interactions API** rejects `audio/ogg` and `audio/ogg_opus` for
  `gemini-3.1-flash-tts-preview`, `gemini-3.8-flash-tts` **and**
  `gemini-3.8-flash-lite-tts`. A true voice-note bubble would therefore
  require a PCM→Opus encoder (ffmpeg/pydub) — a new runtime dependency, and
  one not even installed in this environment (`pydantic` is the only runtime
  dependency).
- **What does work with no new dependency:** the **Interactions API**
  (`POST /v1beta/interactions`) with `gemini-3.8-flash-lite-tts` returns a
  valid **MP3** (`audio/mp3; rate=24000; channels=1`, ID3v2 header verified)
  for `response_format: {"type":"audio","mime_type":"audio/mp3"}`, and
  Telegram `sendAudio` plays MP3 natively.

The user approved the rework (2026-10-14): **MP3 audio file via `sendAudio`**.
Revision R1 below is the authoritative contract; the pre-revision decisions,
scope, plan and validation are superseded.

## Goal

Turn the stored narration script into a **playable audio message in the chat**:
route the raw script to Gemini TTS through the **Interactions API** (a direct
REST call — no ADK, no Gemini Python SDK, no new dependencies), receive the
binary **MP3** payload, keep it briefly in the session (the in-memory
equivalent of "persist to a temporary audio file" — the same handling as the
portrait photo bytes), and deliver it via Telegram **`sendAudio`**. The user
hears the documentary.

## Decisions locked with the user (rounded, 2026-10-14 — Revision R1)

1. **Format & delivery — MP3 audio file via `sendAudio`.** Gemini TTS returns
   **MP3** natively (`gemini-3.8-flash-lite-tts` on the Interactions API), the
   format Telegram `sendAudio` accepts — so **no encoder dependency** is needed
   and "`pydantic` is the only runtime dependency" stays true. A `sendVoice`
   voice note is **rejected**: it needs OGG/Opus, which no endpoint produces,
   and forcing it in would add an encoder dependency. MP3 is the documented,
   dependency-free path that actually plays.
2. **API surface — the Gemini Interactions API** (`POST /v1beta/interactions`).
   Unary `generateContent` on the TTS model returns headerless raw PCM
   (`audio/l16`) and is a legacy path; the Interactions API returns a clean
   audio container (MP3) and is where Google is steering TTS. The existing
   `generateContent` client path is left for the other stages.
3. **Model — `gemini-3.8-flash-lite-tts`** by default; `GEMINI_NARRATOR_MODEL`
   overrides (env-beats-file). The lite tier is ample for a ≤90-word narration
   and cheaper; `gemini-3.8-flash-tts` also works and can be selected via the
   override.
4. **Voice — a prebuilt voice id, `en-gb-storyteller-4`** by default (the
   built-in "Nature Documentary Narrator": 56-year-old male, low/deep pitch,
   British); `GEMINI_NARRATOR_VOICE` overrides. The delivery **style** (measured,
   dramatic wildlife-documentary narration) is a **fixed constant**
   (`DEFAULT_NARRATOR_STYLE`) sent as `speech_metadata.style` — **not**
   prepended to the text. Gemini 3.8 TTS treats the input text as a *verbatim
   transcript*, so prepending direction would be read aloud; style must travel
   as metadata. (This replaces the pre-revision "persona string prepended as a
   text part" mechanism, which was a 3.1-only behaviour.)
5. **Timing & retry — same-update, stored, once-only.** Unchanged from the
   original lock: the audio follows the script message in the **same update**
   (no user input), exactly once; the raw MP3 bytes are stored on the session
   (v5) so a failed delivery re-sends the **stored bytes** with **no
   re-synthesis**; a failed synthesis apologises and retries on the next update.
   Mirrors Phase 5 exactly.
6. **Verification — component + live smoke (now expected to pass).** In-process
   component tests prove portrait → script → audio message with fakes; the live
   Gemini smoke runs with the (now working) `GEMINI_API_KEY` and must return a
   valid MP3. The pre-revision "not reached — key rejected" outcome is obsolete.

## Scope (in — Revision R1)

- **`gemini.py`** — a new **Interactions audio path**, keeping the single
  redaction implementation. `AudioPayload` is unchanged (`data: bytes`,
  `mime_type: str`). Add `GeminiJsonClient.generate_interaction_audio(
  request_body) -> AudioPayload`: POST the JSON body to
  `<base_url>/v1beta/interactions?key=<key>` (model travels **in the body**,
  not the path) and decode the **first `audio` block** of the first step's
  content (`steps[0].content[]`, a block with `type == "audio"`, strict base64
  `validate=True` on `data`, model-declared `mime_type`). Share the same
  POST/normalisation/`_redact`/`from None` machinery as `generate_json` — the
  endpoint is parameterised so both shapes reuse one `_post`.
- **`narrator.py`** (revised):
  - `NarratorError(Exception)`; `NarratorLLM` (Protocol):
    `synthesize(script: str) -> AudioPayload` (unchanged port).
  - `GeminiNarrator(api_key, *, model=DEFAULT_NARRATOR_MODEL, voice=…,
    style=DEFAULT_NARRATOR_STYLE, base_url=…, timeout=…, urlopen=…)` over
    `generate_interaction_audio`.
  - Request body (exact): `{"model": model, "input": [{"type":
    "user_input", "content": [{"type": "text", "text": script,
    "annotations": [{"type": "speech_metadata", "style": style}]}]}],
    "response_format": {"type": "audio", "mime_type": "audio/mp3"},
    "generation_config": {"speech_config": [{"voice": voice}]}}`. The script is
    the payload; the style is metadata, never text.
  - `validate_audio`: non-empty payload whose `mime_type` starts with
    `audio/mp3` **or** `audio/mpeg` (case-insensitive; the model returns the
    decorated `audio/mp3; rate=24000; channels=1`), ≤ `MAX_NARRATION_BYTES`;
    otherwise `NarratorError` (`from None`). The send path normalises to the
    clean constant `audio/mpeg`.
- **`transport.py`** — rename `send_voice` → **`send_audio`** across the
  `Transport` protocol and `UrllibTransport`: multipart upload to **`sendAudio`**
  (file field **`audio`**, filename **`narration.mp3`**) through the existing
  pure `_multipart` helper and the same `_read`/`_decode`/token-redaction path.
  `VOICE_FILENAME` → `AUDIO_FILENAME = "narration.mp3"`.
- **`session.py`** — **unchanged** (v5 already stores `narration: bytes | None`
  and `narrator_sent: bool`; the bytes are simply MP3 now). Docstrings updated
  to say "audio payload" rather than "OGG".
- **Routing (`narrator.py`)**: `handle_narrating(...)` unchanged in behaviour —
  store-once/send-once, stored-bytes re-send, missing-script
  reset-and-reprompt, apology + retry — but its two send sites call
  `send_audio(... media_type="audio/mpeg", filename="narration.mp3")` and its
  logs report the clean `audio/mpeg` mime.
- **Bouncer dispatch / wiring** (`bouncer.py`, `polling.py`, `cli.py`,
  `conftest.py`): unchanged structurally — the narrator still runs in the same
  update and `done && script_sent && !narrator_sent` re-drives it. Only the
  transport method name changes (`FakeTransport.send_voice` → `send_audio`)
  and the CLI `NarratorFactory` signature stays `(api_key, model, voice,
  timeout)` — `voice` is now the prebuilt voice-id.
- **Config** (`config.py` + `defaults.py`): `DEFAULT_NARRATOR_MODEL =
  "gemini-3.8-flash-lite-tts"`, `DEFAULT_NARRATOR_VOICE =
  "en-gb-storyteller-4"`, `DEFAULT_NARRATOR_STYLE` (fixed style string),
  `DEFAULT_NARRATOR_TIMEOUT = 90.0`, `MAX_NARRATION_BYTES = 5 * 1024 * 1024`.
  `get_narrator_model()` / `get_narrator_voice()` / `get_narrator_timeout()`
  keep their names and precedence (env-beats-file; blank → default; timeout
  validated, hard `ConfigError` otherwise). The docstring for
  `get_narrator_voice` changes: it now resolves a **prebuilt voice id**, not a
  free-text persona.
- **Structured logging**: unchanged events — `narration_started`,
  `narrator_ready` (byte count + mime only — never the script, never the
  audio), `narration_sent`, `narrator_failed`, `narration_skipped`.

## Out of scope (YAGNI — Revision R1)

- **No OGG / voice-note (`sendVoice`) path** — rejected (encoder dependency;
  no endpoint produces OGG).
- **No audio encoder dependency** (ffmpeg/pydub) — the whole point of the MP3
  choice.
- **No caption** on the audio (the script message right before it is the
  readable version).
- **No `speech_metadata` beyond a single style** (no multi-speaker, no per-word
  tags) and **no streaming** — one unary call.
- **No new TTS agent/framework** — a direct REST synthesis call by design.
- **No duration/file-length estimation** (we do not parse MP3); the 90-word
  script cap already bounds the note.

## Contracts at boundaries (typed, untrusted input)

| Boundary | Type | Failure behaviour |
| --- | --- | --- |
| Gemini Interactions request | JSON body `{model, input, response_format, generation_config}` | HTTP/network/protocol/malformed → redacted `GeminiError` from None, apology, retry next update |
| Gemini Interactions response | first `type=="audio"` block of `steps[0].content[]` → `{mime_type, data(base64)}` | missing/empty/invalid base64 → redacted `GeminiError` from None |
| Audio payload validation | `mime_type.startswith("audio/mp3")` or `"audio/mpeg"`, non-empty, ≤ `MAX_NARRATION_BYTES` | violation → `NarratorError` (unusable output), retry next update |
| `sendAudio` upload | multipart via `_multipart`, field `audio`, filename `narration.mp3` | failure → apology, stored bytes re-sent next update (no re-synthesis) |
| Session | `SessionState` v5 (`narration: bytes`, `narrator_sent`) | unknown chat → fresh default; wrong type → `ValidationError` |
| Script input | stored v4 `script` (validated prose) | missing → fail loud + reset-and-reprompt |

`chat_id` stays `int` end-to-end. The narration is **synthesised once and
sent once** (`narrator_sent` set only after a successful `sendAudio`).

## Non-negotiables

- **Per-`chat_id` isolation** — one user's audio/script never leaks into
  another's session.
- **Never log or commit secrets** — the single-redaction guarantee extends to
  the Interactions adapter; audio bytes and the script are never written to
  logs.
- **No test contacts Telegram or Gemini** — fakes only, proven by re-running
  the suite with every socket entry point patched to raise (the opt-in live
  smoke in `validation.md` is the sole exception).
- **The narration is delivered exactly once** and is **Telegram-playable
  audio** (MP3 as produced by the model).
- **Fail loud, degrade gracefully, never wedge** — a `done` chat missing its
  audio is always retryable.
- **No new runtime dependency** — stdlib `urllib` + `pydantic` only.

## Acceptance criteria

1. A playable **audio message** arrives in the chat **after the script**,
   speaking the Scripter's words (`sendAudio`, MP3).
2. The narration is delivered **exactly once**, generated from the **stored**
   script (never improvised), and a failed delivery re-sends the stored audio
   with no re-synthesis.
3. `scripts/test` and `scripts/hooks` are green, including all 560 pre-
   existing tests.
4. The opt-in **live smoke passes** with the configured `GEMINI_API_KEY`,
   returning a non-empty MP3.

## Delivered (filled at verification)

_To be completed by the verifier for Revision R1._
