# Validation — The Interviewer (Phase 3)

How we know Phase 3 is correct and safe to merge. A step is "done" only when
the evidence is recorded and the user has approved any spec reconciliation.

## 1. Automated checks

- [x] `scripts/test` — full suite green, including the new `test_gemini` and
  `test_interviewer` suites, component `test_interviewer_flow`, and **all 244
  pre-existing tests unchanged** (the `vision.py` refactor in plan group 2
  must not change any Bouncer behaviour).
  *Verified 2026-10-10 on branch `feature/2026-10-10-interviewer`
  (Python 3.11.2 / pytest 9.1.1): `367 passed in 2.31s`. New suites:
  `test_gemini.py` 24, `test_interviewer.py` 65,
  `component/test_interviewer_flow.py` 4. The refactor is proven
  behaviour-neutral by the two adapter suites that did **not** change:
  `test_vision.py` (30) and `test_token_redaction.py` (19) are byte-identical
  to `main` (`git diff main -- …` = 0 lines) and pass. `main` collects exactly
  `244 tests` (extracted via `git archive main` and run in isolation). Caveat:
  the *literal* count of unchanged Phase 2 tests is 244 minus the handful whose
  assertions the Phase 3 handoff intentionally rewrote (pass path now sends
  success + Q1 and lands in `interviewing`) — see §"Deviations" #4.*
- [x] `scripts/hooks` — `compileall` + smoke import + `ruff` + `mypy` green.
  *Verified 2026-10-10: compileall OK; smoke import OK; `ruff check .` —
  "All checks passed!"; `mypy telegram_documentaries tests` — "Success: no
  issues found in 33 source files"; final line "all checks passed".*
- [x] No test performs network I/O: every Telegram and Gemini interaction
  goes through `FakeTransport` / `FakeGate` / `FakeLLM` / fake `urlopen`.
  *Verified 2026-10-10: the full suite was re-run with `socket.socket.connect`,
  `connect_ex`, `socket.create_connection` and `socket.getaddrinfo` patched to
  raise (a throwaway pytest plugin), and the patch was sanity-checked to fire.
  Result: `367 passed in 3.28s` — no test, fixture or helper opens a socket.
  Every adapter takes an injectable `urlopen`; the interview model is the
  scripted `FakeLLM` and the vision model the scripted `FakeGate`.*

## 2. Behaviour checklist (maps to ROADMAP acceptance)

- [x] Portrait passes the gate → success line followed by **exactly one
  question** (Q1); the session shows `phase == interviewing` and an empty-to-
  growing interview log.
  *Evidence: `test_bouncer.py::test_photo_of_a_human_passes_and_retains_the_image`
  asserts the send list is exactly `[SUCCESS_REPLY, "Q1"]`, `llm.next_calls ==
  [[]]`, `phase is Phase.interviewing`; `interviewer.py::test_start_interview_sends_q1_and_advances_to_interviewing`
  asserts `interview == []` (growing) and `pending_question == "Q1"`; component
  `test_interviewer_flow.py::test_full_interview_delivers_and_stores_the_dossier`
  shows `sent_per_turn == [2, 1, 1, 1, 1, 1]` (success+Q1, then one message per
  answer).*
- [x] Each user answer is recorded (question-then-answer pair, in order) and
  the *next* question arrives **one at a time** — never more than one
  question per message, never a question dump.
  *Evidence: `test_session.py::test_record_interview_answer_appends_question_then_answer_in_order`;
  `test_interviewer.py::test_first_answer_is_recorded_and_question_two_is_asked`
  (`interview == [QaPair("Q1", …)]`, next send is `"Q2"`);
  `test_five_answers_complete_the_interview_and_store_the_dossier` asserts
  `len(sendMessage) == before + 1` per turn; component `sent_per_turn` above and
  `replies[1:6] == ["Q1".."Q5"]`.*
- [x] After exactly **5** answers, a synthesis step produces a `Dossier`
  (`summary`, `suggested_animal`, optional `animal_reason`), the dossier
  **message is sent to the chat**, the dossier is **stored on the session**
  (`dossier` set, `phase == done`).
  *Evidence: `test_interviewer.py::test_five_answers_complete_the_interview_and_store_the_dossier`
  (`state.phase is Phase.done`, `state.dossier == dossier`, `interview` has 5
  ordered pairs, `summarize_calls` got the full 5-pair transcript, report text
  includes the animal + summary); component `test_full_interview_delivers_and_stores_the_dossier`
  asserts `replies[6]` contains "House cat" and "Snores on the sofa". The cap is
  `DEFAULT_QUESTION_COUNT == 5` (`defaults.py`).*
