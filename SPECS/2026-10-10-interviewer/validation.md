# Validation — The Interviewer (Phase 3)

How we know Phase 3 is correct and safe to merge. A step is "done" only when
the evidence is recorded and the user has approved any spec reconciliation.

## 1. Automated checks

- [ ] `scripts/test` — full suite green, including the new `test_gemini` and
  `test_interviewer` suites, component `test_interviewer_flow`, and **all 244
  pre-existing tests unchanged** (the `vision.py` refactor in plan group 2
  must not change any Bouncer behaviour).
- [ ] `scripts/hooks` — `compileall` + smoke import + `ruff` + `mypy` green.
- [ ] No test performs network I/O: every Telegram and Gemini interaction
  goes through `FakeTransport` / `FakeGate` / `FakeLLM` / fake `urlopen`.

## 2. Behaviour checklist (maps to ROADMAP acceptance)

- [ ] Portrait passes the gate → success line followed by **exactly one
  question** (Q1); the session shows `phase == interviewing` and an empty-to-
  growing interview log.
- [ ] Each user answer is recorded (question-then-answer pair, in order) and
  the *next* question arrives **one at a time** — never more than one
  question per message, never a question dump.
- [ ] After exactly **5** answers, a synthesis step produces a `Dossier`
  (`summary`, `suggested_animal`, optional `animal_reason`), the dossier
  **message is sent to the chat**, the dossier is **stored on the session**
  (`dossier` set, `phase == done`).
- [ ] Non-answer content while `interviewing` (photo / sticker / voice /
  document / empty text) re-asks the **current** question and advances
  nothing.
- [ ] A failed question dispatch rolls the just-recorded answer back so the
  interview never advances past what the user saw.
- [ ] `InterviewError` on ask or synthesize → apologetic reply, loud
  structured log, session stays usable, nothing raises into the polling loop;
  the next message retries cleanly.
- [ ] After `done`, non-command input gets a light "observation complete"
  reply; no crash, no state change.
- [ ] `/start` / `/restart` mid-interview resets everything (phase →
  `awaiting_photo`, photo *and* interview log *and* dossier purged).

## 3. Contract & safety checks

- [ ] `chat_id` stays an `int` session key throughout; `SESSION_VERSION` is
  bumped to 2 with the new fields versioned explicitly.
- [ ] Gemini `next_question` and `summarize` outputs are parsed through typed
  Pydantic schemas (`{"question"}` and `Dossier`) — no regex decides a
  question or animal.
- [ ] Single redaction implementation after the refactor: the API key never
  appears in any raised message or log line from **either** adapter, and the
  `vision.py` redaction sweep from Phase 2 still passes **unchanged**.
- [ ] Session state and interview log are isolated per `chat_id`; no
  cross-chat leakage in tests.
- [ ] User answers are bounded (`MAX_ANSWER_LENGTH`) before reaching Gemini;
  the transcript sent per ask is bounded (≤ 5 pairs).
- [ ] `TELEGRAM_BOT_TOKEN` / `GEMINI_API_KEY` never appear in logs, `.env`
  remains gitignored/untracked.

## 4. Spec reconciliation (surfacing drift)

- [ ] Any deviation from `requirements.md` / `plan.md` recorded here with the
  user's approval (mirrors Phase 2's §Deviations).
- [ ] `SPECS/ROADMAP.md` Phase 3 marked complete (only after this checklist).
- [ ] `SPECS/MISSION.md` still matches the delivered experience (no scope
  drift).
- [ ] `README.md` reflects Phase 3: status, interviewer behaviour, and the
  optional `GEMINI_INTERVIEW_MODEL` configuration.

## 5. Manual smoke (optional, live)

Only with real credentials and the user's consent — records nothing that
violates the no-secrets rule:

- [ ] `python -m telegram_documentaries` starts; the Bouncer passes a
  portrait.
- [ ] Q1 arrives immediately after the success line.
- [ ] Five answers proceed one question at a time, then the dossier + animal
  suggestion arrive in chat.
- [ ] A photo sent mid-interview re-asks the current question.

> **Not run** unless a consenting user with credentials does it; the whole
> behaviour is covered without a network by the fakes above.

## 6. Merge gate

- [ ] All of the above checked with evidence in the PR description.
- [ ] Branch `feature/2026-10-10-interviewer` pushed; PR opened against
  `main`.
- [ ] **User** merges (the build agent does not merge).

## Deviations recorded at verification

> To be completed at verification (mirrors Phase 2's §Deviations).