# Plan — The Scripter (Phase 5)

Red/Green TDD: every task group writes its failing tests **first**, then the
code, then runs `scripts/test` until green. `scripts/hooks` runs at the end of
every group. Tests never touch Telegram or Gemini — the one exception is the
opt-in live smoke in `validation.md` (verifier only, and only with a working
key). **All 483 pre-existing tests must stay green throughout.**

New module: `scripter.py` (port + Gemini adapter + scripting routing).
Modified: `session.py`, `bouncer.py`, `config.py`, `defaults.py`,
`polling.py`, `cli.py` + their tests. No third-party HTTP dependency.

---

## 1. Session extension (`session.py`)

Red (`tests/unit/test_session.py`):
- Default state: `version == 4`, `script is None`, `script_sent is False`;
  `reset` clears both.
- `store_script(chat_id, script)` sets `script` (leaves `script_sent` as-is);
  `mark_script_sent(chat_id)` sets the flag without touching `script`, the
  photo, the interview log or the dossier.
- Wrong types (`script` non-str, `script_sent` non-bool) raise
  `ValidationError`; two chats never share a script.

Green:
- `SESSION_VERSION = 4`; `SessionState.script: str | None = None`,
  `script_sent: bool = False`; the two store methods.

Run `scripts/test` + `scripts/hooks`.

## 2. Scripter port + Gemini adapter (`scripter.py`)

Red (`tests/unit/test_scripter.py`, fake `urlopen`):
- Request body: persona ONLY in `systemInstruction` (revered dramatic British
  documentary narrator; quirks/bedtime/snacking framed as funny animal
  behaviours; exactly one paragraph; ≤ 90 words; no markdown, titles or
  preamble; ready for voice-over) — never in a turn; exactly ONE `user`
  content citing the dossier (`suggested_animal`, `summary`, `animal_reason`);
  `generationConfig` JSON `responseSchema` `{"script": str}`.
- `write_script` with a valid reply → returns the stripped script string.
- Post-validation: empty/whitespace → `ScriptError`; `> MAX_SCRIPT_WORDS`
  words → `ScriptError`; a blank-line paragraph break (`\n\n`) → `ScriptError`;
  a script exactly at 90 words passes.
- Every failure path (HTTP / network / protocol / malformed / schema-invalid
  / error payload) → redacted `ScriptError` raised `from None`; API key
  redaction sweep incl. the control-character key.

Green:
- `ScriptError(Exception)`; `ScripterLLM(Protocol)`:
  `write_script(dossier: Dossier) -> str`; `GeminiScripter(api_key, *,
  model=DEFAULT_SCRIPT_MODEL, base_url=…, timeout=…, urlopen=…)` wrapping
  `GeminiJsonClient.generate_json` with a `_Script(BaseModel)` schema.
- `word_count(text) -> int` helper (whitespace `split()`, mechanical and
  tested: emoji-without-spaces counts as one word, hyphenated compounds one).
- Persona + directive constants (the exact "60–90 words, no markdown" contract
  in the persona, per the activity).

Run `scripts/test` + `scripts/hooks`.

## 3. Scripting routing (`scripter.py`)

Red (`tests/unit/test_scripter.py` + `tests/conftest.py` — add `FakeScripter`
with scripted strings/exceptions, recording calls):
- `handle_scripting(transport, chat_id, *, session, scripter, logger)`:
  - no stored script → generate from the dossier → `store_script` → send →
    `mark_script_sent`; exact order asserted (generate → store → send →
    flag).
  - stored script, not yet sent → **re-send the stored text** (scripter NOT
    called), then `mark_script_sent` — the re-ask pattern.
  - `write_script` raises `ScriptError` → apology, no flag, `script_failed`
    event, returns `False`, nothing raises; unexpected errors caught the same
    way (KeyboardInterrupt/SystemExit exempt).
  - send failure → apology, no flag; next call re-sends the stored script
    (no re-generation).
  - missing dossier on a `done` chat → loud `script_failed` + reset-and-
    reprompt, never a crash.
  - over-long script text is bounded by `truncate_message` at the send
    chokepoint.

Green:
- `handle_scripting` + small helpers; the send uses the same bounded
  `sendMessage` pattern as `interviewer._send`; events `script_started`,
  `script_ready` (word count + suggested animal only), `script_sent`,
  `script_failed`, `script_skipped`; the script itself is never logged.

Run `scripts/test` + `scripts/hooks`.

## 4. Bouncer dispatch + wiring (bouncer, polling, config, CLI, conftest)

Red:
- `tests/unit/test_bouncer.py` (extend): after the Converter's same-update
  handoff runs, the **same** update continues into scripting when the chat is
  `done && portrait_sent && !script_sent`; an update arriving in that state
  re-drives scripting (stored-script re-send path); `done && script_sent`
  still keeps the Interviewer's light reply; `/start` resets everything
  including `script`/`script_sent`.
- `tests/unit/test_polling.py`: `scripter` threaded to the handler.
- `tests/unit/test_config.py`: `get_script_model()` env-`GEMINI_SCRIPT_MODEL`-
  beats-file, blank falls back to `gemini-3.1-flash-lite`.
- `tests/unit/test_cli.py`: scripter built from the API key and model,
  threaded into polling.
- `tests/conftest.py`: `FakeScripter` added.

Green:
- `bouncer.py`: accept `scripter`; extend the same-update handoff to run
  scripting after portrait delivery; route `done && portrait_sent &&
  !script_sent` to `scripter.handle_scripting`.
- `polling.py`/`cli.py`: thread `scripter`.
- `config.py`: `get_script_model()`; `defaults.py`: `DEFAULT_SCRIPT_MODEL`,
  `MAX_SCRIPT_WORDS = 90`, `MIN_SCRIPT_WORDS = 60` (prompting target only).

Run `scripts/test` + `scripts/hooks`.

## 5. Component flow + docs + verification

Red/Green:
- Extend `tests/component/test_converter_flow.py` (or a new
  `test_scripter_flow.py`) over the real `poll_once` with fakes: photo gated →
  Q1..Q5 → dossier → teaser → `sendPhoto` → **script message** in the same
  pass with **no user input**; the script is stored in the session
  (`state.script == raw script`); `script_sent` set; text after everything
  gets the light reply; a failed generation apologises and the next update
  completes; a failed send re-sends the **stored** script (FakeScripter not
  re-called). No network.
- Re-run the full suite with every socket entry point patched to raise (same
  throwaway-plugin technique as Phases 3–4) — must stay green and fire.

Docs (validated at verification):
- `README.md`: Phase 5 status, scripter behaviour, `GEMINI_SCRIPT_MODEL`, the
  60–90 word no-markdown contract, script stored for Phase 6.
- `SPECS/TECH.md`: Scripter stage (`ScripterLLM` port), Bouncer-as-router
  handoff chain (dossier → portrait → script in one update), session v4
  (`script`, `script_sent`). With user approval.
- `SPECS/ROADMAP.md`: mark Phase 5 complete when verified.
- Walk `validation.md`.

Finish: `scripts/test` + `scripts/hooks` green; commit; push; open PR to
`main`.

## Verification notes (filled at verification)

_To be completed by the verifier._