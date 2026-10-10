# Plan — The Bouncer (Phase 2)

Red/Green TDD: every task group writes its failing tests **first**, then the
code, then runs `scripts/test` until green. `scripts/hooks` (compileall + smoke
import + ruff + mypy) is run at the end of every group. Tests never touch
Telegram or Gemini.

New modules: `session.py`, `vision.py`, `media.py`, `bouncer.py` (replaces
`echo.py`). No third-party HTTP dependency is added.

---

## 1. Session state driver (`session.py`)

Red:
- `tests/unit/test_session.py`: default state is `awaiting_photo` with
  `version == 1`; `reset` returns a fresh default and purges the photo;
  `record_pass` stores the photo bytes and moves to `gate_passed`; two
  `chat_id`s never share state; unknown chat yields a fresh default.

Green:
- `Phase(str, Enum)`: `awaiting_photo`, `gate_passed`.
- `SessionState(BaseModel)`: `version: int = 1`, `phase: Phase = awaiting_photo`,
  `photo: bytes | None = None`.
- `SessionStore`: `get(chat_id) -> SessionState`, `reset(chat_id) ->
  SessionState`, `record_pass(chat_id, photo) -> SessionState`. Single owner of
  per-`chat_id` read/write/reset; in-memory only.

Run `scripts/test`. Document the driver in `SPECS/TECH.md` §Session state if it
diverges from the contract.

## 2. Boundary models + transport file download

Red:
- Extend `tests/unit/test_models.py`: `PhotoSize` parses; `Message.photo`
  parses a list; unknown fields ignored; wrong types are `ValidationError`.
- New `tests/unit/test_media.py`: `largest_photo(message)` picks the largest by
  `width*height` (tie-break `file_size`); returns `None` when no photos;
  a `FileRef` with a hostile `file_path` (`..`, absolute, `http://…`, empty) is
  rejected.
- Extend `tests/unit/test_transport.py`: `download(file_path)` GETs the file URL
  and returns raw bytes; HTTP/network failures normalise to `TelegramApiError`.

Green:
- `models.py`: add `PhotoSize`, `FileRef`; add `Message.photo: list[PhotoSize] | None`.
- `media.py`: `largest_photo(message) -> PhotoSize | None`;
  `fetch_photo(transport, photo) -> bytes` = `getFile` → validate `FileRef` →
  `transport.download(file_path)`; `InvalidFilePath`/reuse `TelegramApiError`.
- `transport.py`: add `download(file_path: str) -> bytes` to the `Transport`
  protocol and `UrllibTransport` (file base URL `/file/bot<token>/<path>`,
  injectable `urlopen`, same error normalisation). `FakeTransport` in
  `conftest.py` gains `download`.

Run `scripts/test` + `scripts/hooks`.

## 3. Vision port + Gemini adapter (`vision.py`)

Red:
- `tests/unit/test_vision.py` (fake `urlopen`):
  - a valid structured Gemini response → `HumanVerdict(is_human=True/False, reason=…)`;
  - request body includes the image as base64 `inline_data`, the model id, the
    API key in the URL, and a JSON `responseSchema`;
  - non-2xx / `ok=false`-style error payload / malformed JSON / schema-invalid
    output → `VisionError`;
  - the API key is **redacted** in any raised message/log.

Green:
- `HumanVerdict(BaseModel)`: `is_human: bool`, `reason: str | None = None`.
- `VisionGate(Protocol)`: `classify(image: bytes, *, media_type: str = "image/jpeg") -> HumanVerdict`.
- `VisionError(Exception)`.
- `GeminiVisionGate(api_key, *, model=DEFAULT_VISION_MODEL, base_url=…,
  timeout=…, urlopen=…)`: posts to
  `…/v1beta/models/<model>:generateContent?key=<key>` with a bouncer prompt,
  `responseMimeType=application/json` + `responseSchema`, parses the candidate
  text into `HumanVerdict`; normalises all failures to `VisionError`.

Run `scripts/test` + `scripts/hooks`.

## 4. Message routing (`bouncer.py`, replaces `echo.py`)

