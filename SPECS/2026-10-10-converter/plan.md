# Plan — The Converter (Phase 4)

Red/Green TDD: every task group writes its failing tests **first**, then the
code, then runs `scripts/test` until green. `scripts/hooks` (compileall +
smoke import + ruff + mypy) is run at the end of every group. Tests never
touch Telegram or Gemini — the one exception is the explicit, opt-in live
smoke in `validation.md` (verifier, not part of the suite). **All 379
pre-existing tests must stay green throughout.**

New module: `converter.py` (port + Gemini adapter + conversion routing with
teaser/caption/guards). Modified: `session.py`, `gemini.py`, `transport.py`,
`bouncer.py`, `config.py`, `defaults.py`, `polling.py`, `cli.py`,
`tests/conftest.py` + their tests. No third-party HTTP dependency.

---

## 1. Session extension (`session.py`)

Red (`tests/unit/test_session.py`):
- Default state: `version == 3`, `portrait_sent is False`; `reset` clears it.
- `mark_portrait_sent(chat_id)` sets the flag without touching the photo,
  interview log or dossier.
- Wrong types (`portrait_sent` as a non-bool) raise `ValidationError`; two
  chats never share the flag.

Green:
- `SESSION_VERSION = 3`; `SessionState.portrait_sent: bool = False`;
  `SessionStore.mark_portrait_sent(chat_id)`.

Run `scripts/test` + `scripts/hooks`.

## 2. Shared Gemini client gains image output (`gemini.py`)

Red (`tests/unit/test_gemini.py`, fake `urlopen`):
- `generate_image(request_body)`:
  - posts to the same `…generateContent?key=<key>` endpoint,
  - returns a typed payload (`bytes` + `mime_type`) decoded from the first
    candidate's first `inlineData` part (base64 `data`), preserving the
    `mimeType`,
  - normalises and **redacts** the API key in every failure path (the same
    branch sweep as `generate_json`: HTTP / network / protocol / malformed
    JSON / error payload / no candidates / candidate without an `inlineData`
    part / non-base64 `data`), all `from None`,
  - control-character key still redacted (escaped form).

Green:
- `InlineImagePayload` typed model (`data: bytes`, `mime_type: str`).
- `GeminiJsonClient.generate_image(request_body) -> InlineImagePayload`
  reusing the existing POST/redaction plumbing; `generate_json` untouched.

Run `scripts/test` + `scripts/hooks` (expect all pre-existing suites green).

## 3. Transport multipart upload (`transport.py`)

Red:
- Pure helper tests (`tests/unit/test_transport.py`): `_multipart` builds a
  well-formed multipart body — correct boundary (present in the
  `Content-Type` header), `chat_id` text field, optional `caption` text
  field, one `photo` file part carrying the exact bytes, a stable filename
  and the given `Content-Type`.
- `send_photo` tests (fake `urlopen`): request URL `/bot<token>/sendPhoto`,
  `Content-Type: multipart/form-data; boundary=…`, POST body parses back to
  the same fields/bytes; success decodes the payload; failures normalise to
  redacted `TelegramApiError` (network/HTTP/protocol/`ok:false`), token never
  appears in a raised message (plain + escaped/control-char forms).

Green:
- `Transport` Protocol gains `send_photo(...)`; `UrllibTransport` implements
  it via the pure `_multipart` helper, reusing `_read`/`_decode` (so
  redaction and normalisation are identical to `call`).

Run `scripts/test` + `scripts/hooks`.

## 4. Converter port + Gemini adapter (`converter.py`)

Red (`tests/unit/test_converter.py`, fake `urlopen`):
- Request body shape: exactly **one** `user` content; parts are
  `[text-directive, inline_data(photo)]` with the photo base64-identical to
  the input bytes and `mime_type: "image/jpeg"`; the persona is only in
  top-level `systemInstruction` (never a turn); `generationConfig` has
  `responseModalities: ["IMAGE"]` and **no** `responseSchema`; the directive
  text names the dossier's `suggested_animal` and quotes its `summary`
  (behavioural traits) and asks for the hybrid portrait in image form.
