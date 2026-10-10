# Validation — The Bouncer (Phase 2)

How we know Phase 2 is correct and safe to merge. A step is "done" only when the
evidence is recorded and the user has approved any spec reconciliation.

## 1. Automated checks

- [x] `scripts/test` — full suite green, including the new `test_session`,
  `test_media`, `test_vision`, `test_bouncer`, and `test_bouncer_flow` suites.
  *Verified 2026-10-10: `244 passed in 2.03s` on branch
  `feature/2026-10-09-bouncer` (Python 3.11.2 / pytest 9.1.1).*
- [x] `scripts/hooks` — `compileall` + smoke import + `ruff` + `mypy` all green.
  *Verified 2026-10-10: compileall OK; smoke import OK; `ruff check .` — "All
  checks passed"; `mypy telegram_documentaries tests` — "Success: no issues
  found in 28 source files".*
- [x] No test performs network I/O: every Telegram and Gemini interaction goes
  through `FakeTransport` / `FakeGate` / fake `urlopen`.
  *Verified 2026-10-10: the full suite was re-run with `socket.socket`,
  `create_connection` and `getaddrinfo` patched to raise on any use —
  `244 passed in 1.40s`. No test, fixture or helper opens a socket.*

## 2. Behaviour checklist (maps to ROADMAP acceptance)

- [x] `/start` (and `/restart`) resets the session and replies with a
  photo prompt; no gate call is made.
  *Evidence: `test_bouncer.py::test_start_resets_the_session_and_prompts_for_a_photo`,
  `test_restart_behaves_like_start` (assert `gate.calls == 0`, phase back to
  `awaiting_photo`, photo purged); `/start@MiargueBot` mention also handled.*
- [x] Plain text while a photo is expected re-prompts; session untouched.
  *Evidence: `test_plain_text_re_prompts_without_calling_the_gate`; the
  component flow `test_text_after_pass_re_prompts_without_losing_the_session`
  proves a re-prompt never purges a retained portrait.*
- [x] Other content (document / sticker / voice) re-prompts; no crash.
  *Evidence: `test_other_content_re_prompts_without_calling_the_gate`
  (parametrized over document/sticker/voice); no exception escapes
  `handle_update`.*
- [x] Portrait photo → gate returns `is_human=True` → success message sent,
  `SessionState.phase == gate_passed`, and the photo bytes are retained.
  *Evidence: `test_photo_of_a_human_passes_and_retains_the_image`
  (`store.get(7).phase is Phase.gate_passed`, `photo == b"PORTRAIT-BYTES"`);
  mirrored in `tests/component/test_bouncer_flow.py`.*
- [x] Non-human photo (animal / landscape / empty) → cheeky rejection sent and
  the session is reset (`phase == awaiting_photo`, photo purged).
  *Evidence: `test_photo_without_a_human_is_rejected_and_resets_the_session`
  (stale `record_pass` state is reset: phase back to `awaiting_photo`,
  photo purged).*
- [x] Photo sent with a caption is treated as a photo.
  *Evidence: `test_photo_with_a_caption_is_still_treated_as_a_photo`.*
- [x] Album / media group → the **first** photo is used.
  *Evidence: `test_album_first_photo_is_gated_and_its_siblings_are_ignored`
  and component `test_album_batch_uses_only_the_first_photo` — the first member
  is gated, follow-ups sharing a `media_group_id` return `SKIPPED` (logged
  `bouncer_album_skipped`). Implementation deviation: dedupe is remembered per
  chat in the session (`last_media_group_id`) — see §"Deviations recorded".*
- [x] `VisionError` (bad Gemini response / API error) → apologetic reply, loud
  structured log with traceback, session remains usable, nothing raises into the
  polling loop.
  *Evidence: `test_vision_error_becomes_an_apology_and_leaves_the_session_usable`
  (apology reply, `vision_failed` event with `exc_info`, then a later photo
  still passes); `tests/unit/test_vision.py` sweeps malformed/schema-invalid/
  HTTP/network failures into `VisionError`.*
- [x] `TelegramApiError` while fetching the file → same graceful path.
  *Evidence: `test_photo_fetch_failure_becomes_an_apology`,
  `test_download_failure_becomes_an_apology` (`photo_fetch_failed` event,
  apology reply, gate never called).*
- [x] `sendMessage` failure → logged loudly, update handled without crashing.
  *Evidence: `test_send_failure_is_logged_loudly_and_returns_false`
  (`reply_failed` event with `exc_info`, returns `False`), plus
  `test_send_failure_marks_the_lifecycle_degraded_not_success` and
  `test_unexpected_send_exception_is_swallowed_and_logged`. `log_lifecycle`
  logs the `False` as `degraded` (WARNING), never success.*
- [x] Messageless updates (edited message, callback query) are still skipped.
  *Evidence: `test_messageless_update_is_skipped` (returns `SKIPPED`, no
  reply); `test_messageless_update_logs_a_skip_lifecycle_not_a_degraded_warning`.*

## 3. Contract & safety checks

- [x] `chat_id` is an `int` used as the session key throughout (no silent
  str/int mismatch).
  *Evidence: `Chat.id: int` in `models.py`; `SessionStore._sessions:
  dict[int, SessionState]`; `bouncer.py` keys by `message.chat.id`; tests
  assert `replies[0]["chat_id"] == 1` (int, not `"1"`).*
