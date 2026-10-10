# Plan — The Interviewer (Phase 3)

Red/Green TDD: every task group writes its failing tests **first**, then the
code, then runs `scripts/test` until green. `scripts/hooks` (compileall +
smoke import + ruff + mypy) is run at the end of every group. Tests never
touch Telegram or Gemini. **All 244 pre-existing tests must stay green
throughout** — especially the `vision.py` redaction suite, which proves the
refactor in group 2 changes nothing.

New modules: `gemini.py` (shared Gemini JSON client), `interviewer.py`
(port + Gemini adapter + interview routing). Modified: `session.py`,
`vision.py`, `bouncer.py`, `config.py`, `defaults.py`, `polling.py`,
`cli.py` + their tests and `conftest.py`. No third-party HTTP dependency.

---

## 1. Session extension (`session.py`)

Red:
- Extend `tests/unit/test_session.py`: default state is still
  `awaiting_photo` with `version == 2`; phases `interviewing` and `done`
  exist; `interview` defaults to `[]` and `dossier` to `None`; new methods:
  - `start_interview(chat_id)` → phase `interviewing`, log still empty.
  - `record_interview_answer(chat_id, question, answer)` → appends a
    `QaPair` (question-then-answer, in order).
  - `appoint_dossier(chat_id, dossier)` → sets `dossier` and phase `done`.
  - `reset` clears `interview` and `dossier` along with the photo.
  - `QaPair` and `Dossier` parse/validate; wrong types raise
    `ValidationError`.
  - two `chat_id`s never share an interview.

Green:
- `QaPair(BaseModel)`: `question: str`, `answer: str`.
- `Dossier(BaseModel)`: `summary: str`, `suggested_animal: str`,
  `animal_reason: str | None = None`.
- `Phase`: add `interviewing`, `done`.
- `SessionState`: `version = 2`, `interview: list[QaPair] = []`,
  `dossier: Dossier | None = None`.
- `SessionStore`: the three new methods (single owner of read/write/reset).

Run `scripts/test` + `scripts/hooks`.

## 2. Shared Gemini JSON client (`gemini.py`) + vision refactor

Red: no new behaviour — the goal is **all 244 existing tests still pass
unchanged**. Test scaffolding only:
- New `tests/unit/test_gemini.py` (fake `urlopen`): the client POSTs to
  `…/v1beta/models/<model>:generateContent?key=<key>`, returns the parsed
  candidate text, normalises HTTP/network/protocol/malformed-JSON/schema-
  invalid/error-payload failures into one typed `GeminiError`, and **redacts
  the API key** from every raised message (including the escaped-form,
  control-character and `from None` cases the Phase 2 sweep covers).

Green:
- `gemini.py`:
  - `GeminiError(Exception)` — message guaranteed redacted.
  - `GeminiJsonClient(api_key, *, model, base_url, timeout, urlopen)`:
    `generate_json(request_body, response_schema) -> dict[str, Any]` —
    POST, read, parse top-level JSON, error-payload check, candidates →
    text → `json.loads`, validate fields against the schema via Pydantic,
    single `_redact` implementation (plain + `unicode_escape` forms), all
    `from None`.
- Refactor `vision.py` to use `GeminiJsonClient` for its `generateContent`
  call and `HumanVerdict` validation. `VisionError` stays the adapter's
  domain error; its message is produced from the (already redacted)
  `GeminiError`. **API shape of `GeminiVisionGate` unchanged.**

Run `scripts/test` (expect the full 244 green — *especially*
`test_vision.py` and `test_token_redaction.py`) + `scripts/hooks`.

> Note: if the refactor turns out to need more than one Red/Green pass, keep
> it honest — this group is complete only when the existing suites pass
> byte-for-byte against the same assertions.

## 3. Interviewer port + Gemini adapter (`interviewer.py`)

Red:
- `tests/unit/test_interviewer.py` (fake `urlopen`):
  - `next_question` with a valid `{"question": "..."}` reply → returns the
    question string; request body carries the researcher persona, the
    transcript (Q&A pairs as alternating roles), and the JSON schema.
  - `summarize` with a valid `{"summary", "suggested_animal",
    "animal_reason"}` reply → `Dossier`.
  - malformed / schema-invalid / HTTP / network / error payload →
    `InterviewError`; API key redacted in every raised message.

