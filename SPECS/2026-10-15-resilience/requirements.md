# Requirements — Resilience (Phase 7)

## Context

- **Phase 6 (The Narrator) is merged to `main`** (PR #7, merge commit
  `1d182e4`): the bot now completes the whole chain — portrait → interview →
  hybrid portrait → narration script → **MP3 audio message**. `scripts/test` =
  697 passed, `scripts/hooks` green, no test touches the network.
- **This is Phase 7** from `SPECS/ROADMAP.md`:
  - `/start` and `/restart`: purge session state **and** temporary media
    without restarting the process.
  - Wrong-payload-at-wrong-stage guards across all phases.
  - API timeout / transient-failure fallbacks that degrade gracefully.
  - **Acceptance:** reset mid-interview clears everything; misplaced
    photos/text at any stage are re-prompted; Gemini/TTS failures never kill
    the conversation.
- **Branch:** `feature/2026-10-15-resilience` (created off `main` at
  `1d182e4`).
- **User framing (2026-10-15):** multi-agent fragility accumulates at state
  transitions; a user who drops off, abandons the flow, or sends unexpected
  media never needs to feel trapped. A `/restart` (or `/start`) must run an
  **explicit reset routine** performing three named actions:
  1. **Cancel Active Sessions** — terminate any running interview loop / waiting
     state for the requesting `chat_id`.
  2. **Purge Ephemeral Assets** — delete temporary media on disk (original
     photo, converted PNG, generated OGG/MP3 voice files).
  3. **Re-Initialize Agent Memory** — clear conversation history buffers; reset
     state trackers to the greeting stage.
  The spec must **require** this routine and reconcile it with the real
  architecture.

## What "resilience" means in this architecture (reality check)

Phase 7 is a **formalize / harden / verify** pass, not a green-field build. The
existing design already covers most of the surface; the work is to make the
contracts explicit, close the real gaps, and lock them in with tests.

- **State is in-memory only.** `SPECS/MISSION.md` (Non-goals) and
  `session.py` say it plainly: photos, dossiers and media "live in memory only
  and vanish on reset or process exit". There are **no temp files on disk
  anywhere** — `media.py` downloads photo bytes into memory
  (`SessionState.photo`), and the narrator keeps the MP3 bytes in memory
  (`SessionState.narration`). The Phase 6 comment "the in-memory equivalent of
  persist to a temporary audio file" is the canonical statement of this.
- **There are no per-chat background loops or threads.** The gateway is a
  single long-polling loop (`polling.run_polling` → `poll_once` →
  `bouncer.handle_update`) that processes one batch at a time; per-chat state is
  a dict entry in `SessionStore`. A `/start` update is therefore handled
  **atomically**: no in-flight interview turn can survive it, because none runs
  concurrently.
- **`SessionStore.reset()` already purges everything** — it installs a fresh
  `SessionState()`, dropping the photo, interview log, pending question,
  dossier, script, narration, media-group dedupe and every flag, and returning
  the chat to `awaiting_photo`.

So the three named actions map onto this architecture as:

| Named action (user) | Real mechanism |
| --- | --- |
| Cancel Active Sessions | Reset the per-chat state so the chat is back at `awaiting_photo`; no thread to kill (single-threaded, atomic command handling). |
| Purge Ephemeral Assets | Drop the in-memory `photo` and `narration` byte buffers (the only "media"; nothing on disk). |
| Re-Initialize Agent Memory | Clear the interview log, pending question, dossier, script, media-group dedupe and all delivered-flags. |

### Decisions locked with the user (2026-10-15)

1. **Purge = clear in-memory buffers.** Keep the in-memory design; do **not**
   add on-disk temp media. Purging drops `SessionState.photo` and
   `SessionState.narration` (and any future buffer) via a fresh default. This
   keeps `SPECS/MISSION.md` ("no long-term storage") and `TECH.md` true.
2. **Scope = the full Phase 7 trio.** The spec covers all three ROADMAP bullets
   (reset, wrong-payload guards, transient-failure fallbacks), even though two
   are largely already built — the pass makes them explicit and tested.
3. **Approach = formalize + harden + verify.** Introduce one named, testable
   reset routine with structured logging, close the concrete gaps (command
   recognition, transient `getUpdates` failures), add a phase × content-type
   guard matrix, and verify the whole surface.
