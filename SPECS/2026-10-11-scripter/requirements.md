# Requirements — The Scripter (Phase 5)

## Context

- **Phase 4 (The Converter) is merged to `main`** (PR #5): a typed
  `ConverterLLM` port, the hybrid portrait delivered via multipart
  `sendPhoto`, `SESSION_VERSION` 3 with the `portrait_sent` flag, the Bouncer
  as router with the same-update handoff (dossier → teaser → portrait in one
  update). `scripts/test` = 483 passed, `scripts/hooks` green, no test touches
  the network.
- **This is Phase 5** from `SPECS/ROADMAP.md`: "Gemini 3.1 Flash Lite:
  one-paragraph (~60–90 words) dramatic British-documentary narration in
  English, built from the dossier."
- **Branch:** `feature/2026-10-11-scripter` (created off `main`).
- Model per `SPECS/TECH.md` and this activity: **Gemini 3.1 Flash Lite**.
- When Phase 4 ends, a `done` chat holds the portrait already delivered
  (`portrait_sent == True`) and the session still carries the `Dossier` from
  the interview — the exact input the Scripter consumes. This phase closes the
  voice gap: the visual is complete, the narration is not.

## Goal

Immediately after the hybrid portrait is delivered, the bot generates a
**dramatic, comedic one-paragraph narration script** (~60–90 words) from the
interview dossier — written by a *revered, dramatic British wildlife
documentary narrator* who frames human quirks, bedtime habits and snacking
patterns as funny animal behaviours — **stores the raw script in the session**
(ready for Phase 6 audio synthesis) and **sends it to the chat**. The user's
activity contract: model Gemini 3.1 Flash Lite; persona enforced via the
system instruction; output exactly one paragraph of max 90 words, no markdown,
ready for voice synthesis; script captured in the state for the TTS stage.

## Decisions locked with the user (2026-10-11)

1. **Framework — typed port, not ADK.** Same drift-by-precedent as Phases
   2–4 (all previously user-approved): the Scripter is a **typed
   `ScripterLLM` port** with a Gemini REST adapter over the shared
   `GeminiJsonClient`, not a Google ADK agent. ADK remains deferred
   (`SPECS/TECH.md`: "if a later phase adopts it, it will wrap these same
   ports"). A single text generation call gains nothing from a framework, and
   it keeps "pydantic is the only runtime dependency" true.
2. **Delivery & state — after the portrait, stored, once-only.** The script
   is generated in the **same update** that delivers the portrait (no user
   input); the raw script string is **stored on the session**
   (`SessionState.script`) ready for Phase 6, then sent to the chat; a
   `script_sent` flag makes it deliver exactly once. A failed *send* re-sends
   the **stored** script on the next update (no re-generation — the exact
   re-ask pattern from the Interviewer's `pending_question`); a failed
   *generation* recovers on the next update too.
3. **Constraints enforced mechanically.** The adapter validates the model's
   output before anything is stored or sent: non-empty, **≤ 90 words**, and a
   single paragraph (no blank-line paragraph breaks). Anything else is
   unusable model output → `ScriptError` (apology + retry), exactly like
   Phase 3's `MAX_QUESTION_LENGTH` guard. Prompting (persona, 60–90 word
   target, no markdown) steers; the word bound is law.
4. **Verification — component + conditional live smoke.** Same as Phase 4:
   in-process component tests prove portrait → script → message with fakes;
   a live Gemini smoke runs **only if a working key exists** in `.env`
   (currently the configured key is invalid — recorded honestly as "not
   reached" otherwise). The full "on your phone" Telegram sequence remains a
   documented manual step.

## Scope (in)

- **Session extension** (`session.py`): `SESSION_VERSION = 4`;
  `SessionState.script: str | None = None` (raw script for Phase 6),
  `script_sent: bool = False`; store methods `store_script(chat_id, script)`
  and `mark_script_sent(chat_id)`; `reset` clears both.
- **`ScripterLLM` port + `GeminiScripter` adapter** (`scripter.py`):
  - `write_script(dossier: Dossier) -> str` — one validated narration script.
  - Request body: persona **only** in top-level `systemInstruction` (the
    revered dramatic British narrator; frame quirks/bedtime/snacking as funny
    animal behaviours; exactly one paragraph, ≤ 90 words, no markdown, no
    titles or preamble, ready for voice-over); **one** `user` content citing
    the dossier (`suggested_animal`, `summary`, optional `animal_reason`);
    JSON `responseSchema` `{"script": str}` (the repo's schema-first pattern).
  - Post-validation (before any store/send): strip; non-empty;
    `word_count(script) <= MAX_SCRIPT_WORDS`; no blank-line paragraph break.
    Violations → `ScriptError`. `GeminiError` → redacted `ScriptError` raised
    `from None` (single-redaction guarantee preserved).
- **Scripting routing** (`scripter.py`): `handle_scripting(transport,
  chat_id, *, session, scripter, logger)`:
  - Already-stored script not yet sent → **re-send the stored text**
    (LLM-free), then `mark_script_sent` on success.
  - Otherwise: generate from the stored dossier → `store_script` → send →
    `mark_script_sent` (delayed commit: sent only after the user saw it).
  - Failures (generation or send) → apology, loud `script_failed`, no flag,
    retryable on the next update; missing dossier → fail loud +
    reset-and-reprompt; nothing raises into the polling loop.
  - The script is bounded by `MAX_MESSAGE_LENGTH`/`truncate_message` at the
    send chokepoint like every other outbound text.
- **Bouncer dispatch** (`bouncer.py`): after the Converter's same-update
  handoff succeeds, if the chat is `done && portrait_sent && !script_sent`,
  run the Scripter **in the same update**; any update arriving in that state
  re-drives scripting (retry/re-send); `done && script_sent` keeps the
  Interviewer's light reply.
- **Config** (`config.py`, `defaults.py`):
  `DEFAULT_SCRIPT_MODEL = "gemini-3.1-flash-lite"`, `MAX_SCRIPT_WORDS = 90`,
  `MIN_SCRIPT_WORDS = 60` (prompting target only — **not** enforced) in
  `defaults.py`; `get_script_model()` reading optional `GEMINI_SCRIPT_MODEL`
  (env-beats-file).
- **Wiring** (`polling.py`, `cli.py`): build one `GeminiScripter` and thread
  `scripter` through the poll loop and handler signatures.
- **Structured logging**: events `script_started`, `script_ready` (word
  count + suggested animal only), `script_sent`, `script_failed`,
  `script_skipped`. **The raw script text is never logged** (user-derived
  content that can echo the dossier's secrets — same rule as the dossier
  itself).

## Out of scope (YAGNI)

- The Narrator/TTS (Phase 6) — the script is *stored*; nothing renders audio
  yet.
- Resilience policy (Phase 7) — single-attempt graceful degradation in scope,
  backoff/retry policy is not.
- Script editing/regeneration commands, multiple script variants, or style
  knobs — one deterministic script per interview.
- Captions or formatting of the script message beyond the plain text.
- Enforcing a minimum word count (60 is a prompting target; only the 90-max
  is law — a hard floor would reject legitimately punchy narration).

## Contracts at boundaries (typed, untrusted input)

| Boundary | Type | Failure behaviour |
| --- | --- | --- |
| Gemini `write_script` output | `{"script": str}` via Pydantic | malformed/HTTP/network → `ScriptError` (redacted), apology, retry next update |
| Script validation | non-empty, ≤ `MAX_SCRIPT_WORDS`, no blank-line break | violation → `ScriptError` (unusable output), retry next update |
| Script send | `sendMessage`, bounded by `truncate_message` | failure → apology, stored script re-sent next update (LLM-free) |
| Session | `SessionState` v4 per `chat_id` (`script`, `script_sent`) | unknown chat → fresh default; wrong type → `ValidationError` |
| Dossier input | stored `Dossier` (v2 schema) | missing → fail loud + reset-and-reprompt |

`chat_id` stays `int` end-to-end. The script is **stored once and sent once**
(`script_sent` set only after a successful send); Phase 6 reads
`SessionState.script`.

## Non-negotiables

- **Per-`chat_id` isolation** — never leak one user's script/dossier to
  another.
- **Never log or commit secrets** — the single-redaction guarantees extend to
  a fourth adapter; the script (model-generated from user speech) is never
  written to logs either.
- **No test contacts Telegram or Gemini** — fakes only (the opt-in live smoke
  in `validation.md` is the sole exception, and it only runs with a working
  key).
- **The script is delivered exactly once** and is **ready for voice
  synthesis**: plain one-paragraph prose, no markdown, no titles.
- **Fail loud, degrade gracefully, never wedge** — a `done` chat missing its
  script is always retryable.

## Acceptance criteria (from ROADMAP Phase 5)

1. A narration script arrives in the chat **immediately after the portrait**,
   built from the dossier, reading as a dramatic British documentary
   voice-over.
2. The raw script string is **captured in the session state** (ready for
   audio synthesis) and delivered exactly once.
3. `scripts/test` and `scripts/hooks` are green, including all 483 pre-
   existing tests.

## Delivered (filled at verification)

Filled 2026-10-12 by the Phase 5 verifier.

- **`scripter.py` (new):** typed `ScripterLLM` port (`write_script(dossier) ->
  str`) + `GeminiScripter` Gemini REST text adapter over the shared
  `GeminiJsonClient` (persona only in `systemInstruction`; exactly one `user`
  content citing `suggested_animal`/`summary`/`animal_reason`; JSON
  `responseSchema` `{"script": str}`; default model `gemini-3.1-flash-lite`);
  mechanical output contract (non-empty, single paragraph, ≤ 90 words, blank-
  line/whitespace/CRLF-aware); `handle_scripting` routing — generate → store →
  send → flag, stored-script re-send LLM-free (re-ask pattern), apology +
  retry on failure, fail-loud + reset-and-reprompt on a missing dossier,
  truncate-bounded `sendMessage`. Logs `script_started`, `script_ready` (word
  count + suggested animal only), `script_sent`, `script_failed`,
  `script_skipped` and the `script_resend` debug marker; **the script text is
  never logged** and the API key stays redacted (plain + control-character
  canaries).
- **`session.py`:** `SESSION_VERSION = 4`; `SessionState.script: str | None`
  (stored for Phase 6) + `script_sent: bool = False`; `store_script` /
  `mark_script_sent` (flag moves only after a successful send); `reset` clears
  both.
- **`bouncer.py`:** Scripter threaded through dispatch; after the Converter's
  same-update handoff succeeds, the **same** update continues into scripting
  (fresh-completion and retried-portrait paths — scripter called exactly once);
  `done && portrait_sent && !script_sent` routes to the Scripter; `done &&
  script_sent` keeps the light reply; `/start` resets `script`/`script_sent`.
- **`polling.py` / `cli.py`:** `scripter` threaded through `poll_once` /
  `run_polling`; CLI builds one `GeminiScripter` (`ScripterFactory`) from the
  API key and model.
- **`config.py` / `defaults.py`:** `get_script_model()` (env-beats-file, blank
  → default); `DEFAULT_SCRIPT_MODEL = "gemini-3.1-flash-lite"`,
  `MAX_SCRIPT_WORDS = 90`, `MIN_SCRIPT_WORDS = 60` (prompting target, never
  enforced).
- **Tests:** `tests/unit/test_scripter.py` (55) + `tests/component/
  test_scripter_flow.py` (4) + extensions (bouncer/session/config/cli/polling).
  `scripts/test`: **560 passed** (483 pre-existing stay green). `scripts/hooks`
  green (`compileall` + smoke import + `ruff` + `mypy`: no issues). Full suite
  re-run with all four socket entry points patched to raise (throwaway plugin,
  sanity-checked): **560 passed — no test touches the network.** Live Gemini
  smoke: **not reached — key rejected by Google** (UUID-shaped, not `AIza…`).