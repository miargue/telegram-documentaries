# Plan — Resilience (Phase 7)

Red/Green TDD: every task group writes its failing tests **first**, then the
code, then `scripts/test` until green, with `scripts/hooks` at the end of the
group. No test touches Telegram or Gemini. **All 697 pre-existing tests must
stay green** (adjusted only where this spec deliberately changes a
signature/behaviour). Keep the gateway **single-threaded** — no threads, no
queues.

Modified: `session.py`, `bouncer.py`, `polling.py`, `tests/unit/test_session.py`,
`tests/unit/test_bouncer.py`, `tests/unit/test_polling.py`,
`tests/conftest.py` (only if a new fake is needed), the component test, README,
and `SPECS/*`.

---

## 1. Session purge + report (`session.py`)

Red (`tests/unit/test_session.py`):
- `PurgeReport` is a pydantic model with `chat_id: int`,
  `previous_phase: Phase`, `discarded_photo: bool`, `discarded_narration: bool`,
  `discarded_script: bool`, `discarded_interview_answers: int`,
  `discarded_dossier: bool`, `previous_media_group_id: str | None`; it exposes
  **no** bytes / user text.
- `purge(chat_id)` on a **fresh** chat returns a report describing an empty
  outgoing state (`previous_phase == awaiting_photo`, all `discarded_*` false/0)
  and leaves `get(chat_id)` as a fresh default.
- `purge` **mid-interview** (a chat advanced through `record_pass` →
  `start_interview` → some `record_interview_answer`, with a dossier and a
  stored script + narration + flags): the report reflects every discarded item
  (`discarded_photo is True`, `discarded_narration is True`,
  `discarded_script is True`, `discarded_dossier is True`,
  `discarded_interview_answers == n`, `previous_phase is Phase.interviewing`),
  and the state afterwards is a fresh default (`awaiting_photo`, empty
  `interview`, `pending_question is None`, `dossier is None`, `photo is None`,
  `narration is None`, `script is None`, all flags `False`,
  `last_media_group_id is None`).
- `purge` is **per-chat isolated**: purging chat A leaves chat B's state intact.
- `reset` no longer exists (`hasattr(SessionStore, "reset") is False`) — the
  rename is not a compatibility shim.

Green:
- Add `PurgeReport`; rename `reset` → `purge`, snapshotting the outgoing state
  into a report before installing `SessionState()`. Update the module/class
  docstrings (the reset semantics section) accordingly.

Run `scripts/test` + `scripts/hooks`.

## 2. Explicit reset routine + command recognition (`bouncer.py`)

Red (`tests/unit/test_bouncer.py`):
- `reset_chat(session, chat_id, *, update_id, log)` returns a `PurgeReport` and
  emits exactly one `session_reset` INFO record carrying the report's fields
  (and never any media bytes / user text). On a mid-interview chat the report
  shows the discarded items.
- Command recognition via `_is_start_command` (or the public behaviour through
  `handle_update`):
  - `/start`, `/restart` reset and prompt (`reason="command"`).
  - `/Start`, `/START`, `/ReStart` reset too (case-insensitive).
  - `/start@MiargueBot` and `/restart@somebot` reset.
  - a **photo captioned `/start`** resets (drops the photo) and prompts — the
    photo is **not** gated.
  - a photo with a **non-command caption** (e.g. `"my cat"`) is gated as normal.
  - `/start ` with trailing text (`/restart now`) resets.
  - a non-command text starting with `/` (`/help`) in `awaiting_photo`
    re-prompts (never crashes).
- The gate **rejection** path also calls `reset_chat` (a `session_reset` record
  is emitted before the rejection reply).
- `handle_update` resets on a command **at every phase** (`awaiting_photo`,
  `gate_passed`, `interviewing`, `done`) — asserted for at least
  `interviewing` and `done`.

Green:
- Add `reset_chat`; replace both `session.reset(chat_id)` call sites with it
  (start command and rejection). Add `_command(message)` (reads `message.text`
  then `message.caption`, strips `@botname`, lowercases) and rework
  `_is_start_command(message)`. Keep `_START_COMMANDS = frozenset({"/start",
  "/restart"})`.

