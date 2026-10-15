# Validation — Resilience (Phase 7)

How to prove Phase 7 is done and mergeable. Work through every step and record
the real command output in the `Evidence` slots. Steps marked **verifier** are
run by the verifier agent; all others by the implementer.

## 0. Preconditions

- [ ] On branch `feature/2026-10-15-resilience`, based on `main` at `1d182e4`.
- [ ] `git status` clean before starting; `scripts/test`/`scripts/hooks` green
      on the pre-change tree (697 passed baseline).

## 1. The explicit reset routine (`session.py`, `bouncer.py`)

- [ ] `PurgeReport` is a pydantic model with `chat_id`, `previous_phase`,
      `discarded_photo`, `discarded_narration`, `discarded_script`,
      `discarded_interview_answers`, `discarded_dossier`,
      `previous_media_group_id` — and **no** bytes/user text fields.
- [ ] `SessionStore.purge(chat_id) -> PurgeReport` exists; `SessionStore.reset`
      **no longer exists** (no compatibility alias).
- [ ] `reset_chat(session, chat_id, *, update_id, log) -> PurgeReport` is the
      single named routine used by **both** the start-command path and the gate
      rejection path.
- [ ] A `session_reset` INFO record is emitted on every reset, carrying the
      report fields; the log line contains **no** photo/audio bytes and no user
      text (grep the captured record's `__dict__`).
- [ ] Manually confirm the three named actions:
  1. **Cancel Active Sessions** — a mid-interview chat returns to
     `awaiting_photo`; a `/start` during an interview resets (atomic, no
     concurrent turn).
  2. **Purge Ephemeral Assets** — after purge, `state.photo is None` and
     `state.narration is None` (the in-memory media is gone).
  3. **Re-Initialize Agent Memory** — after purge: `interview == []`,
     `pending_question is None`, `dossier is None`, `script is None`, every flag
     `False`, `last_media_group_id is None`.

**Evidence:**
```
<paste the relevant pytest output / a short REPL check>
```

## 2. Command recognition (case / `@botname` / caption)

- [ ] `handle_update` resets and prompts for: `/start`, `/restart`, `/Start`,
      `/START`, `/ReStart`, `/start@MiargueBot`, `/restart now`.
- [ ] A **photo captioned `/start`** resets (drops the photo, prompts); the
      photo is **not** sent to the gate.
- [ ] A photo with a **non-command caption** is gated normally.
- [ ] A command resets at **every** phase (asserted at `interviewing` and
      `done`).

**Evidence:**
```
<paste the parametrized matrix run>
```

## 3. Wrong-payload-at-wrong-stage matrix

- [ ] A parametrized matrix over (phase × content) passes: text during photo
      intake → re-prompt; unsupported content (sticker/voice/document) →
      re-prompt at `awaiting_photo` and re-ask mid-interview; an album sibling →
      benign skip; empty text → re-prompt; a messageless update → acked, no
      reply; `done` variants route to the documented retry/light path.
- [ ] No case raises out of `handle_update` (the matrix asserts return values,
      not exceptions).

**Evidence:**
```
<paste the matrix run>
```

## 4. Transient `getUpdates` resilience

- [ ] A `getUpdates` failure is retried: a transport failing N times then
      succeeding yields the normal offset, with one `get_updates_retry` record
      and one injected `sleep` per failure.
- [ ] Backoff delays follow `1.0, 2.0, 4.0, …` and are capped at
      `RETRY_MAX_DELAY` (30.0).
- [ ] `OSError` is retried identically; `KeyboardInterrupt` propagates.
- [ ] The replaced test no longer asserts propagation; all other polling tests
      (malformed/poison/non-dict/messageless/offset) are green.

**Evidence:**
```
<paste the polling test run>
```

## 5. Whole-suite + static checks

- [ ] `scripts/test` green — **all 697 pre-existing tests still pass** (or a
      documented count if the deliberate signature change alters a test).
- [ ] `scripts/hooks` green (compileall + smoke import + ruff + mypy).
- [ ] No test touches the network: re-run the full suite with every socket entry
      point patched to raise (`socket.socket.connect`, `connect_ex`,
      `create_connection`, `socket.getaddrinfo`) and confirm it stays green and
      that the patch actually fires.

**Evidence:**
```
<paste scripts/test summary, scripts/hooks summary, and the socket-blocked run>
```

## 6. Behaviour invariants

- [ ] **Isolation:** purging chat A leaves chat B untouched.
- [ ] **Total reset:** after purge a chat equals a fresh `SessionState()`.
- [ ] **Timeout invariant:** the transport client timeout (30.0 s default)
      exceeds `LONG_POLL_TIMEOUT` (10 s), so a long poll is never client-cut
      mid-window. Confirm both constants and assert `DEFAULT_TIMEOUT >
      LONG_POLL_TIMEOUT`.
- [ ] **Never wedged:** every stage failure path still degrades (apology /
      retry next update) and never raises into `poll_once`.

**Evidence:**
```
<paste the invariant assertions>
```

## 7. Spec/doc reconciliation

- [ ] `SPECS/ROADMAP.md` Phase 7 requires the explicit reset routine (three
      actions), the guard matrix and the transient-failure policy; decisions
      (in-memory purge, single-threaded, retry-with-backoff) recorded; status
      updated.
- [ ] `SPECS/TECH.md` documents reset semantics, command recognition, the
      `getUpdates` retry policy, the single-threaded decision and the timeout
      invariant.
- [ ] `SPECS/MISSION.md` states the reset routine's three actions.
- [ ] `README.md` reflects the user-facing behaviour.
- [ ] `validation.md` §Deviations lists any difference between this spec and
      what shipped, with the user's approval.

## Deviations

_(filled at verification — list every difference between the spec and the
shipped code, and its justification/approval)_