- [x] Non-answer content while `interviewing` (photo / sticker / voice /
  document / empty text) re-asks the **current** question and advances
  nothing.
  *Evidence: `test_interviewer.py::test_photo_mid_interview_reasks_the_current_question_without_advancing`
  (re-sends `"Q3"`, log unchanged, **no** new `next_question` call),
  `test_non_text_content_mid_interview_reasks_the_current_question[document|sticker|voice]`,
  `test_empty_text_mid_interview_reasks_the_current_question[""|"   "]`,
  `test_pending_question_survives_non_answer_re_asks_until_answered`; component
  `test_photo_mid_interview_reasks_the_current_question_without_advancing`
  (`[SUCCESS_REPLY, "Q1", "Q2", "Q2"]`).*
- [x] A failed question dispatch rolls the just-recorded answer back so the
  interview never advances past what the user saw.
  *Evidence: `test_interviewer.py::test_next_question_send_failure_does_not_commit_and_reasks`
  (sends `[Q1, APOLOGY, Q1]`, `interview == []`, phase still `interviewing`);
  `test_dossier_send_failure_is_retried_on_the_next_message`
  (`interview` stays 4, retry then completes). Implemented as a **delayed
  commit** rather than a store-level undo — same observable behaviour; see
  §"Deviations" #1.*
- [x] `InterviewError` on ask or synthesize → apologetic reply, loud
  structured log, session stays usable, nothing raises into the polling loop;
  the next message retries cleanly.
  *Evidence: `test_start_interview_ask_failure_apologises_and_stays_retryable`
  (apology, phase stays `gate_passed`, `interview_failed` event, returns
  `False`); `test_next_question_ask_failure_apologises_and_retries_next_message`
  (next message lands the answer and asks `"Q2"`);
  `test_synthesis_failure_apologises_keeps_interviewing_and_retries` (phase
  `interviewing`, log length 4, next message → `done`);
  `test_interviewing_state_without_a_pending_question_fails_loud`;
  `test_unexpected_error_is_caught_and_degrades_politely`;
  `test_keyboard_interrupt_is_not_swallowed`. The
  `handle_update` wrapper catches everything else and keeps the loop alive.*
- [x] After `done`, non-command input gets a light "observation complete"
  reply; no crash, no state change.
  *Evidence: `test_interviewer.py::test_done_phase_replies_lightly_without_touching_state`
  and `test_a_sixth_answer_after_completion_is_a_light_no_op` (5 pairs,
  dossier and phase unchanged, **no** LLM call); component
  `test_text_after_completion_gets_a_light_reply_without_state_change`;
  `test_bouncer.py::test_text_after_completion_is_delegated_to_the_interviewer`
  (gate never re-consulted).*
- [x] `/start` / `/restart` mid-interview resets everything (phase →
  `awaiting_photo`, photo *and* interview log *and* dossier purged).
  *Evidence: `test_bouncer.py::test_start_mid_interview_resets_the_whole_session`
  and `test_session.py::test_reset_purges_photo_interview_and_dossier`
  (`photo is None`, `interview == []`, `dossier is None`), plus
  `test_reset_purges_the_pending_question`; component
  `test_start_mid_interview_resets_photo_and_interview_log`.*

## 3. Contract & safety checks

- [x] `chat_id` stays an `int` session key throughout; `SESSION_VERSION` is
  bumped to 2 with the new fields versioned explicitly.
  *Evidence: `models.py` `Chat.id: int`;
  `SessionStore._sessions: dict[int, SessionState]`; every interview/route
  signature takes `chat_id: int`; `session.py` `SESSION_VERSION = 2`;
  `test_session.py::test_session_state_is_a_pydantic_model_with_a_stable_version`
  asserts `state.version == 2`. New fields (`interview`, `pending_question`,
  `dossier`) all live on the versioned `SessionState`.*