- [x] Telegram `file_path` from `getFile` is validated as a safe relative path
  before any URL is built (rejects `..`, absolute paths, schemes).
  *Evidence: `media._is_safe_relative_path` percent-decodes (bounded) before
  validating; `test_fetch_photo_rejects_a_hostile_file_path_without_downloading`
  sweeps `..`, absolute, `http://`, `file://`, `C:\`, NUL, whitespace-only,
  single/double-encoded `..` — all rejected with `download` never called;
  `InvalidFilePath` subclasses `TelegramApiError` so callers degrade uniformly.*
- [x] Gemini output is parsed into `HumanVerdict` via the typed schema; no regex
  or blind string matching decides "human".
  *Evidence: `vision.py` posts `responseMimeType=application/json` +
  `responseSchema` (boolean `is_human`), parses the candidate text through
  `HumanVerdict.model_validate`; `test_classify_requests_a_json_schema_not_a_regex`; schema-invalid output → `VisionError`.*
- [x] Session state is isolated per `chat_id`; no cross-chat leakage in tests.
  *Evidence: `test_sessions_are_isolated_per_chat_id`,
  `test_resetting_one_chat_leaves_another_untouched` (session driver),
  `test_sessions_never_leak_between_chats` (through the Bouncer).*
- [x] `TELEGRAM_BOT_TOKEN` and `GEMINI_API_KEY` never appear in logs or raised
  messages (redaction test), and `.env` remains gitignored/untracked.
  *Evidence: `tests/unit/test_token_redaction.py` (19 tests) and the vision
  redaction sweep `test_every_normalised_vision_failure_redacts_the_api_key`
  cover message, formatted traceback and JSON log line, including
  control-character and escaped-form redaction. `git check-ignore .env` →
  `.gitignore:7:.env`; `.env` is untracked (only `.env.example` is tracked).*
- [x] Missing `GEMINI_API_KEY` fails fast at startup with exit code `2`.
  *Evidence: `test_main_returns_config_error_when_api_key_missing` — `main`
  returns `2` with a `config_error` event and the transport is never built
  (`_refuse_transport` would raise). CLI resolves token *and* API key before
  probing `getMe`.*

## 4. Spec reconciliation (surfacing drift)

- [x] **Deferred ADK drift recorded:** implementation uses a typed `VisionGate`
  port + Gemini REST adapter, not Google ADK. `SPECS/TECH.md` updated (with user
  approval) to reflect the current architecture and note ADK migration intent.
  *Done 2026-10-10: TECH.md §Stack/§Architecture now describe the typed-port +
  stdlib-urllib architecture; ADK marked deferred.*
- [x] `SPECS/ROADMAP.md` Phase 2 marked complete (only after this checklist).
  *Done 2026-10-10 after the checklist above passed.*
- [x] `SPECS/MISSION.md` still matches the delivered experience (no scope drift).
  *Verified 2026-10-10: MISSION's Bouncer description (human check, cheeky
  rejection + reset, re-prompt on out-of-order input, albums reduced to first
  photo, in-memory per-chat state) matches the implementation; no edit needed.*
- [x] `README.md` reflects Phase 2: status, Bouncer behaviour, and the new
  `GEMINI_API_KEY` / optional `GEMINI_VISION_MODEL` configuration.
  *Done 2026-10-10.*

## 5. Manual smoke (optional, live)

Only with real credentials and the user's consent — records nothing that
violates the no-secrets rule:

- [ ] `python -m telegram_documentaries` starts; `getMe` probe logs the bot.
- [ ] Send `/start` → prompt for a portrait.
- [ ] Send a portrait → success acknowledgement.
- [ ] Send a photo of an animal/object → cheeky rejection.
- [ ] Send text mid-flow → re-prompt.

> **Not run.** No live smoke was performed at verification (no credentials /
> user consent). The whole behaviour is covered without a network by the
> fakes above, but item 5 remains open until a consenting user runs it.

## 6. Merge gate

- [ ] All of the above checked with evidence in the PR description.
  *Recorded here; the PR description should reference this file.*
- [ ] Branch `feature/2026-10-09-bouncer` pushed; PR opened against `main`.
  *Not done at verification — the verifier does not commit/push; the build
  agent performs this.*
- [ ] **User** merges (the build agent does not merge).

## Deviations recorded at verification (plan → implementation)

- **Album dedupe via `media_group_id`** — the plan said "gate called with the
  **first** photo" of a multi-photo message. Telegram delivers an album as one
  update *per photo* sharing a `media_group_id`, so the implementation
  classifies the first member and remembers the id
  (`SessionState.last_media_group_id` / `SessionStore.remember_media_group`);
  follow-ups return
  the `SKIPPED` sentinel and are logged as `bouncer_album_skipped`. `/start`
  clears the dedupe. Matches the user decision "album proceeds with first
  photo" (`requirements.md` decision 3).
- **`defaults.py`** — a new dependency-free module holding
  `DEFAULT_VISION_MODEL` as the single neutral home so `config.py` and
  `vision.py` agree without importing each other (plan had the constant live in
  `vision.py`).
- **`SKIPPED` lifecycle sentinel** — `logging_config.py` gained a
  `Skipped` sentinel; `handle_update` returns it for messageless updates and
  album follow-ups so `log_lifecycle` logs `skipped` at INFO rather than a false
  `degraded` WARNING.
- **`InvalidFilePath` subclasses `TelegramApiError`** — so media-layer path
  rejections degrade through the same single catch in `bouncer.py`
  (plan allowed either spelling).
- **Session schema grew one field** — `SessionState` adds
  `last_media_group_id: str | None = None` (the dedupe), alongside the planned
  `version` / `phase` / `photo`.
