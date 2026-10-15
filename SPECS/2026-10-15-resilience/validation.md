# Validation — Resilience (Phase 7)

How to prove Phase 7 is done and mergeable. Work through every step and record
the real command output in the `Evidence` slots. Steps marked **verifier** are
run by the verifier agent; all others by the implementer.

**Walked 2026-10-15 by the verifier agent.** Every checklist item below is
tick'd with the command output that proved it; §Deviations lists the three
(non-blocking) differences between this spec and what shipped.

## 0. Preconditions

- [x] On branch `feature/2026-10-15-resilience`, based on `main` at `1d182e4`.
- [x] `git status` clean before starting; `scripts/test`/`scripts/hooks` green
      on the pre-change tree (697 passed baseline).

**Evidence:**
```
$ git branch --show-current
feature/2026-10-15-resilience
$ git merge-base feature/2026-10-15-resilience main   # == git rev-parse main
1d182e4f1deaee2529a30d3d5a37fb8d3c503d78
$ git log --oneline -3
06321e8 Phase 7 spec rev: retry only transient getUpdates failures; tighten reset-audit wording
192af8d Phase 7 spec: Resilience — explicit reset routine, guard matrix, transient getUpdates retry
1d182e4 Merge pull request #7 from miargue/feature/2026-10-12-narrator   <- base == main

Baseline re-confirmed on a detached worktree of the pre-change tree:
$ git worktree add /tmp/opencode/base-1d182e4 1d182e4
$ (cd /tmp/opencode/base-1d182e4 && python -m pytest tests/ -q)
697 passed in 5.60s
$ (cd /tmp/opencode/base-1d182e4 && scripts/hooks)
compileall ok / smoke import ok / ruff "All checks passed!" /
mypy "Success: no issues found in 42 source files" / "all checks passed"

"git status clean before starting" is a historical precondition; at
verification time the tree is deliberately dirty with exactly the 12 intended
Phase 7 files (7 source + 5 test), nothing else:
$ git status --short
 M telegram_documentaries/{bouncer,converter,narrator,polling,scripter,session}.py
 M tests/component/test_full_gateway_flow.py tests/conftest.py
 M tests/unit/{test_bouncer,test_cli,test_polling,test_session}.py
```

## 1. The explicit reset routine (`session.py`, `bouncer.py`)

- [x] `PurgeReport` is a pydantic model with `chat_id`, `previous_phase`,
      `discarded_photo`, `discarded_narration`, `discarded_script`,
      `discarded_interview_answers`, `discarded_dossier`,
      `previous_media_group_id` — and **no** bytes/user text fields.
- [x] `SessionStore.purge(chat_id) -> PurgeReport` exists; `SessionStore.reset`
      **no longer exists** (no compatibility alias).
- [x] `reset_chat(session, chat_id, *, update_id, log) -> PurgeReport` is the
      single named routine used by **both** the start-command path and the gate
      rejection path.