Green:
- `InterviewError(Exception)`.
- `InterviewLLM(Protocol)`: `next_question(transcript: list[QaPair]) -> str`;
  `summarize(transcript: list[QaPair]) -> Dossier`.
- `GeminiInterviewer(api_key, *, model=DEFAULT_INTERVIEW_MODEL, base_url=…,
  timeout=…, urlopen=…)` wrapping `GeminiJsonClient`:
  - ask prompt: "investigative, playful, slightly eccentric documentary
    researcher"; transcript as `user`/`model` alternation; final user part
    "ask question N of 5 — exactly one question"; schema
    `{"question": str}`.
  - synthesize prompt: same persona; categories requested — sleep cycles,
    territory marking, favourite forage — plus suggested animal and
    justification; schema `{"summary", "suggested_animal",
    "animal_reason"}`.
  - `next_question` and `summarize` normalise `GeminiError` → redacted
    `InterviewError` raised `from None`.

Run `scripts/test` + `scripts/hooks`.

## 4. Interview routing (`interviewer.py`)

Red — `tests/unit/test_interviewer.py` + `tests/conftest.py` (`FakeLLM`
with scripted questions/dossiers/errors; existing `FakeGate`,
`FakeTransport` unchanged):
- `start_interview(transport, chat_id, *, session, llm, logger)`:
  - sets phase `interviewing`, asks Q1 via `llm.next_question([])`, sends it
    — one message, one question.
  - `llm.next_question` raises `InterviewError` → apology sent, phase stays
    `gate_passed` (retryable), no crash.
- `handle_update` while `interviewing`:
  - text answer → record; if fewer than 5 answers, ask the next question
    (transcript of recorded pairs); if 5 answers, **synthesize**.
  - non-text (photo/sticker/voice/document) or empty text → re-ask the
    current question (send it again), no state advance.
  - dispatch failure after recording (question send fails) → the commit of
    the answer is **rolled back** so the interview never advances past what
    the user saw; apology + re-ask.
  - synthesis failure → apology, phase stays `interviewing`, log full; next
    message retries synthesis.
- `handle_update` while `done`: non-command input → light
  "observation complete" reply (no crash, no state change).
- No exception ever escapes into the polling loop.

Green:
- `interviewer.py` routing functions implementing the behaviour above, using
  `SessionStore` methods from group 1, `MAX_ANSWER_LENGTH` truncation of
  answers, structured events (`question_asked`, `answer_recorded`,
  `dossier_created`, `interview_failed`, …).

Run `scripts/test` + `scripts/hooks`.

## 5. Bouncer handoff + wiring (bouncer, polling, config, CLI)

Red:
- `tests/unit/test_bouncer.py` (extend): pass path now also calls
  `start_interview` → success line **and** Q1 are sent; `gate_passed` +
  any update → interview start retried (or handled by the interviewer);
  `interviewing`/`done` updates are delegated to the interview handler;
  `/start` always resets even mid-interview.
- `tests/unit/test_polling.py`: `llm` threaded to the handler.
- `tests/unit/test_config.py`: `get_interview_model()` env-`GEMINI_INTERVIEW_MODEL`-beats-file, blank falls back to `gemini-3.1-flash-lite`.
- `tests/unit/test_cli.py`: interview adapter built from the API key and
  model, threaded into polling.

Green:
- `bouncer.py`: accept `llm`; dispatch on phase (`gate_passed`/
  `interviewing`/`done` → interviewer; else the Phase 2 flow); success path
  calls `interviewer.start_interview`.
- `polling.py`: thread `llm`.
- `config.py`: `get_interview_model()`; `defaults.py`: `DEFAULT_INTERVIEW_MODEL`,
  `DEFAULT_QUESTION_COUNT = 5`, `MAX_ANSWER_LENGTH = 400`.

Run `scripts/test` + `scripts/hooks`.

