# Requirements — The Interviewer (Phase 3)

## Context

- **Phase 2 (The Bouncer) is merged to `main`** (PR #2): a typed `VisionGate`
  port + Gemini REST adapter; `session.py` (in-memory, per-`chat_id`,
  versioned); `media.py` photo intake; `bouncer.py` routing; graceful
  degradation everywhere; `scripts/test` = 244 passed.
- **This is Phase 3** from `SPECS/ROADMAP.md`: "Sequential stateful Q&A: 5–7
  questions, one at a time, state keyed by `chat_id`; builds a behavioural
  dossier; outputs a suggested animal."
- **Branch:** `feature/2026-10-10-interviewer` (created off `main`).
- Model per `SPECS/TECH.md`: **Gemini 3.1 Flash Lite**.
- The Bouncer's success path currently sends "Hold tight, the interview is
  coming." and stops. Phase 3 makes the interview **actually come**: the
  handoff is automatic the moment a portrait passes the gate.

## Goal

A user who passed the Bouncer is taken through a **sequential, one-question-
at-a-time** interview. After exactly 5 answers, a final synthesis step
produces a **behavioural dossier** — a rich descriptive summary of the
subject's quirks (sleep cycles, territory marking, favourite forage, …) plus
a **suggested animal** — which is both **sent to the chat** and **stored in
the session** for Phase 4 (Converter).

## Decisions locked with the user (2026-10-10)

1. **Gemini generates each question.** Each turn, the code sends the persona
   prompt + the transcript so far to Gemini and receives **exactly one next
   question** (JSON schema: `{"question": "..."}`), validated by schema, not
   regex. The code still owns turn-taking, the 5-question cap, and session
   phases. Malformed model output degrades to an apology + re-ask of the
   current question — it never advances the interview and never crashes.
2. **Summary visibility.** On completion, the dossier + suggested animal is
   sent to the chat **and** stored on the session state for Phase 4.
3. **Fixed at exactly 5 questions**, then synthesis. (No config knob — YAGNI;
   the user declined a configurable count.)
4. **Automatic handoff.** When a portrait passes the gate, the Bouncer sends
   its success line and the Interviewer immediately asks question 1 — the
   user's very next message is their first answer. `gate_passed` becomes a
   *transient* phase: it only survives a degraded start, and any next update
   retries the start.
5. **Framework (drift by precedent).** Consistent with Phase 2's
   user-approved decision, the Interviewer is **not** built on Google ADK: it
   is a **typed `InterviewLLM` port** with a Gemini REST adapter, sharing one
   **common Gemini JSON client** extracted from `vision.py` (so the
   secret-redaction logic exists exactly once, not twice). ADK remains
   deferred (already reconciled in `SPECS/TECH.md`); `TECH.md` §Architecture
   will note the Interviewer as the orchestrator stage with this port.

## Scope (in)

- **Session extension** (`session.py`): `Phase` gains `interviewing` and
  `done`; `SessionState` gains the interview log (`list[QaPair]` of
  question/answer) and `dossier: Dossier | None`; `SESSION_VERSION` bumped to
  2 (in-memory only — no migration needed, bump is the contract). New store
  methods: `start_interview`, `record_interview_answer`, `appoint_dossier`.
- **Shared Gemini JSON client** (new `gemini.py`): the REST plumbing
  currently private to `vision.py` — endpoint build, POST, HTTP/network/
  protocol normalisation, key redaction (`from None`), candidate-text
  extraction, `responseSchema` validation — extracted once and reused by the
  vision and interview adapters. `vision.py` is **refactored to use it with
  no behaviour change** (all 244 existing tests must stay green).
- **`InterviewLLM` port** + **`GeminiInterviewer` adapter** (`interviewer.py`):
  - `next_question(transcript) -> str` — one question, schema-validated.
  - `summarize(transcript) -> Dossier` — final synthesis, schema-validated
    (`summary`, `suggested_animal`, `animal_reason`).
  - Same secret guarantees as the vision adapter: redacted messages, `from
    None`, typed `InterviewError`.
- **Interview routing** (`interviewer.py`): `start_interview`, `handle_update`
  and the synthesis step:
  - One question at a time; the current question is re-sent when the user
    sends anything that isn't an answer (photo/sticker/voice/document/empty
    text during `interviewing`), and again on a degraded dispatch — the
    interview never advances past what the user actually saw.
  - Exactly 5 answers collected → synthesis → dossier sent + stored → phase
    `done`.
  - After `done`: non-command input gets a light "observation complete —
    `/restart` for a new subject" reply.
  - Degradation: `InterviewError` on ask/synthesize → apology + re-ask /
    retry on next message, session stays usable, nothing raises into the
    polling loop.
- **Bouncer handoff** (`bouncer.py`): after the pass, call
  `interviewer.start_interview` (Q1 follows the success line). Route any
  update arriving in `gate_passed` / `interviewing` / `done` to the
  interview handler; `/start` / `/restart` still reset everything.
- **Config** (`config.py`, `defaults.py`): `DEFAULT_INTERVIEW_MODEL =
  gemini-3.1-flash-lite`, `DEFAULT_QUESTION_COUNT = 5`,
  `MAX_ANSWER_LENGTH = 400` in `defaults.py`; `get_interview_model()` reading
  optional `GEMINI_INTERVIEW_MODEL` (env-beats-file, like the vision model).
