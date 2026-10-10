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

> To be completed at verification: what shipped, deviations from this plan
> with user approval (mirrors Phase 2's §Verification notes).