## 6. Component flow + docs + verification

Red/Green:
- `tests/component/test_interviewer_flow.py` over the real `poll_once` with
  fakes: pass → success + Q1; answer × 5 (asserting exactly one question
  sent per update and each re-ask re-sends the *current* question) →
  dossier message sent + stored (`phase == done`, `dossier.suggested_animal`
  present); photo mid-interview → re-ask, no advance; `/start` mid-interview
  → reset + photo prompt; post-`done` text → light reply. No network.

Docs (validated at verification):
- `README.md`: Phase 3 status, interviewer behaviour, `GEMINI_INTERVIEW_MODEL`,
  5-question interview, dossier handoff.
- `SPECS/TECH.md`: Interviewer stage description (typed `InterviewLLM` port;
  Interviewer is the orchestrator of its own turn-taking via the shared
  client) with user approval.
- `SPECS/ROADMAP.md` / `MISSION.md`: mark Phase 3 complete when verified.
- Walk `validation.md`.

Finish: `scripts/test` + `scripts/hooks` green; commit; push; open PR to
`main`.

## Verification notes (filled at verification)

Verified 2026-10-10. All six groups shipped; `scripts/test` = **367 passed**
and `scripts/hooks` green. The plan was followed with the deviations below
(full evidence in `validation.md`; accepted deviations #1–#8 recorded there).

### Group-by-group

- **Group 1 (session):** as planned, plus `ask_question` and the
  `pending_question` field, and an optional `question` argument on
  `start_interview` — all in service of the delayed commit (deviation #2).
- **Group 2 (shared client + vision refactor):** `gemini.py` created as
  planned; the existing suites stayed green. Confirmed behaviour-neutral:
  `test_vision.py` and `test_token_redaction.py` are byte-identical to `main`
  (0-line diff) and pass. The client gained an injectable `logger` beyond the
  planned signature (deviation #3). This group's "244 unchanged" is true for
  the refactor; later groups deliberately rewrote a few Phase 2 handoff
  assertions (deviation #4).
- **Group 3 (port + adapter):** as planned. Prompts carry the researcher
  persona as `systemInstruction` (never a user turn) and the transcript as
  strict user/model alternation ending in one user turn. Added the
  `MAX_QUESTION_LENGTH` guard (deviation #6).
- **Group 4 (routing):** as planned, except the answer "rollback" is a
  **delayed commit** (deviation #1), not a store undo. `pending_question`
  makes re-asks exact and LLM-free. Non-answers, ask/synthesize failures,
  post-`done` replies and the no-escape guarantee all verified.
- **Group 5 (handoff + wiring):** as planned: `bouncer.handle_update` gained
  `llm` and delegates `gate_passed`/`interviewing`/`done`; `polling`/`cli`
  thread the interviewer. `get_interview_model()` and the defaults shipped.
- **Group 6 (component + docs):** `test_interviewer_flow.py` (4 tests) drives
  the real `poll_once`; docs reconciled at verification (this section, plus
  ROADMAP/MISSION/TECH/README).

### Deviations from this plan

1. Delayed commit replaces "roll back" (group 4) — same observable contract.
2. Store surface larger than the three methods in group 1 (`ask_question`,
   `pending_question`, optional `start_interview(question=…)`).
3. `GeminiJsonClient` gained an injectable `logger`; `vision.py` re-exports
   the moved names for compatibility.
4. "All 244 pre-existing tests unchanged" was not literally maintained across
   *all* groups: the handoff intentionally changed pass-path assertions
   (success + Q1, `interviewing`), and handlers gained an `llm` parameter.
5. ROADMAP/MISSION "5–7 questions" reconciled to the delivered exactly 5
   (user decision).
6. Added `MAX_QUESTION_LENGTH` (4096) guard, not listed in the plan.
7. `cli.py` description / `__init__.py` docstring Phase-2 wording corrected at
   verification (documentation fix; no test depended on the strings).

### Not done by the verifier

Commit/push/PR and the user merge (merge gate) are the build agent's/user's
steps, not the verifier's. The optional live manual smoke was not run (no
credentials/consent).