- [x] A `session_reset` INFO record is emitted on every `reset_chat` call,
      carrying the report fields; the log line contains **no** photo/audio bytes
      and no user text (grep the captured record's `__dict__`). The three
      invariant-break `purge` calls in converter/scripter/narrator log their own
      `*_failed` ERROR (they cannot import `bouncer.reset_chat` without a cycle),
      and the `reset_chat` docstring says "every `reset_chat` call" — not "every
      reset".
- [x] Manually confirm the three named actions:
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
$ python -m pytest tests/unit/test_session.py -k "purge or reset" -v
test_purge_purges_the_photo_and_returns_to_awaiting_photo PASSED
test_purge_on_an_unknown_chat_reports_an_empty_outgoing_state PASSED
test_purge_report_is_a_pydantic_model_with_exactly_the_declared_fields PASSED
test_purge_mid_interview_reports_every_discarded_item PASSED
test_purge_is_per_chat_isolated PASSED
test_session_store_has_no_reset_method PASSED
test_remembered_media_group_survives_a_pass_and_a_purge PASSED
test_purge_clears_a_remembered_media_group PASSED
test_purge_clears_the_pending_question PASSED
test_purge_purges_photo_interview_and_dossier PASSED
test_purge_clears_the_portrait_sent_flag PASSED
test_purge_clears_the_script_and_the_script_sent_flag PASSED
test_purge_clears_the_narration_and_the_narrator_sent_flag PASSED
====================== 13 passed, 34 deselected in 0.09s =======================

$ python -m pytest tests/unit/test_bouncer.py -k "reset_chat or gate_rejection_uses or a_reset_logs" -v
test_reset_chat_returns_a_report_and_logs_one_session_reset_record PASSED
test_reset_chat_on_a_fresh_chat_reports_an_empty_purge PASSED
test_gate_rejection_uses_the_reset_routine_and_logs_session_reset PASSED
test_a_reset_logs_session_reset_and_a_normal_update_does_not PASSED
============================== 4 passed ========================================

REPL check (PYTHONPATH=/home/codio/workspace python /tmp/opencode/manual_check.py):
purge fields: ['chat_id', 'discarded_dossier', 'discarded_interview_answers',
  'discarded_narration', 'discarded_photo', 'discarded_script',
  'previous_media_group_id', 'previous_phase']
PurgeReport is pydantic: True
SessionStore.purge sig: (self, chat_id: 'int') -> 'PurgeReport'
SessionStore.reset exists: False
reset_chat sig: (session: 'SessionStore', chat_id: 'int', *,
                 update_id: 'int', log: 'logging.Logger') -> 'PurgeReport'
reset_chat docstring contains 'every ``reset_chat`` call': True
reset_chat docstring contains a bare 'every reset': False

report: {'chat_id': 7, 'previous_phase': 'done', 'discarded_photo': True,
  'discarded_narration': True, 'discarded_script': True,
  'discarded_interview_answers': 1, 'discarded_dossier': True,
  'previous_media_group_id': 'album-9'}
session_reset record: event=session_reset level=INFO
  extras={'chat_id': 7, 'update_id': 555, 'previous_phase': 'done',
  'discarded_photo': True, 'discarded_narration': True, 'discarded_script': True,
  'discarded_interview_answers': 1, 'discarded_dossier': True,
  'previous_media_group_id': 'album-9'}
record leaks bytes/user text (grep of str(record.__dict__)):
  False   # 'PHOTO-BYTES', 'ID3', 'USER SECRET SUMMARY', script text all absent

Named action 2 + 3 after purge:
  photo: None | narration: None | script: None
  interview: [] | pending: None | dossier: None
  flags: False False False | media_group: None | phase: awaiting_photo
  state == fresh SessionState(): True

Call-site audit (only two reset_chat call sites in the package):
  telegram_documentaries/bouncer.py:150  (start-command path)
  telegram_documentaries/bouncer.py:449  (gate-rejection path)
Stage-module invariant-break purges each preceded by their own ERROR:
  converter.py:313 _conversion_failed -> log.error(event=conversion_failed) ; :318 session.purge
  scripter.py:369  _script_failed     -> log.error(event=script_failed)     ; :374 session.purge
  narrator.py:275  _narrator_failed   -> log.error(event=narrator_failed)   ; :280 session.purge
(asserted by test_handle_conversion_resets_and_reprompts_...,
 test_handle_scripting_resets_and_reprompts_when_the_dossier_is_missing,
 test_handle_narrating_resets_and_reprompts_when_the_script_is_missing —
 each asserts its *_failed event, an ERROR-level record, and a purged state.)

Action 1 (cancel active sessions, atomic):
  test_start_mid_interview_resets_the_whole_session → awaiting_photo, photo None,
    interview [], dossier None
  test_start_command_resets_at_every_phase[interviewing] → full clear
  component test_gateway_full_chain_restart_mid_interview_and_transient_blip:
    chat 2 at Phase.interviewing with photo → '/restart' → awaiting_photo,
    photo None, interview [], PROMPT_PHOTO reply
  Atomicity: grep for threading/asyncio/Thread/concurrent.futures in
    telegram_documentaries/ → "NO concurrency primitives found" (one polling loop).
```

## 2. Command recognition (case / `@botname` / caption)

- [x] `handle_update` resets and prompts for: `/start`, `/restart`, `/Start`,
      `/START`, `/ReStart`, `/start@MiargueBot`, `/restart now`.
- [x] A **photo captioned `/start`** resets (drops the photo, prompts); the
      photo is **not** sent to the gate.
- [x] A photo with a **non-command caption** is gated normally.
- [x] A command resets at **every** phase (asserted at `interviewing` and
      `done`).

**Evidence:**
```
$ python -m pytest tests/unit/test_bouncer.py \
    -k "start_command or command_extraction or captioned_start or non_command_caption or gate_rejection_uses or reset_chat or slash_non or a_reset_logs" -v
test_reset_chat_returns_a_report_and_logs_one_session_reset_record PASSED
test_reset_chat_on_a_fresh_chat_reports_an_empty_purge PASSED
test_command_extraction_reads_text_then_caption_case_insensitively PASSED
test_start_command_variants_reset_and_prompt_for_a_photo[/start] PASSED
test_start_command_variants_reset_and_prompt_for_a_photo[/restart] PASSED
test_start_command_variants_reset_and_prompt_for_a_photo[/Start] PASSED
test_start_command_variants_reset_and_prompt_for_a_photo[/START] PASSED
test_start_command_variants_reset_and_prompt_for_a_photo[/ReStart] PASSED
test_start_command_variants_reset_and_prompt_for_a_photo[/start@MiargueBot] PASSED
test_start_command_variants_reset_and_prompt_for_a_photo[/restart@somebot] PASSED
test_start_command_variants_reset_and_prompt_for_a_photo[/restart now] PASSED
test_start_command_variants_reset_and_prompt_for_a_photo[/start ] PASSED
test_photo_captioned_start_resets_and_is_not_gated PASSED
test_photo_with_a_non_command_caption_is_gated_as_normal PASSED
test_slash_non_command_text_re_prompts_without_crashing PASSED
test_gate_rejection_uses_the_reset_routine_and_logs_session_reset PASSED
test_start_command_resets_at_every_phase[text_command-awaiting_photo] PASSED
test_start_command_resets_at_every_phase[text_command-gate_passed] PASSED
test_start_command_resets_at_every_phase[text_command-interviewing] PASSED
test_start_command_resets_at_every_phase[text_command-done] PASSED
test_start_command_resets_at_every_phase[command_as_photo_caption-awaiting_photo] PASSED
test_start_command_resets_at_every_phase[command_as_photo_caption-gate_passed] PASSED
test_start_command_resets_at_every_phase[command_as_photo_caption-interviewing] PASSED
test_start_command_resets_at_every_phase[command_as_photo_caption-done] PASSED
test_a_reset_logs_session_reset_and_a_normal_update_does_not PASSED
====================== 25 passed, 115 deselected in 0.15s ======================

Key assertions inside those tests:
- variants: sendMessage == [PROMPT_PHOTO], gate.calls == 0, phase back to
  awaiting_photo, photo None, 'session_reset' in events, prompted.reason == 'command'.
- captioned '/start@MiargueBot': gate.calls == 0 AND transport.calls_for("getFile") == []
  (the photo is never downloaded or gated); state photo None.
- non-command caption 'my cat': gate.calls == 1, gate.images == [b"PORTRAIT-BYTES"],
  SUCCESS_REPLY, phase interviewing (gated as normal).
- every-phase reset (4 phases × text/caption = 8 cells): full state clear
  (interview [], dossier None, all deliver flags False, narration None).
```

## 3. Wrong-payload-at-wrong-stage matrix

- [x] A parametrized matrix over (phase × content) passes: text during photo
      intake → re-prompt; unsupported content (sticker/voice/document) →
      re-prompt at `awaiting_photo` and re-ask mid-interview; an album sibling →
      benign skip; empty text → re-prompt; a messageless update → acked, no
      reply; `done` variants route to the documented retry/light path.
- [x] No case raises out of `handle_update` (the matrix asserts return values,
      not exceptions).

**Evidence:**
```
$ python -m pytest tests/unit/test_bouncer.py -k "guard_matrix" -v
test_guard_matrix_awaiting_photo[text] PASSED
test_guard_matrix_awaiting_photo[empty_text] PASSED
test_guard_matrix_awaiting_photo[unsupported_sticker] PASSED
test_guard_matrix_awaiting_photo[unsupported_voice] PASSED
test_guard_matrix_awaiting_photo[unsupported_document] PASSED
test_guard_matrix_awaiting_photo[photo] PASSED
test_guard_matrix_awaiting_photo[photo_caption] PASSED
test_guard_matrix_awaiting_photo[messageless] PASSED
test_guard_matrix_awaiting_photo[album_sibling] PASSED
test_guard_matrix_awaiting_photo[command] PASSED
test_guard_matrix_awaiting_photo[command_caption] PASSED
test_guard_matrix_gate_passed_retries_the_start[{text,empty_text,unsupported_* x3,photo,photo_caption}] PASSED (7)
test_guard_matrix_gate_passed_benign_skips[{messageless,album_sibling}] PASSED (2)
test_guard_matrix_interviewing[{text,empty_text,photo,photo_caption,unsupported_sticker,unsupported_voice,unsupported_document}] PASSED (7)
test_guard_matrix_interviewing_benign_skips[{messageless,album_sibling}] PASSED (2)
test_guard_matrix_done_incomplete_portrait_retries_conversion[7 cells] PASSED
test_guard_matrix_done_missing_script_retries_scripting[7 cells] PASSED
test_guard_matrix_done_missing_audio_synthesises[7 cells] PASSED
test_guard_matrix_done_complete_replies_lightly[7 cells] PASSED
test_guard_matrix_done_terminal_contents[{command,command_caption,messageless,album_sibling}] PASSED (4)
====================== 61 passed, 79 deselected in 0.26s =======================

Matrix assertions read directly from tests/unit/test_bouncer.py:
- awaiting_photo: text/empty_text/sticker/voice/document → True + PROMPT_PHOTO
  and gate.calls == 0; photo/photo_caption → gated (phase interviewing);
  messageless/album_sibling → SKIPPED with no sendMessage.
- interviewing: text → next question 'Q2' + recorded QaPair; empty_text/photo/
  sticker/voice/document → re-ask the stored 'Q1', interview [], pending 'Q1',
  llm.next_calls == [] (no model call).
- done variants: incomplete_portrait → Converter runs (sendPhoto, portrait_sent),
  missing_script → Scripter runs (script_sent), missing_audio → Narrator runs
  (sendAudio, narrator_sent), complete → OBSERVATION_COMPLETE_REPLY with no
  converter/scripter/narrator calls; command/command_caption → PROMPT_PHOTO and
  a full reset (phase awaiting_photo, portrait_sent False).
- No exception escapes: every cell asserts a return value (True or SKIPPED);
  an exception would fail the test. 'acked, no reply' for messageless updates is
  additionally covered by the pre-existing
  test_polling.py::test_messageless_update_is_acked_without_a_reply.
```

## 4. Transient `getUpdates` resilience

- [x] A `getUpdates` failure is retried: a transport failing N times then
      succeeding yields the normal offset, with one `get_updates_retry` record
      and one injected `sleep` per failure.
- [x] Backoff delays follow `1.0, 2.0, 4.0, …` and are capped at
      `RETRY_MAX_DELAY` (30.0).
- [x] **Transient** classification retried: `OSError`; `TelegramApiError` with no
      code, code `429`, or code `>= 500`.
- [x] **Permanent** classification propagated (not retried): `TelegramApiError`
      with code `400`/`401`/`403`/`404`/`409` — it escapes `poll_once`.
- [x] `KeyboardInterrupt` propagates.
- [x] `run_polling` accepts/forwards `sleep` so the retry path is testable
      end-to-end without waiting.
- [x] The replaced test no longer asserts propagation; all other polling tests
      (malformed/poison/non-dict/messageless/offset) are green.

**Evidence:**
```
$ python -m pytest tests/unit/test_polling.py -k "get_updates or transient or permanent or process_control or run_polling_forwards or http_status or non_transport" -v
test_non_object_get_updates_payload_is_logged_and_never_crashes PASSED
test_get_updates_is_long_polling_with_offset_param PASSED
test_transient_get_updates_failure_is_retried_then_succeeds PASSED
test_get_updates_backoff_grows_exponentially_and_is_capped PASSED
test_oserror_on_get_updates_is_retried_like_a_transport_error PASSED
test_transient_telegram_codes_are_retried[429] PASSED
test_transient_telegram_codes_are_retried[500] PASSED
test_http_status_code_takes_precedence_over_the_error_code PASSED
test_permanent_coded_errors_propagate_unretried[400] PASSED
test_permanent_coded_errors_propagate_unretried[401] PASSED
test_permanent_coded_errors_propagate_unretried[403] PASSED
test_permanent_coded_errors_propagate_unretried[404] PASSED
test_permanent_coded_errors_propagate_unretried[409] PASSED
test_run_polling_forwards_the_injected_sleep_to_the_retry_path PASSED
test_process_control_signals_are_not_retried[KeyboardInterrupt] PASSED
test_process_control_signals_are_not_retried[SystemExit] PASSED
test_non_transport_error_from_get_updates_propagates PASSED
====================== 17 passed, 17 deselected in 0.11s =======================

$ python -m pytest tests/unit/test_polling.py -q     # the whole file
34 passed in 0.14s     # malformed / poison / non-dict / messageless / offset all still green

$ python -m pytest tests/unit/test_cli.py -k "transient" -v
test_main_rides_out_a_transient_get_updates_failure PASSED
======================= 1 passed, 28 deselected in 0.08s =======================

Key assertions read from the tests:
- retry-then-success: 2 failures → get_updates_attempts == 3, offset == 71,
  slept == [1.0, 2.0], retry records attempt [1,2] / delay [1.0,2.0] / offset 0,
  one sendMessage (batch handled after recovery).
- backoff cap: 8 consecutive failures →
  [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0], max == RETRY_MAX_DELAY (30.0).
- transient: raw OSError retried; no-code TelegramApiError (the default
  _FlakyGetUpdates error) retried; 429 and 500 retried; HTTP 503 (status_code)
  beats error_code 400 and is retried.
- permanent: 400/401/403/404/409 → pytest.raises(TelegramApiError) escaping
  poll_once, exc is the original error, get_updates_attempts == 1, slept == [].
- KeyboardInterrupt / SystemExit propagate with slept == []; a non-transport
  RuntimeError also escapes (not swallowed).
- run_polling(sleep=...) → poll_once → _get_updates: slept == [1.0], 2 attempts.
- CLI: the OLD test test_main_logs_get_updates_failure_and_exits_non_zero was
  replaced by test_main_rides_out_a_transient_get_updates_failure (exit 0,
  attempts == 2, prompt delivered); test_get_updates_transport_failure_propagates_loudly
  is gone from collection (replaced by test_transient_get_updates_failure_is_retried_then_succeeds).
- Exit-1 for a PERMANENT coded error: verified by
  test_permanent_coded_errors_propagate_unretried (escapes poll_once) plus
  code inspection — cli.main wraps run_polling in
  `except (TelegramApiError, OSError): log(gateway_failed); return 1`
  (telegram_documentaries/cli.py:243-262); the getMe probe path is covered by
  test_main_exits_non_zero_when_the_get_me_probe_fails. See §Deviations (2).
```

## 5. Whole-suite + static checks

- [x] `scripts/test` green — **all 697 pre-existing tests still pass** (or a
      documented count if the deliberate signature change alters a test).
- [x] `scripts/hooks` green (compileall + smoke import + ruff + mypy).
- [x] No test touches the network: re-run the full suite with every socket entry
      point patched to raise (`socket.socket.connect`, `connect_ex`,
      `create_connection`, `socket.getaddrinfo`) and confirm it stays green and
      that the patch actually fires.

**Evidence:**
```
$ scripts/test
============================= 802 passed in 3.33s ==============================

Pre-existing-test accounting (697 baseline vs 802 now), from a node-id diff of
the two trees (pytest --collect-only | sort, comm):
  removed pre-existing node-ids: 12
    - 10 SessionStore.reset* tests renamed 1:1 to purge* (reset → purge rename)
      (+ new: purge report fields, mid-interview report, per-chat isolation,
       has-no-reset-method, empty-outgoing-state = 14 new session tests total)
    - test_polling.py::test_get_updates_transport_failure_propagates_loudly
      → REPLACED by test_transient_get_updates_failure_is_retried_then_succeeds
      (plan §4 required this)
    - test_cli.py::test_main_logs_get_updates_failure_and_exits_non_zero
      → REWRITTEN as test_main_rides_out_a_transient_get_updates_failure
      (see §Deviations (1))
  new node-ids: 117
  697 − 12 + 117 = 802 ✔ ; the 685 unchanged pre-existing tests all pass.

$ scripts/hooks
[hooks] compileall (byte-compile package + tests)
[hooks] smoke import (package must import cleanly)
[hooks] ruff (lint)
All checks passed!
[hooks] mypy (type check: package + tests)
Success: no issues found in 42 source files
[hooks] all checks passed

No-network run (throwaway plugin, same approach as Phases 4-6):
$ cat /tmp/opencode/socket_guard.py        # autouse fixture patching
    socket.socket.connect / socket.socket.connect_ex /
    socket.create_connection / socket.getaddrinfo -> raises AssertionError
$ PYTHONPATH=/tmp/opencode python -m pytest \
      /tmp/opencode/test_socket_patch_sanity.py -p socket_guard -q
4 passed in 0.02s                          # the patch FIRES on all 4 entry points
$ PYTHONPATH=/tmp/opencode python -m pytest tests/ -p socket_guard -q
802 passed in 3.44s                        # full suite, zero sockets, still green
```

## 6. Behaviour invariants

- [x] **Isolation:** purging chat A leaves chat B untouched.
- [x] **Total reset:** after purge a chat equals a fresh `SessionState()`.
- [x] **Timeout invariant:** the transport client timeout (30.0 s default)
      exceeds `LONG_POLL_TIMEOUT` (10 s), so a long poll is never client-cut
      mid-window. Confirm both constants and assert `DEFAULT_TIMEOUT >
      LONG_POLL_TIMEOUT`.
- [x] **Never wedged:** every stage failure path still degrades (apology /
      retry next update) and never raises into `poll_once`.

**Evidence:**
```
Isolation:
  test_purge_is_per_chat_isolated, test_purging_one_chat_leaves_another_untouched,
  test_sessions_never_leak_between_chats, test_pending_questions_are_isolated_per_chat_id
  component .../restart_mid_interview_and_transient_blip (chat 2 '/restart' →
    chat 1 still done with photo/narration/script intact) — all green.
  REPL: purge chat1 → chat2 photo kept True, phase interviewing, pending 'Q1'.

Total reset:
  REPL: after purge state == SessionState() → True
  test_purge_mid_interview_reports_every_discarded_item asserts every field
  (phase awaiting_photo, photo/interview/dossier/script/narration None,
   flags False, last_media_group_id None).

Timeout invariant:
  telegram_documentaries/transport.py:36   DEFAULT_TIMEOUT = 30.0
  telegram_documentaries/polling.py:46     LONG_POLL_TIMEOUT = 10
  REPL: assert 30.0 > 10  → "timeout invariant DEFAULT_TIMEOUT > LONG_POLL_TIMEOUT: OK"
  committed test: tests/unit/test_transport.py::test_http_socket_timeout_outlasts_the_long_poll_hold
  (asserts DEFAULT_TIMEOUT >= LONG_POLL_TIMEOUT — Phase-1 legacy; see §Deviations (3)).

Never wedged (stage failures degrade, nothing raises into poll_once):
$ python -m pytest tests/ -k "vision_error or interview_failed or conversion_failed
    or script_failed or narrator_failed or send_failure or reply_failed or apology
    or retryable or keeps_acking or crash or degrade or raises" -q
58 passed, 744 deselected in 0.67s
  (covers vision/photo-fetch/download/gate apologies, interview/conversion/
   scripter/narrator *_failed paths, reply_failed, and
   test_send_failure_is_logged_and_the_loop_keeps_acking — the loop keeps
   acking after a send failure).
  Plus test_gateway_survives_a_non_normalised_transport_crash and
  test_non_object_get_updates_payload_is_logged_and_never_crashes at the loop level.
```

## 7. Spec/doc reconciliation

- [x] `SPECS/ROADMAP.md` Phase 7 requires the explicit reset routine (three
      actions), the guard matrix and the transient-failure policy; decisions
      (in-memory purge, single-threaded, retry-with-backoff) recorded; status
      updated.
- [x] `SPECS/TECH.md` documents reset semantics, command recognition, the
      `getUpdates` retry policy, the single-threaded decision and the timeout
      invariant.
- [x] `SPECS/MISSION.md` states the reset routine's three actions.
- [x] `README.md` reflects the user-facing behaviour.
- [x] `validation.md` §Deviations lists any difference between this spec and
      what shipped, with the user's approval.

**Evidence:** docs updated at verification (2026-10-15) —
`SPECS/ROADMAP.md` §7 now **Status: complete** with the delivered summary
(the three-action `reset_chat` routine, command recognition, the 61-cell guard
matrix, transient-only `getUpdates` retry) and the four locked decisions
(in-memory purge, single-threaded, retry-with-backoff, permanent-errors-surface)
plus the verification numbers; `SPECS/TECH.md` gained **§Resilience (Phase 7)**
(command recognition, guard matrix, retry policy, single-threaded decision,
timeout invariant) and its §Session state reset-semantics bullet was rewritten
around `purge` → `PurgeReport` → `reset_chat` (three actions, `session_reset`
audit, stage-module `*_failed` exception); `SPECS/MISSION.md` §In scope and
§Success criteria now state the three named actions; `README.md` was updated
(Phase 7 status, the `/start`/`/restart` bullet with recognition rules and the
three actions, transient-retry + single-threaded + timeout notes in the
long-polling section, exit codes, the `session_reset`/`get_updates_retry` log
events, the Phase 7 test note, the Roadmap section). §Deviations below.

## Deviations

Filled at verification. All three are non-blocking; none changes the
behaviour required by this spec. (1) is a direct consequence of decision 5
("retry only transient `getUpdates` failures"), which the user approved on
2026-10-15 (spec revision commit `06321e8`).

1. **The old CLI exit-1-on-`getUpdates`-failure test was rewritten (not listed
   by name in the plan).** Plan §4 only named
   `test_get_updates_transport_failure_propagates_loudly` for replacement, but
   `test_cli.py::test_main_logs_get_updates_failure_and_exits_non_zero`
   asserted the *same* old contract (any `getUpdates` `TelegramApiError` →
   exit 1) and would now fail. It was rewritten as
   `test_main_rides_out_a_transient_get_updates_failure` (a transient blip →
   the loop retries, the run completes, **exit 0**), which is the required
   behaviour under the approved decision. **Consequence:** there is no
   CLI-level test that a *permanent* coded `getUpdates` failure exits 1. That
   path is verified by (a) `test_permanent_coded_errors_propagate_unretried`
   proving the error escapes `poll_once`, and (b) code inspection that
   `cli.main` wraps `run_polling` in `except (TelegramApiError, OSError): …
   return 1` with a `gateway_failed` ERROR log (`cli.py:243-262`), alongside
   the still-green `test_main_exits_non_zero_when_the_get_me_probe_fails`.
   *Judgement: coverage note, not a behaviour gap — no checklist item fails.*
2. **Test-count accounting.** The "all 697 pre-existing tests still pass"
   clause is met with the spec's documented-count allowance: 685 unchanged
   pre-existing tests pass as-is; 12 pre-existing node-ids were removed
   deliberately — 10 `reset` → `purge` renames (decision 6), the plan-required
   polling-contract replacement, and the CLI test in deviation (1); 117 new
   tests bring the total to **802**.
3. **Timeout-invariant assertion strength.** This checklist asks to *assert*
   `DEFAULT_TIMEOUT > LONG_POLL_TIMEOUT`; the strict assertion was performed at
   verification (`30.0 > 10`, recorded in §6) and holds. The *committed*
   test — `test_http_socket_timeout_outlasts_the_long_poll_hold`, which
   predates this phase — asserts `>=`. Not a behaviour defect (the constants
   are strictly ordered); flagged so the build agent can decide whether to
   tighten that one-line assertion. **Not changed by the verifier** (test
   changes are for the implementer).

Also of note (within plan allowances): `tests/conftest.py` gained a `_capture`
log-capture context manager (the plan allowed a conftest change "only if a new
fake is needed"); no new fake transport/gate/LLM was added.