- [x] Gemini `next_question` and `summarize` outputs are parsed through typed
  Pydantic schemas (`{"question"}` and `Dossier`) — no regex decides a
  question or animal.
  *Evidence: `interviewer.py` `_Question(BaseModel)` and `Dossier` validated by
  `GeminiJsonClient.generate_json(..., schema)`; requests carry
  `responseMimeType: application/json` + `responseSchema`;
  `test_interviewer.py::test_next_question_contents_alternate_and_end_with_one_user_turn`
  asserts `generationConfig["responseSchema"] == QUESTION_SCHEMA`;
  `test_summarize_sends_the_persona_categories_and_schema` asserts
  `DOSSIER_SCHEMA`. No regex anywhere in the adapters.*
- [x] Single redaction implementation after the refactor: the API key never
  appears in any raised message or log line from **either** adapter, and the
  `vision.py` redaction sweep from Phase 2 still passes **unchanged**.
  *Evidence: one `_redact` in `gemini.py` (plain + `unicode_escape` forms), all
  raises `from None`;
  `test_gemini.py::test_every_normalised_gemini_failure_redacts_the_api_key`
  (5 branches) and `test_api_key_with_a_control_character_is_still_redacted`;
  `test_interviewer.py::test_every_normalised_interview_failure_redacts_the_api_key`
  (5 branches × 2 methods) and `test_api_key_with_a_control_character_is_still_redacted`;
  `test_vision.py` and `test_token_redaction.py` are byte-identical to `main`
  and green. The shared client's diagnostics are attributed to the adapter's
  own logger (`test_gemini.py::test_unreadable_http_error_body_warns_on_the_injected_logger`,
  `test_interviewer.py::test_unreadable_http_error_body_warns_on_the_interviewer_logger`).*
- [x] Session state and interview log are isolated per `chat_id`; no
  cross-chat leakage in tests.
  *Evidence: `test_interviewer.py::test_two_chats_never_share_an_interview`,
  `test_session.py::test_two_chats_never_share_an_interview` and
  `test_pending_questions_are_isolated_per_chat_id`,
  `test_sessions_are_isolated_per_chat_id`, and
  `test_bouncer.py::test_sessions_never_leak_between_chats`.*
- [x] User answers are bounded (`MAX_ANSWER_LENGTH`) before reaching Gemini;
  the transcript sent per ask is bounded (≤ 5 pairs).
  *Evidence: `interviewer.py` truncates text to `MAX_ANSWER_LENGTH` (400,
  `defaults.py`) and logs `answer_truncated`;
  `test_interviewer.py::test_long_answer_is_truncated_before_it_reaches_the_transcript`;
  the transcript handed to `next_question`/`summarize` is `session.interview`
  which the count-gated state machine can never push past
  `DEFAULT_QUESTION_COUNT` (5) — `test_summarize_sends_the_persona_categories_and_schema[5]`
  and `test_a_sixth_answer_after_completion_is_a_light_no_op`.*
- [x] `TELEGRAM_BOT_TOKEN` / `GEMINI_API_KEY` never appear in logs, `.env`
  remains gitignored/untracked.
  *Evidence: the redaction sweeps above assert the key is absent from `str(exc)`,
  the formatted traceback and the JSON log line; the logger-event tests assert
  dossiers log only the suggested animal, never the user-derived summary
  (`test_dossier_created_logs_the_animal_but_not_the_summary`). `git check-ignore
  -v .env` → `.gitignore:7:.env`; `.env` is untracked (only `.env.example` is
  tracked). A full-tree scan for Google/Telegram/OpenAI/private-key shapes found
  no secret in any tracked or untracked file.*

## 4. Spec reconciliation (surfacing drift)

- [x] Any deviation from `requirements.md` / `plan.md` recorded here with the
  user's approval (mirrors Phase 2's §Deviations).
  *Done 2026-10-10 — see §"Deviations recorded at verification" below.*
- [x] `SPECS/ROADMAP.md` Phase 3 marked complete (only after this checklist).
  *Done 2026-10-10 after the checklist above passed; "5–7 questions" reconciled
  to the delivered exactly-5.*
- [x] `SPECS/MISSION.md` still matches the delivered experience (no scope
  drift).
  *One wording drift reconciled 2026-10-10: the end-to-end experience now says
  **5** questions (was "5–7"), matching the locked user decision. No other
  scope change.*
- [x] `README.md` reflects Phase 3: status, interviewer behaviour, and the
  optional `GEMINI_INTERVIEW_MODEL` configuration.
  *Done 2026-10-10.*

## 5. Manual smoke (optional, live)

Only with real credentials and the user's consent — records nothing that
violates the no-secrets rule:

- [ ] `python -m telegram_documentaries` starts; the Bouncer passes a
  portrait.
- [ ] Q1 arrives immediately after the success line.
- [ ] Five answers proceed one question at a time, then the dossier + animal
  suggestion arrive in chat.
- [ ] A photo sent mid-interview re-asks the current question.

> **Not run.** No live smoke was performed at verification (no credentials /
> user consent). The whole behaviour is covered without a network by the fakes
> above, but this item remains open until a consenting user runs it.

## 6. Merge gate

- [ ] All of the above checked with evidence in the PR description.
  *Recorded here; the PR description should reference this file.*
- [ ] Branch `feature/2026-10-10-interviewer` pushed; PR opened against
  `main`.
  *Not done at verification — the verifier does not commit/push; the build
  agent performs this.*
- [ ] **User** merges (the build agent does not merge).

## Deviations recorded at verification

Recorded 2026-10-10 against `requirements.md` and `plan.md`; the user's locked
decisions (exactly 5 questions, typed port + shared client, automatic handoff)
were honoured.

1. **Delayed commit instead of "commit then roll back" (plan group 4,
   requirements "rolls the just-recorded answer back").** The Q/A pair is
   written to the transcript only *after* the next artifact (the next question,
   or the dossier report) has been successfully sent. On a dispatch failure the
   pair is never committed and the stored pending question is re-sent, so the
   interview still never advances past what the user saw — the observable
   contract holds without a store-level undo. `SessionState.pending_question`
   stores the exact question text the user is looking at, so a re-ask is
   LLM-free and reproduces the same phrasing; `record_interview_answer`
   consumes it and `ask_question` installs the successor.
2. **Session store gained more than plan group 1's three methods.**
   `plan.md` group 1 listed `start_interview`, `record_interview_answer`,
   `appoint_dossier`; the shipped `SessionStore` also adds `ask_question`, and
   `SessionState` gains `pending_question: str | None`, while
   `start_interview` takes an optional `question` argument. These support the
   delayed commit above. (Requirements §Scope already named the same three
   methods, so this is a plan-vs-code delta, not a contract change.)
3. **Shared `gemini.py` client extracted from `vision.py` (as planned in group
   2).** The client gained an injectable `logger` parameter beyond the planned
   signature so `vision.py`/`interviewer.py` diagnostics land on their own
   module loggers. `vision.py` re-exports `GeminiError`,
   `GeminiJsonClient`, `DEFAULT_BASE_URL`, `DEFAULT_TIMEOUT` and `UrlopenFn`
   for backward compatibility; its `VisionGate`/`GeminiVisionGate` API is
   unchanged.
4. **Group-2's "244 tests unchanged" holds for the refactor, not for later
   groups.** `test_vision.py` and `test_token_redaction.py` are byte-identical
   to `main` and green (the refactor is behaviour-neutral). The Phase 3 handoff
   *intentionally* rewrote some Phase 2 assertions: the pass path now sends
   `SUCCESS_REPLY` + `Q1` and lands in `interviewing` (was `gate_passed`), and
   handlers gained an `llm` parameter. Files touched for this:
   `test_bouncer.py`, `test_bouncer_flow.py`, `test_full_gateway_flow.py`,
   `test_cli.py`, `test_config.py`, `test_polling.py`, `test_session.py`,
   `conftest.py`.
5. **ROADMAP/MISSION "5–7 questions" → delivered exactly 5.** The user locked
   exactly 5 (requirements decision 3). `ROADMAP.md` Phase 3 and `MISSION.md`
   step 3 were reconciled to 5 at verification.
6. **`MAX_QUESTION_LENGTH = 4096` guard added (not in the plan).**
   `next_question` rejects an empty/whitespace or over-Telegram-limit question
   as unusable model output, so the degrade path takes a fresh draw instead of
   looping on a question Telegram cannot deliver.
7. **Automatic handoff detail.** `gate_passed` survives only a degraded start:
   `start_interview` advances to `interviewing` only once **both** the ask and
   the send have succeeded; a `gate_passed` update retries the start. Matches
   requirements decision 4.
8. **User-facing Phase-2 wording drift fixed at verification.** `cli.py`'s
   parser description and the `telegram_documentaries/__init__.py` docstring
   still said "Phase 2 gateway"; both were updated to describe the Phase 3
   Bouncer + Interviewer (no test referenced either string).