Run `scripts/test` + `scripts/hooks`.

## 3. Wrong-payload-at-wrong-stage matrix (`tests/unit/test_bouncer.py`)

Red — a parametrized matrix over **(phase × content)** proving the guard
behaviour and that nothing raises:
- phases: `awaiting_photo`, `gate_passed`, `interviewing`, `done`
  (plus `done` variants: `!portrait_sent`, `portrait_sent && !script_sent`,
  `script_sent && !narrator_sent`, all-sent).
- contents: text, command (case/@botname/caption variants), photo, photo album
  sibling, photo with non-command caption, empty text, unsupported
  (sticker/voice/document via `document_update`), messageless update.
- expected outcomes asserted narrowly: which reply text (prompt / re-ask /
  light completion / retry path) or a benign skip, and that the session is left
  in the documented state.

Green:
- Fix any genuine gap the matrix surfaces (the known one is G2/G1, done in §2).
  No speculative guards: only add code the matrix proves is needed.
- Add a `_RecordingHandler`-based assertion that a reset produced a
  `session_reset` record and a normal update did not.

Run `scripts/test` + `scripts/hooks`.

## 4. Transient `getUpdates` resilience (`polling.py`)

Red (`tests/unit/test_polling.py`):
- A transport that fails `getUpdates` **N times then succeeds**: `poll_once`
  retries, sleeps once per failure via the injected `sleep`, and returns the
  normal offset; the structured `get_updates_retry` record is emitted per
  failure with `offset`, `attempt` and `delay`.
- Backoff is capped: the recorded delays follow `1.0, 2.0, 4.0, …` and never
  exceed `RETRY_MAX_DELAY` (use enough consecutive failures to reach the cap).
- `OSError` is retried the same as `TelegramApiError`.
- `KeyboardInterrupt` from the transport propagates (not retried).
- **Replace** `test_get_updates_transport_failure_propagates_loudly` with a test
  asserting the new contract (retry, then success). The malformed/poison/
  non-dict update tests stay green unchanged.

Green:
- Add constants `RETRY_BASE_DELAY`, `RETRY_FACTOR`, `RETRY_MAX_DELAY` and
  `_get_updates(transport, offset, *, log, sleep=time.sleep)`; `poll_once`
  fetches through it. Retry forever; only `KeyboardInterrupt`/`SystemExit` (and
  non-transport errors) escape. Update the module docstring: transient
  `getUpdates` failures are now retried, not propagated.
- Note in the docstring that slow stage calls are bounded by their configured
  timeouts and run synchronously (single-threaded by decision).

Run `scripts/test` + `scripts/hooks`.

## 5. Component flow (`tests/component/…`)

Red/Green:
- Extend the full-gateway component test: drive `poll_once` through
  photo → interview → portrait → script → audio, then send `/restart`
  mid-interview in a **second** chat and assert the reset routine purged the
  in-memory photo/narration (the store no longer holds them) and re-prompted;
  assert the first chat is untouched (isolation). A transient getUpdates failure
  between batches (fake `sleep`) is ridden out and the run completes. No network.

Run `scripts/test` + `scripts/hooks`.

## 6. Docs + verification

Green (validated at verification):
- `SPECS/ROADMAP.md`: rewrite Phase 7 to **require** the explicit reset routine
  (the three named actions mapped to mechanisms), the guard matrix and the
  transient-failure policy; record the decisions (in-memory purge, single-
  threaded, retry-with-backoff). Mark the phase complete at verification.
- `SPECS/TECH.md`: reset semantics (`purge` + `PurgeReport`), command
  recognition, the `getUpdates` retry policy, the single-threaded decision, and
  the timeout invariant (client timeout > server long-poll).
- `SPECS/MISSION.md`: state the reset routine's three actions.
- `README.md`: `/start`/`/restart` reset-routine behaviour and the resilience
  notes.
- Walk `validation.md`.

Finish: `scripts/test` + `scripts/hooks` green; commit; push; open the PR.

## Verification notes (filled at verification)

_(to be completed by the verifier agent)_