Red:
- Replace `tests/unit/test_echo.py` with `tests/unit/test_bouncer.py`:
  - `/start` and `/restart` → reset + prompt-for-photo message; no gate call.
  - text (non-command) → prompt-for-photo message; no gate call.
  - photo + `is_human=True` → success message, `record_pass` called, session
    `gate_passed`, gate called once.
  - photo + `is_human=False` → cheeky rejection, session **reset**.
  - photo message with a caption → treated as a photo.
  - multi-photo (album) message → gate called with the **first** photo.
  - non-photo content (document/sticker/voice) → re-prompt.
  - gate raises `VisionError` → apology logged loudly (traceback), session stays
    usable, no exception escapes.
  - photo fetch raises `TelegramApiError` → same graceful path.
  - `sendMessage` failure → logged loudly, `handle_update` returns `False`.
  - messageless update → skipped (unchanged from Phase 1).
- `tests/conftest.py`: add `FakeGate` (scripted verdicts/errors), multi-size
  `photo_update`, `getFile` + file-bytes support in `FakeTransport`.

Green:
- `bouncer.py`: `handle_update(update, transport, *, session, gate, logger=None)`
  implementing the routing above; per-call structured events
  (`bouncer_prompted`, `bouncer_verdict`, `bouncer_rejected`, `bouncer_passed`,
  `photo_fetch_failed`, `vision_failed`, `reply_failed`). Delete `echo.py` and
  `ECHO_REPLY` (echo superseded by user decision).
- `models.py`: keep `PhotoSize` populated (already added in group 2).

Run `scripts/test` + `scripts/hooks`.

## 5. Wiring: polling, config, CLI

Red:
- `tests/unit/test_polling.py`: `poll_once` / `run_polling` thread `session` and
  `gate` through to the handler.
- `tests/unit/test_config.py`: `get_api_key()` env-beats-file, missing →
  `ConfigError`, blank rejected.
- `tests/unit/test_cli.py`: missing `GEMINI_API_KEY` → exit `2` with
  `config_error`; present → gate built from the key and passed to polling; the
  `getMe` probe still runs (exit `1` on failure).

Green:
- `config.py`: add `GEMINI_API_KEY` resolution (`get_api_key`) and
  `get_vision_model()` (env `GEMINI_VISION_MODEL`, default
  `gemini-3.1-flash-lite`).
- `polling.py`: add `session` and `gate` keyword params, pass to
  `handle_update`.
- `cli.py`: resolve token **and** API key; build `SessionStore()` and
  `gate_factory(api_key)` (injectable, default `GeminiVisionGate`); pass both
  into `run_polling`; key/description untouched by tests via fakes.

Run `scripts/test` + `scripts/hooks`.

## 6. Component flow + docs + verification

Red/Green:
- `tests/component/test_bouncer_flow.py`: over the real `poll_once` with
  `FakeTransport` + `FakeGate`: `/start` → prompt; non-human photo → rejection +
  reset; human photo → pass; text after pass → re-prompt. No network.

Docs (validated at verification):
- `README.md`: Phase 2 status, Bouncer behaviour, `GEMINI_API_KEY` /
  `GEMINI_VISION_MODEL`, the gate's reject/reset/re-prompt rules.
- `SPECS/TECH.md`: **reconcile the deferred-ADK drift** (typed `VisionGate`
  port + Gemini REST adapter now; ADK migration noted for a later phase) with
  user approval.
- `SPECS/ROADMAP.md` / `MISSION.md`: mark Phase 2 complete when verified.
- Walk `validation.md`.

Finish: `scripts/test` + `scripts/hooks` green; commit; push; open PR to `main`.

## Verification notes (2026-10-10)

All six groups shipped on `feature/2026-10-09-bouncer`; `scripts/test` =
244 passed, `scripts/hooks` green, no test touches the network (full suite
re-run with sockets blocked passes).

Deviations from this plan, recorded at verification:

- **Album handling (group 4, "gate called with the first photo").** Telegram
  delivers an album as one update per member sharing a `media_group_id`. The
  implementation gates the first member and records the id on the session
  (`remember_media_group`); later members return `SKIPPED`
  (`bouncer_album_skipped`). `SessionState` therefore also carries
  `last_media_group_id`.
- **`defaults.py`** — `DEFAULT_VISION_MODEL` (planned in `vision.py`) lives in
  a new dependency-free module shared with `config.py`.
- **`SKIPPED` sentinel (groups 4/6)** — `handle_update` returns
  `bool | Skipped`; `logging_config.log_lifecycle` logs `skipped` at INFO.
- **`InvalidFilePath`** subclasses `TelegramApiError` (group 2).
- **No live smoke run** — validation.md §5 remains open (no credentials); all
  behaviour is covered by fakes.
- Group 6's doc work (README/TECH/ROADMAP) is done at verification as
  specified; MISSION.md needed no change.