- `hybridize` returns `GeneratedImage` with the bytes/mime from the fake
  `inlineData` response.
- Unusable outputs raise `ConverterError`: missing/empty `inlineData`,
  non-image mime, bytes > `MAX_PHOTO_BYTES`; HTTP/network/malformed failures
  → `ConverterError`; API key redacted in every raised message (full sweep
  + control-character key).

Green:
- `ConverterError`, `GeneratedImage(BaseModel)`, `ConverterLLM(Protocol)`:
  `hybridize(photo: bytes, dossier: Dossier, *, media_type: str =
  "image/jpeg") -> GeneratedImage`; `GeminiConverter(api_key, *, model=
  DEFAULT_CONVERTER_MODEL, base_url=…, timeout=DEFAULT_CONVERTER_TIMEOUT,
  urlopen=…)` wrapping `GeminiJsonClient.generate_image`, wrapping
  `GeminiError` → redacted `ConverterError` (`from None`).
- Directive + persona constants (e.g. "fuse the subject's likeness with a
  {suggested_animal}, letting the behavioural quirks shine through: {summary}
  — reply with only the hybrid portrait image").

Run `scripts/test` + `scripts/hooks`.

## 5. Conversion routing (`converter.py`)

Red (`tests/unit/test_converter.py` + `tests/conftest.py` — add `FakeConverter`
with scripted `GeneratedImage` values / exceptions; `FakeTransport` gains
`send_photo` recording):
- `handle_conversion(transport, chat_id, *, session, converter, logger)`:
  - sends the teaser, calls `converter.hybridize(session.photo, dossier)`,
    calls `transport.send_photo(chat_id, image, media_type, caption)`
    **only once the image is validated**, then `session.mark_portrait_sent`
    — order asserted (teaser → hybridize → send_photo → flag).
  - `hybridize` raises `ConverterError`/anything → apology, **no** flag,
    `conversion_failed` event, returns `False`, nothing raises.
  - `send_photo` fails → apology, no flag, retryable.
  - photo or dossier missing on a `done` chat → loud `conversion_failed`
    event + reset-and-reprompt (never a crash).
  - caption constant is tiny and contains the suggested animal.
- Each of these is tested for the exact send sequence (no duplicate teasers,
  no double portraits).

Green:
- `handle_conversion` (and small helpers) in `converter.py` implementing the
  above; structured events `conversion_started`, `conversion_succeeded`,
  `portrait_sent`, `conversion_failed`, `conversion_skipped`.

Run `scripts/test` + `scripts/hooks`.

## 6. Bouncer dispatch + wiring (bouncer, polling, config, CLI, conftest)

Red:
- `tests/unit/test_bouncer.py` (extend): a `done && !portrait_sent` chat routes
  to the Converter (retry path); after an interview update completes the
  interview (freshest dossier), the **same-update handoff** runs the Converter
  in that same pass; `done && portrait_sent` keeps delegating to the
  Interviewer's light reply; `/start` still resets everything including the
  flag.
- `tests/unit/test_polling.py`: `converter` threaded to the handler.
- `tests/unit/test_config.py`: `get_converter_model()` env-
  `GEMINI_CONVERTER_MODEL`-beats-file, blank falls back to
  `gemini-3.1-flash-image`; `get_converter_timeout()` env-beats-file, blank
  falls back to `120.0`.
- `tests/unit/test_cli.py`: converter built from the API key/model/timeout and
  threaded into polling.
- `tests/conftest.py`: `FakeTransport.send_photo` records; `FakeConverter`
  added.

Green:
- `bouncer.py`: accept `converter`; dispatch `done && !portrait_sent` →
  `converter.handle_conversion`; run the same-update handoff after a delegated
  update leaves the chat finishable; pass the converter through the delegated
  calls.
- `polling.py` / `cli.py`: thread `converter`.
- `config.py`: `get_converter_model()`, `get_converter_timeout()`;
  `defaults.py`: `DEFAULT_CONVERTER_MODEL`, `DEFAULT_CONVERTER_TIMEOUT`,
  `MAX_PHOTO_BYTES`.

Run `scripts/test` + `scripts/hooks`.

## 7. Component flow + docs + verification

Red/Green:
- `tests/component/test_converter_flow.py` over the real `poll_once` with
  fakes: photo gated → success + Q1 → answers × 5 → dossier report → teaser →
  `sendPhoto` carrying the fake-generated bytes → `portrait_sent` flag;
  the portrait arrives with no user input in between (same update); a failed
  `hybridize` apologises and the next update completes; text after a delivered
  portrait gets the light reply. No network.
- Re-run the full suite with all socket entry points patched to raise (same
  throwaway-plugin technique as Phase 3) — must stay green and fire.

Docs (validated at verification):
- `README.md`: Phase 4 status, converter behaviour,
  `GEMINI_CONVERTER_MODEL` / `GEMINI_CONVERTER_TIMEOUT`, portrait delivery.
- `SPECS/TECH.md`: Converter stage (typed `ConverterLLM` port; `gemini.py`
  now `generate_json` + `generate_image`; transport gains multipart
  `send_photo`); the Bouncer as router across all stages incl. the same-update
  handoff; session note: `portrait_sent` flag, version 3. With user approval.
- `SPECS/ROADMAP.md`: mark Phase 4 complete when verified.
- Walk `validation.md`.

Finish: `scripts/test` + `scripts/hooks` green; commit; push; open PR to
`main`.

## Verification notes (filled at verification)

- **Automated checks.** `scripts/test` = `483 passed in 4.21s`; `scripts/hooks`
  = `[hooks] all checks passed` (ruff "All checks passed!", mypy "Success: no
  issues found in 36 source files"). Baseline confirmed: collecting the suite
  on `main` in a throwaway worktree = `379 tests collected`; the delta is the
  104 Phase 4 additions. All read paths through the shipped modules match the
  plan's task groups (§1 session, §2 `generate_image`, §3 multipart, §4 port +
  adapter, §5 routing, §6 dispatch/wiring, §7 component + docs).
- **No-network proof.** Throwaway plugin at `/tmp/opencode/nonet/nonet_plugin.py`
  patches `socket.socket.connect`, `connect_ex`, `socket.create_connection`
  and `socket.getaddrinfo`; its sanity probe fired for all four, and the full
  suite re-ran green: `483 passed in 2.68s`.
- **Behaviour.** Same-update handoff (photo → Q1..Q5 → dossier → teaser →
  `sendPhoto` → `portrait_sent`, no user input) verified by
  `test_final_answer_hands_off_to_the_converter_in_the_same_update` and the
  component `test_full_pass_delivers_the_hybrid_portrait_exactly_once`.
  Request-shape, validation, degradation/retry, missing-artifact reset,
  post-portrait light reply, `/start` reset and redaction are each covered by
  the named tests recorded in `validation.md`.
- **Events.** `conversion_started`, `conversion_succeeded`, `portrait_sent`,
  `conversion_failed`, `conversion_skipped` — all present and asserted; payloads
  carry only `chat_id` / `phase` / `portrait_sent` / `mime_type`.
- **Live smoke.** Live Gemini call **not reached**: `thispersondoesnotexist.com`
  was unavailable (404 / HTML), the fallback Wikimedia Commons portrait
  (1,276,121 bytes, `image/jpeg`) was fetched, and `hybridize` got
  `http error: HTTP 403 Forbidden: Your API key was reported as leaked. Please
  use another API key.` A bare list-models call with the same key returned the
  same 403, confirming a blocked credential. Classified "not reached" (not
  HTTP-400 / malformed output). Recorded in `validation.md` §5.
- **Deviations.** One benign extension: `get_converter_timeout` raises
  `ConfigError` on non-numeric / non-positive / non-finite values (the plan only
  specified blank → default). No other drift from `requirements.md` /
  `plan.md`; `SPECS/MISSION.md` unchanged.