4. **Keep the gateway single-threaded.** Do **not** move the slow Converter
   (~120 s) off the polling loop into a worker/queue. Instead: ensure every
   stage call is **bounded by a timeout** (already true for Gemini adapters and
   the transport) and make `getUpdates` survive transient failures. A
   slow-but-bounded conversion briefly pauses polling — acceptable for a
   single-user bot, and far simpler than introducing concurrency.
5. **Transient `getUpdates` failures retry with backoff, stay alive — permanent
   ones surface.** A momentary network fault (`OSError`) or a *transient*
   `TelegramApiError` (HTTP/Telegram code `429` or `>= 500`, or a
   network-normalised error with no code) must never kill the bot: log loudly
   and retry with capped exponential backoff indefinitely. A **permanent**
   `TelegramApiError` (a coded 4xx other than `429` — e.g. `401` revoked token,
   `403`, `404`, `409` duplicate poller) is **not** retried; it propagates so
   `cli.py` exits `1` and the fault surfaces. The startup `getMe` probe still
   surfaces a bad token.
6. **Rename `reset` → `purge`, returning a typed report.** No back-compat
   alias (repo convention). The report feeds the `session_reset` structured
   log.

## Goal

Make the gateway provably hard to wedge: a named `reset_chat` routine that
cancels the chat's session, purges its in-memory media and re-initializes its
agent memory (logging exactly what it discarded); a command recognizer that
resets on `/start`/`/restart` regardless of case, `@botname` suffix, or whether
the command arrives as a **photo caption**; a phase × content-type guard matrix
proving every misplaced payload is re-prompted and never crashes; and a polling
loop that rides out transient Telegram failures with bounded backoff instead of
dying.

## Real gaps found (grounded in the code)

| # | Gap | Evidence | Fix |
| --- | --- | --- | --- |
| G1 | `/start` recognition is **case-sensitive** (`/Start`, `/RESTART` ignored) | `bouncer._is_start_command` compares exactly | lowercase the command token |
| G2 | A command sent as a **photo caption** does not reset (`/start` with a photo → gated as a portrait) | `bouncer.handle_update` reads only `message.text`; `Message.caption` exists but is unused for routing | extract the command from `text` **or** `caption` |
| G3 | The reset is **not logged as a discrete event** and discards nothing observable | `session.reset` returns a bare `SessionState`; `bouncer` logs no purge details | add `PurgeReport` + a `session_reset` log from the routine |
| G4 | A transient `getUpdates` failure **kills the bot** | `poll_once` calls `transport.call("getUpdates", …)` unguarded; `polling.py` docstring says "transient-failure resilience is Phase 7"; `cli.main` catches `(TelegramApiError, OSError)` and returns `1` | retry transient failures with capped exponential backoff (`_get_updates`); propagate permanent coded errors |
| G5 | The wrong-payload guards exist but are **not proven across the full phase × content matrix** | tests cover individual cases, not the matrix | parametrized matrix test |

## Scope (in)

- **`session.py`** — a typed `PurgeReport` (pydantic) and a renamed
  `SessionStore.purge(chat_id) -> PurgeReport` that snapshots the outgoing
  state's discardable summary (previous phase, whether a photo/script/narration
  was discarded, the interview-answer count, whether a dossier existed, the
  previous media-group id), installs a fresh default, and returns the report.
  `reset` is removed (single owner; no alias). `PurgeReport` carries **no**
  photo/audio bytes and no user text.
- **`bouncer.py`** — the explicit routine
  `reset_chat(session, chat_id, *, update_id, log) -> PurgeReport`: purges via
  `session.purge`, logs one structured `session_reset` event from the report
  (event + chat_id + previous_phase + discarded_* booleans + counts — never
  bytes), and returns the report. Both reset sites (the start command and the
  gate rejection) call it. `_command(message)` extracts the leading command from
  `message.text` **or** `message.caption`, strips an `@botname` suffix, and
  lowercases it; `_is_start_command(message)` compares against
  `{"/start", "/restart"}`.
