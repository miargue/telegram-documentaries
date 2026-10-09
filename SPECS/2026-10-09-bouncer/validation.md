# Validation — The Bouncer (Phase 2)

How we know Phase 2 is correct and safe to merge. A step is "done" only when the
evidence is recorded and the user has approved any spec reconciliation.

## 1. Automated checks

- [ ] `scripts/test` — full suite green, including the new `test_session`,
  `test_media`, `test_vision`, `test_bouncer`, and `test_bouncer_flow` suites.
- [ ] `scripts/hooks` — `compileall` + smoke import + `ruff` + `mypy` all green.
- [ ] No test performs network I/O: every Telegram and Gemini interaction goes
  through `FakeTransport` / `FakeGate` / fake `urlopen`.

## 2. Behaviour checklist (maps to ROADMAP acceptance)

- [ ] `/start` (and `/restart`) resets the session and replies with a
  photo prompt; no gate call is made.
- [ ] Plain text while a photo is expected re-prompts; session untouched.
- [ ] Other content (document / sticker / voice) re-prompts; no crash.
- [ ] Portrait photo → gate returns `is_human=True` → success message sent,
  `SessionState.phase == gate_passed`, and the photo bytes are retained.
- [ ] Non-human photo (animal / landscape / empty) → cheeky rejection sent and
  the session is reset (`phase == awaiting_photo`, photo purged).
- [ ] Photo sent with a caption is treated as a photo.
- [ ] Album / media group → the **first** photo is used.
- [ ] `VisionError` (bad Gemini response / API error) → apologetic reply, loud
  structured log with traceback, session remains usable, nothing raises into the
  polling loop.
- [ ] `TelegramApiError` while fetching the file → same graceful path.
- [ ] `sendMessage` failure → logged loudly, update handled without crashing.
- [ ] Messageless updates (edited message, callback query) are still skipped.

## 3. Contract & safety checks

- [ ] `chat_id` is an `int` used as the session key throughout (no silent
  str/int mismatch).
- [ ] Telegram `file_path` from `getFile` is validated as a safe relative path
  before any URL is built (rejects `..`, absolute paths, schemes).
- [ ] Gemini output is parsed into `HumanVerdict` via the typed schema; no regex
  or blind string matching decides "human".
- [ ] Session state is isolated per `chat_id`; no cross-chat leakage in tests.
- [ ] `TELEGRAM_BOT_TOKEN` and `GEMINI_API_KEY` never appear in logs or raised
  messages (redaction test), and `.env` remains gitignored/untracked.
- [ ] Missing `GEMINI_API_KEY` fails fast at startup with exit code `2`.

## 4. Spec reconciliation (surfacing drift)

- [ ] **Deferred ADK drift recorded:** implementation uses a typed `VisionGate`
  port + Gemini REST adapter, not Google ADK. `SPECS/TECH.md` updated (with user
  approval) to reflect the current architecture and note ADK migration intent.
- [ ] `SPECS/ROADMAP.md` Phase 2 marked complete (only after this checklist).
- [ ] `SPECS/MISSION.md` still matches the delivered experience (no scope drift).
- [ ] `README.md` reflects Phase 2: status, Bouncer behaviour, and the new
  `GEMINI_API_KEY` / optional `GEMINI_VISION_MODEL` configuration.

## 5. Manual smoke (optional, live)

Only with real credentials and the user's consent — records nothing that
violates the no-secrets rule:

- [ ] `python -m telegram_documentaries` starts; `getMe` probe logs the bot.
- [ ] Send `/start` → prompt for a portrait.
- [ ] Send a portrait → success acknowledgement.
- [ ] Send a photo of an animal/object → cheeky rejection.
- [ ] Send text mid-flow → re-prompt.

## 6. Merge gate

- [ ] All of the above checked with evidence in the PR description.
- [ ] Branch `feature/2026-10-09-bouncer` pushed; PR opened against `main`.
- [ ] **User** merges (the build agent does not merge).