- **Persona** (in the adapter's prompts): *investigative, playful, slightly
  eccentric documentary researcher*; the synthesis prompt must elicit the
  dossier categories the user specified — sleep cycles, territory marking,
  favourite forage — plus the suggested animal and its justification.
- **Wiring** (`polling.py`, `cli.py`): resolve interview model, build
  `GeminiInterviewer` from the API key, thread `llm` through to the handler.
- **Structured logging**: events `interview_started`, `question_asked`,
  `answer_recorded`, `dossier_created`, `interview_failed`,
  `interview_skipped`, plus existing patterns; never log dossiers verbatim if
  they echo unredacted user text with secrets (dossier is user-derived text;
  log the suggested animal, not the full summary).

## Out of scope (YAGNI)

- Converter, Scripter, TTS (Phases 4–6) — the dossier is *stored*, nothing
  consumes it yet.
- Retry/backoff/timeout policy (Phase 7) — single-attempt graceful
  degradation is in scope, resilience is not.
- Full `/restart` semantics (Phase 7) — reset already exists and is reused.
- Persistent/durable conversation storage (MISSION: in-memory only).
- Configurable question count (user decision 3).

## Contracts at boundaries (typed, untrusted input)

| Boundary | Type | Failure behaviour |
| --- | --- | --- |
| User answer text | `str`, truncated to `MAX_ANSWER_LENGTH` before any use | over-length → truncate (logged), never raise |
| Non-answer content mid-interview (photo/sticker/voice/document) | `Message` | re-ask current question, no state advance |
| Gemini `next_question` output | `{"question": str}` via Pydantic | malformed/HTTP/network → `InterviewError`, apology + re-ask |
| Gemini `summarize` output | `Dossier` via Pydantic | malformed → `InterviewError`, apology, retry on next message |
| Session | `SessionState` per `chat_id`, `version = 2` | unknown chat → fresh default (`awaiting_photo`) |
| Transcript sent to Gemini | `list[QaPair]`, bounded (≤ 5, ≤ `MAX_ANSWER_LENGTH`) | never unbounded |

`chat_id` stays `int` end-to-end. The interview log is **append-only**
question-then-answer pairs; the question count is derived from the log, never
from message guessing.

## Non-negotiables

- **Per-`chat_id` isolation** — never leak one user's interview/dossier to
  another.
- **Never log or commit secrets** — same guarantee as Phase 2, now for two
  adapters sharing one redaction implementation.
- **No test contacts Telegram or Gemini** — fakes only.
- **One question per message, ever.** No question dump, no back-to-back
  questions in a single send.

## Acceptance criteria (from ROADMAP Phase 3)

1. Full questionnaire completes **in order** — dossier summary + animal
   suggestion delivered to the chat and stored on the session.
2. Answers out of order (photo mid-interview, extra text, `/start` mid-
   interview) never corrupt the session; non-answers re-ask the current
   question.
3. `scripts/test` and `scripts/hooks` are green, including all 244 pre-
   existing tests (the `vision.py` refactor changes no Bouncer behaviour).

## Delivered (filled at verification)

Verified 2026-10-10 on branch `feature/2026-10-10-interviewer`:
`scripts/test` = **367 passed**; `scripts/hooks` green (compileall + smoke
import + ruff + mypy "no issues found in 33 source files"); the full suite also
passes with all socket entry points patched to raise (no network I/O). The
`vision.py` refactor is behaviour-neutral: `test_vision.py` and
`test_token_redaction.py` are byte-identical to `main` (which collects 244
tests). Full checklist and per-item evidence in `validation.md`.

### What shipped

- `session.py`: `Phase` gains `interviewing`/`done`; `SESSION_VERSION = 2`;
  `SessionState` adds `interview: list[QaPair]`, `pending_question: str | None`
  and `dossier: Dossier | None`; `SessionStore` adds `start_interview`
  (optional question), `ask_question`, `record_interview_answer` and
  `appoint_dossier`; `QaPair`/`Dossier` Pydantic models.
- `gemini.py` (new): the shared `GeminiJsonClient` (single REST plumbing +
  single API-key redaction), used by both adapters.
- `vision.py`: refactored onto `GeminiJsonClient`; public API unchanged.
- `interviewer.py` (new): typed `InterviewLLM` port + `GeminiInterviewer`
  adapter (`next_question`, `summarize`), and the routing
  (`start_interview`, `handle_update`) that owns the phase machine, the
  exactly-5 cap and the single-question-per-message rule.
- `bouncer.py`: automatic handoff — the pass path sends the success line then
  Q1; `gate_passed`/`interviewing`/`done` updates are delegated to the
  Interviewer; `/start`/`/restart` still reset everything.
- `config.py`/`defaults.py`: `DEFAULT_INTERVIEW_MODEL = gemini-3.1-flash-lite`,
  `DEFAULT_QUESTION_COUNT = 5`, `MAX_ANSWER_LENGTH = 400`,
  `get_interview_model()` reading optional `GEMINI_INTERVIEW_MODEL`
  (env-beats-file).
- `polling.py`/`cli.py`: one `GeminiInterviewer` built from the API key and
  interview model, threaded through the poll loop.
- Structured events: `interview_started`, `question_asked`,
  `answer_recorded`, `answer_truncated`, `dossier_created`,
  `interview_failed`, `interview_skipped`.

### Deviations from this spec

1. **Delayed commit instead of "roll back"** (see `validation.md` #1): the
   Q/A pair is committed only after the next question/report is sent.
   `pending_question` stores the exact shown question so re-asks make no LLM
   call. The session store therefore also adds `ask_question` and the
   `pending_question` field beyond the three methods named in §Scope.
2. **`MAX_QUESTION_LENGTH = 4096` guard** added in the adapter (not specified):
   an empty/whitespace or unsendable over-long question is unusable output and
   raises `InterviewError`, so the retry path takes a fresh draw.
3. **Exactly 5, not "5–7".** Already locked in this spec (decision 3); the
   ROADMAP/MISSION wording was reconciled to match.
4. No other scope drift: Phase 4+ consumers, retry/backoff policy, durable
   storage and a configurable question count all remain out of scope.