- **`polling.py`** — `_get_updates(transport, offset, *, log, sleep=time.sleep)`
  retries `transport.call("getUpdates", …)` on **transient** failures — an
  `OSError`, or a `TelegramApiError` whose code is absent, `429` or `>= 500` —
  forever with capped exponential backoff (`RETRY_BASE_DELAY = 1.0`,
  `RETRY_FACTOR = 2.0`, `RETRY_MAX_DELAY = 30.0`), logging a structured
  `get_updates_retry` event per attempt (`offset`, `attempt`, `delay`, redacted
  error). A **permanent** coded `TelegramApiError` (any other 4xx) propagates
  unhandled, so the CLI exits `1`. `poll_once` uses it. `KeyboardInterrupt` /
  `SystemExit` still propagate. The malformed/poison-update handling is
  unchanged.
- **Tests** — `test_session.py` (`purge` report, per-chat isolation),
  `test_bouncer.py` (the routine + command matrix: case, `@botname`, caption,
  each phase), `test_polling.py` (retry-then-success, backoff capping, the
  replaced old "propagates loudly" test), and the component flow (reset
  mid-interview drops the stored photo/narration and re-prompts).
- **Docs** — `SPECS/ROADMAP.md` (Phase 7 rewritten to require the explicit
  routine and the locked decisions), `SPECS/TECH.md` (reset semantics, poll-loop
  retry policy, single-threaded decision, timeout invariants),
  `SPECS/MISSION.md` (reset routine wording), `README.md` (user-facing
  `/start`/`/restart` behaviour + resilience notes).

## Out of scope (YAGNI)

- **No on-disk temp media / temp-file lifecycle.** Rejected: contradicts "no
  long-term storage" and adds I/O for no gain.
- **No background worker thread / job queue / off-loop generation.** Rejected:
  a bounded synchronous call is simpler and sufficient for one user.
- **No session TTL / expiry sweeper.** Nothing is "trapped"; a user can always
  `/start` or carry on, and in-memory state vanishes on process exit.
- **No new runtime dependency** — stdlib only.
- **No change to the hash/format of the pipeline stages** (Bouncer, Interviewer,
  Converter, Scripter, Narrator behaviour is otherwise untouched).

## Contracts at boundaries

| Boundary | Type | Failure behaviour |
| --- | --- | --- |
| Command text/caption | `str | None` from `Message` | unrecognised → normal phase routing; a start command always resets |
| `SessionStore.purge` | `chat_id: int` → `PurgeReport` | unknown chat → fresh default, report reflects an empty outgoing state |
| `getUpdates` transport call | `TelegramApiError` / `OSError` | transient (no code / `429` / `>= 500`): log `get_updates_retry`, sleep with capped backoff, retry forever; permanent coded 4xx: propagate (CLI exits `1`) |
| Stage calls (vision/interview/converter/scripter/narrator/transport) | bounded by their configured timeout | failure degrades as today (apology / retry next update) — never raises into the loop |

`chat_id` stays `int` end-to-end. `PurgeReport` contains no media bytes.

## Non-negotiables

- **Per-`chat_id` isolation** — purging one chat never touches another's state.
- **Reset never leaks** — `PurgeReport` and the `session_reset` log carry no
  photo/audio bytes and no user text.
- **Reset is total** — after `purge`, the chat is exactly a fresh default
  (`awaiting_photo`, empty log, no photo/script/narration/dossier, no flags, no
  media-group dedupe).
- **The bot never dies on a transient network blip** — `getUpdates` retries
  transient failures (`OSError`, or a `TelegramApiError` with no code, `429` or
  `>= 500`) with backoff; a permanent coded error propagates and the process
  exits `1` (as does the startup `getMe` probe on a bad token).
- **Every reset is auditable** — each `reset_chat` call emits one
  `session_reset` event; the stage modules' invariant-break `purge` calls log
  their own `*_failed` ERROR (they cannot import `bouncer.reset_chat` without a
  cycle).
- **No test contacts Telegram or Gemini** — fakes + injectable `sleep` only.
- **No new runtime dependency** — `pydantic` remains the only runtime dep.

## Acceptance criteria

1. `/start` **and** `/restart` — in any case, with or without an `@botname`
   suffix, whether sent as a message or a photo caption — run the explicit reset
   routine: the chat's session is cancelled, in-memory media is purged and agent
   memory is re-initialized, with a loud `session_reset` log describing exactly
   what was discarded.
2. A reset issued **mid-interview** clears the photo, interview log, pending
   question, dossier, script, narration and flags, and returns the chat to the
   greeting (`awaiting_photo`); one chat's reset never affects another's.
3. **Wrong payload at the wrong stage** — text during photo intake, a photo/text
   mid-interview, an album sibling, unsupported content (sticker/voice/
   document), empty text, and a messageless update — is re-prompted or skipped
   as appropriate at every phase, never crashing and never wedging.
4. A **transient `getUpdates` failure** (`OSError`, or a `TelegramApiError` with
   no code, `429`, or `>= 500`) is logged loudly and retried with capped
   exponential backoff; the loop stays alive and resumes acking when the
   transport recovers. A **permanent** coded `TelegramApiError` propagates so
   the process exits `1` instead of looping forever.
5. `scripts/test` and `scripts/hooks` are green, including all 697 pre-existing
   tests (adjusted only where this spec changes a signature/behaviour).

## Delivered (filled at verification)

Verified 2026-10-15 on branch `feature/2026-10-15-resilience` (spec commits
`192af8d`, `06321e8`; implementation uncommitted in the working tree at
verification time). Everything in **Scope (in)** shipped:

- **`session.py`** — `PurgeReport` (pydantic, exactly the eight declared
  fields, no bytes/user-text fields — asserted by
  `test_purge_report_is_a_pydantic_model_with_exactly_the_declared_fields`)
  and `SessionStore.purge(chat_id) -> PurgeReport`; `SessionStore.reset` is
  gone (no alias — `test_session_store_has_no_reset_method`).
- **`bouncer.py`** — `reset_chat(session, chat_id, *, update_id, log) ->
  PurgeReport`, called from exactly two places: the start-command path
  (`bouncer.py:150`) and the gate-rejection path (`bouncer.py:449`). It emits
  one `session_reset` INFO record per call (report fields only; the test greps
  the captured record's `__dict__` for photo/audio bytes and user text and
  finds none). `_command()` / `_is_start_command()` normalise text **or**
  caption, first token, `@botname` stripped, lowercased.
- **`polling.py`** — `RETRY_BASE_DELAY = 1.0`, `RETRY_FACTOR = 2.0`,
  `RETRY_MAX_DELAY = 30.0`, `_is_transient()`, and
  `_get_updates(transport, offset, *, log, sleep=time.sleep)` used by
  `poll_once`; `run_polling` accepts/forwards `sleep`. Transient (`OSError`,
  or `TelegramApiError` with no code / `429` / `>= 500`) retried forever with
  capped backoff; permanent coded 4xx (`400`/`401`/`403`/`404`/`409`)
  propagates out of `poll_once` so `cli.main` returns `1`
  (`cli.py:243-262`, `gateway_failed`).
- **`converter.py` / `scripter.py` / `narrator.py`** — the three
  invariant-break `reset` call sites became `purge`, each still preceded by
  its own `conversion_failed` / `script_failed` / `narrator_failed` ERROR
  (import-cycle constraint preserved).
- **Tests** — 117 new tests: purge/report/isolation (`test_session.py`), the
  routine + command matrix + 61-cell guard matrix (`test_bouncer.py`),
  retry/backoff/classification (`test_polling.py`), the transient CLI ride-out
  (`test_cli.py`) and the two-chat component flow with mid-interview
  `/restart` + a transient blip
  (`test_full_gateway_flow.py::test_gateway_full_chain_restart_mid_interview_and_transient_blip`).
- **Docs** — `SPECS/ROADMAP.md` (Phase 7 complete with the delivered summary
  and the four locked decisions), `SPECS/TECH.md` (new §Resilience (Phase 7):
  command recognition, guard matrix, `getUpdates` retry policy,
  single-threaded decision, timeout invariant; §Session state reset semantics
  rewritten around `purge`/`PurgeReport`/`reset_chat`), `SPECS/MISSION.md`
  (the reset routine's three actions), `README.md` (user-facing `/start`
  behaviour, resilience notes, exit codes, event list, roadmap).

Verification result: `scripts/test` **802 passed** (697 baseline preserved:
685 unchanged pre-existing tests pass, 12 deliberately renamed/replaced, 117
new), `scripts/hooks` green, full suite green with every socket entry point
patched to raise. Checklist-by-checklist evidence and the deviations list are
in `validation.md`.
