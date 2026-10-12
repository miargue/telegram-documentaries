# Validation — The Scripter (Phase 5)

How we know Phase 5 is correct and safe to merge. A step is "done" only when
the evidence is recorded and the user has approved any spec reconciliation.

Verifier: opencode (Phase 5 verifier). Date: **2026-10-12**. Branch:
`feature/2026-10-11-scripter` (working tree, uncommitted). Not merged.

## 1. Automated checks

- [x] `scripts/test` — full suite green, including the new `test_scripter`
  suite and the component flow; **all 483 pre-existing tests** stay green
  except the intended dispatch changes (handler signatures gain `scripter`;
  the same-update handoff continues into scripting; `done &&
  portrait_sent && !script_sent` routes to the Scripter before the light
  reply).
  **Evidence (2026-10-12):** `scripts/test` → `560 passed in 3.74s` (77 new:
  55 in `tests/unit/test_scripter.py` + 4 in `tests/component/test_scripter_flow.py`
  + extensions in `test_bouncer.py`, `test_session.py`, `test_config.py`,
  `test_cli.py`, `test_polling.py`, `test_converter_flow.py`,
  `test_interviewer_flow.py`, `test_full_gateway_flow.py`,
  `test_bouncer_flow.py`). Dispatcher signature changes are the only pre-
  existing-test edits (see `git diff --stat`, 16 files, 673 insertions).
- [x] `scripts/hooks` — `compileall` + smoke import + `ruff` + `mypy` green.
  **Evidence (2026-10-12):** `All checks passed!`; `mypy`: `Success: no issues
  found in 39 source files`.
- [x] No test performs network I/O: the full suite is re-run with
  `socket.socket.connect`, `connect_ex`, `socket.create_connection` and
  `socket.getaddrinfo` patched to raise (throwaway plugin, sanity-checked to
  fire) — must stay green. Every adapter takes an injectable `urlopen`; the
  scripter is the scripted `FakeScripter`.
  **Evidence (2026-10-12):** throwaway plugin `/tmp/opencode/netblock.py`
  (loader: `PYTHONPATH=/tmp/opencode python -m pytest -p netblock tests`).
  Sanity-check: canary `/tmp/opencode/test_netblock_canary.py` proves all four
  entry points raise under the plugin (`4 passed`); identity check confirmed
  `getaddrinfo`/`create_connection`/`connect`/`connect_ex` are the plugin's
  `_blocked` after `pytest_configure` and the originals before. Full suite with
  the plugin: **`560 passed in 3.46s`** — no test touched the network.
  Adapter seam: `GeminiScripter.urlopen` injectable at `scripter.py:136-152`;
  `FakeScripter` at `tests/conftest.py:378-406` (records calls, never I/O).

## 2. Behaviour checklist (maps to ROADMAP acceptance + locked decisions)

- [x] The narrative script arrives **immediately after the portrait, in the
  same update, with no user input**: portrait `sendPhoto` → script message.
  *Evidence: component flow asserts the exact send sequence (…, teaser,
  sendPhoto, script) ends the one pass.*
  **Evidence:** `tests/component/test_scripter_flow.py:113`
  (`test_full_pass_delivers_the_narration_right_after_the_portrait`) asserts the
  one-pass send sequence `teaser` (texts[7]) → `sendPhoto` (photos == 1) →
  **script message** (texts[8], `scripter.calls == 1`, test_scripter_flow.py:151-165);
  the follow-up turn is not even polled until after (texts[9]). Fresh-completion
  handoff also proven at `tests/unit/test_bouncer.py:1136`
  (`test_final_answer_hands_off_to_the_converter_in_the_same_update`, asserts
  `texts[-1] == script` with the dossier + portrait, lines 1189-1193).
- [x] The raw script string is **captured in the session state**,
  `state.script == generated text`, ready for Phase 6 audio synthesis.
  *Evidence: session tests + component flow assert `state.script` and
  `script_sent`.*
  **Evidence:** `tests/unit/test_scripter.py:586-609`
  (`test_handle_scripting_generates_and_delivers_the_script`, asserts
  `state.script == SCRIPT`, `script_sent is True`); `tests/unit/test_session.py:377-447`
  (`test_store_script_sets_the_script_and_leaves_the_flag_alone`,
  `test_mark_script_sent_sets_the_flag_without_touching_the_script`,
  `test_reset_clears_the_script_and_the_script_sent_flag`, typed-field +
  per-chat isolation tests); component `test_scripter_flow.py:162-165`
  (`state.script`/`script_sent`, `len(scripter.calls) == 1`,
  `scripter.calls[0] == dossier`). Store-before-send at `scripter.py:404-414`,
  `session.py:198-220`.
- [x] The Gemini request enforces the persona: *revered, dramatic British
  wildlife documentary narrator observing a wild beast*; quirks, bedtime
  habits and snacking patterns framed as funny animal behaviours; persona
  ONLY in `systemInstruction`; exactly ONE `user` content citing the dossier
  (`suggested_animal`, `summary`, `animal_reason`); JSON `responseSchema`
  `{"script": str}`; model `gemini-3.1-flash-lite`.
  *Evidence: `test_scripter.py` request-body assertions (persona in
  systemInstruction, one content, schema, dossier text).*
  **Evidence:** `tests/unit/test_scripter.py:132-149`
  (`test_write_script_posts_a_single_user_turn_citing_the_dossier` — exactly 1
  content, role `user`, all three dossier fields present, persona excluded from
  the turn); `:152-164` (`test_write_script_keeps_the_persona_out_of_the_turns` —
  `system == SCRIPT_PERSONA`); `:166-181` (`test_write_script_persona_is_the_revered_british_narrator` —
  "british", "documentary narrator", "one paragraph", "90 words", "no markdown",
  "voice", "revered"); `:183-197` (`test_write_script_asks_for_json_with_the_script_schema` —
  `responseSchema == SCRIPT_SCHEMA == {"type":"object","properties":{"script":{"type":"string"}},"required":["script"]}`);
  `:199-208` (animal_reason omitted when absent); `:226-237`
  (`test_write_script_uses_the_default_script_model` —
  `defaults.DEFAULT_SCRIPT_MODEL == "gemini-3.1-flash-lite"`). Constants:
  `SCRIPT_PERSONA` `scripter.py:75-84`, `SCRIPT_DIRECTIVE` `scripter.py:89-95`,
  `SCRIPT_SCHEMA` `scripter.py:97-105`, `DEFAULT_SCRIPT_MODEL`
  `defaults.py:21`, model default wired in `scripter.py:140` and config
  `GEMINI_SCRIPT_MODEL` `config.py:193-210`.
- [x] Output constraint enforced mechanically: non-empty; ≤ `MAX_SCRIPT_WORDS`
  (90); single paragraph (no `\n\n`); a script exactly at the bound passes.
  *Evidence: `test_scripter.py` validation tests (empty / 91 words / `\n\n` /
  90-word pass).*
  **Evidence:** `tests/unit/test_scripter.py:273-278` (empty), `:280-284`
  (whitespace-only), `:287-292` (91 words), `:295-298` (exactly 90 passes),
  `:301-308` (`\n\n` break), `:317-326` (whitespace-only blank line),
  `:328-336` (CRLF blank line), `:310-314` / `:338-344` (soft single line break
  accepted). Regex `_BLANK_LINE = r"\n[ \t\r]*\n"` at `scripter.py:110`;
  enforcement `_validate` at `scripter.py:203-217`; `MAX_SCRIPT_WORDS = 90`
  `defaults.py:29`; `MIN_SCRIPT_WORDS = 60` prompting-only `defaults.py:28`
  (never enforced — locked decision 3).
- [x] The script is delivered exactly once: `script_sent` set only after a
  successful send; a failed send re-sends the **stored** script with **no**
  re-generation (the Interviewer's re-ask pattern).
  *Evidence: routing tests (stored-script re-send does not call the scripter)
  + component degraded variant.*
  **Evidence:** `tests/unit/test_scripter.py:612-636`
  (`test_handle_scripting_flag_is_evidence_not_intent` — flag observed `False`
  at the moment of the send, `True` after), `:688-711`
  (`test_handle_scripting_stores_the_script_even_when_the_send_fails`),
  `:714-737` (`test_handle_scripting_resends_the_stored_script_without_retrying` —
  scripter scripted to raise on any call, `scripter.calls == []`),
  `:766-787` (resend that fails stays retryable);
  `tests/unit/test_bouncer.py:1283-1307`
  (`test_done_with_a_stored_script_resends_it_without_calling_the_scripter`);
  component `tests/component/test_scripter_flow.py:234-282`
  (`test_failed_send_reuses_the_stored_script_without_regenerating` —
  `state.script_sent is True` and `len(scripter.calls) == 1` after the retry).
  Routing `handle_scripting` re-send branch `scripter.py:335-363`.
- [x] Degraded generation (failure / unusable output) → apology, loud
  `script_failed`, no flag, retry on the next update; missing dossier →
  fail loud + reset-and-reprompt; nothing raises into the polling loop.
  *Evidence: `test_scripter.py` degradation tests + component variants.*
  **Evidence:** `tests/unit/test_scripter.py:789-811`
  (`test_handle_scripting_apologises_and_stays_retryable_on_generation_failure`),
  `:813-824` (`test_handle_scripting_never_raises_on_an_unexpected_generation_error`),
  `:826-834` (only KeyboardInterrupt escapes), `:889-909`
  (`test_handle_scripting_resets_and_reprompts_when_the_dossier_is_missing` —
  loud `script_failed` + reset to `awaiting_photo` + `SCRIPT_RESET_REPLY`);
  component `tests/component/test_scripter_flow.py:168-231`
  (`test_degraded_generation_apologises_and_retries_on_the_next_update` —
  `script_failed` event, apology in-band, `script_sent` False, next update
  completes, `scripter.calls == 2`); bouncer `tests/unit/test_bouncer.py:1310-1367`
  (`test_handoff_scripting_failure_leaves_the_chat_retryable`).
  Implementation: `scripter.py:382-391` (generation catch), `:365-376`
  (missing dossier), `_send` swallows with loud `reply_failed` `scripter.py:243-269`.
- [x] The script message is plain prose — no markdown, titles or preamble —
  and is bounded by `truncate_message` at the send chokepoint.
  *Evidence: persona + validation tests; send-path tests.*
  **Evidence:** persona contract tests `test_scripter.py:166-181`; stored
  script is the raw validated string `:240-247`; send bounded at the chokepoint
  `scripter.py:254` (`truncate_message(text)`); regression guard
  `tests/unit/test_scripter.py:912-935`
  (`test_handle_scripting_truncates_an_oversized_script_before_sending` —
  delivered `message_length <= MAX_MESSAGE_LENGTH`, stored script stays raw).
- [x] After `script_sent`, non-command input gets the light "observation
  complete" reply; no re-scripting, no state change. `/start` resets
  `script`/`script_sent` with everything else.
  *Evidence: bouncer dispatch tests + session reset test.*
  **Evidence:** `tests/unit/test_bouncer.py:278-307`
  (`test_text_after_completion_is_delegated_to_the_interviewer`, fully complete
  chat → `OBSERVATION_COMPLETE_REPLY`, no photos, gate untouched);
  `:459-498` (album after completion → exactly one light reply); the router
  falls through to the Interviewer only when `done && portrait_sent &&
  script_sent` (`bouncer.py:195-199`); light reply is a pure send with no
  mutation (`interviewer.py:523-524`). `/start` reset:
  `tests/unit/test_bouncer.py:1370-1389`
  (`test_start_clears_the_script_and_script_sent_flag`), component
  `tests/component/test_scripter_flow.py:285-322`
  (`test_start_after_the_narration_resets_the_script_state`), reset clears both
  `tests/unit/test_session.py:406-416`.

## 3. Contract & safety checks

- [x] `chat_id` stays `int`; `SESSION_VERSION` bumped to 4; `script` and
  `script_sent` are stored, typed state (never inferred).
  **Evidence:** `models.py:25` (`Chat.id: int`), `session.py:100`
  (`dict[int, SessionState]`), `scripter.py:244` (`chat_id: int`); `SESSION_VERSION = 4`
  `session.py:33`; `SessionState.script: str | None` / `script_sent: bool`
  `session.py:79-83`; typed-field rejection tests `tests/unit/test_session.py:423-436`
  (non-str `script`, non-bool `script_sent` → `ValidationError`); version-assert
  tests `tests/unit/test_session.py:27` and `:100-110`.
- [x] The script crosses every boundary as typed data (`ScripterLLM` port,
  Pydantic `{"script": str}`), never a raw payload.
  **Evidence:** typed port `ScripterLLM.write_script(dossier) -> str`
  `scripter.py:63-68`; `_Script(BaseModel)` `scripter.py:113-114`; candidates
  schema-validated by `GeminiJsonClient.generate_json` `gemini.py:208-225`;
  routing accepts only a `Dossier` `scripter.py:383`; session stores validated
  `str` `session.py:198-208`.
- [x] Single redaction implementations survive: the API key never appears in
  any raised `ScriptError`/`GeminiError` message or log line (plain +
  escaped/control-character forms).
  **Evidence:** shared `_redact` (plain + unicode-escaped control-char form)
  `gemini.py:337-346`; scripter canaries `tests/unit/test_scripter.py:462-479`
  (`test_every_script_failure_redacts_the_api_key` — five key-bearing failure
  branches, checks `str(exc)`, formatted traceback, `JsonFormatter` line,
  `__cause__ is None`) and `:482-500` (`test_scripter_redacts_a_control_character_key` —
  `SECRET-SCRIPTER-KEY-\nCANARY`); shared-client control-char canary
  `tests/unit/test_gemini.py:366-387`; every scripter failure normalised with
  `from None` / `__suppress_context__` `tests/unit/test_scripter.py:382-410`,
  `scripter.py:163-169`.
- [x] The **script text is never written to logs** (user-derived content that
  can echo the dossier's secrets); log events carry only ids, word count and
  the suggested animal. `.env` remains gitignored/untracked.
  **Evidence:** `test_handle_scripting_never_logs_the_script_or_dossier_text`
  `tests/unit/test_scripter.py:639-661`; `script_ready` carries only
  `word_count` + `suggested_animal` `test_scripter.py:663-685`; `script_resend`
  debug marker carries only `word_count` `test_scripter.py:740-763`;
  production lines `scripter.py:339-346` (`script_resend`), `:393-402`
  (`script_ready`), `:415-422` (`script_sent`) — no script text; `.env` in
  `.gitignore:7` and `git check-ignore .env` → ignored; `git status` shows
  `.env` nowhere (untracked).

## 4. Spec reconciliation (surfacing drift)

- [x] Any deviation from `requirements.md` / `plan.md` recorded here with the
  user's approval (mirrors Phases 2–4).
  **No blocking deviation.** One additive, non-spec item recorded: the
  `script_resend` debug event (word count only) for the stored-script re-send
  path (`scripter.py:339-347`, test `test_scripter.py:740-763`) — it implements
  the locked "re-send stored script LLM-free" decision and adds no surface,
  matches the checklist's "script_resend debug marker" wording, and stays
  within the approved typed-port + store-once + component-test approach
  (requirements.md decisions 1, 2, 4; user-approved 2026-10-11).
- [x] `SPECS/ROADMAP.md` Phase 5 marked complete (only after this checklist).
  **Done (2026-10-12):** Phase 5 marked complete with date, branch, delivered
  summary, test counts (560), hooks result, network-isolation evidence and live
  smoke status. See `SPECS/ROADMAP.md` §5.
- [x] `SPECS/TECH.md` updated: Scripter stage and `ScripterLLM` port, the
  Bouncer-as-router handoff chain ending in scripting, session v4
  (`script` + `script_sent`). With user approval.
  **Done (2026-10-12):** Framework + architecture bullets now cover the
  `ScripterLLM` port and the dossier → portrait → script same-update chain;
  Session state section gains Version 4. User approval basis: requirements
  decision 1 (typed port) and decision 2 (store-once/same-update), approved
  2026-10-11.
- [x] `SPECS/MISSION.md` still matches (step 5 already describes the
  narration; no scope drift expected).
  **Confirmed (2026-10-12):** mission step 5 — "The Scripter writes a 60–90
  word dramatic documentary narration" — matches exactly what Phase 5 ships
  (`MIN_SCRIPT_WORDS = 60` prompt target, `MAX_SCRIPT_WORDS = 90` hard bound).
  No edit needed; no drift.
- [x] `README.md` reflects Phase 5: status, scripter behaviour,
  `GEMINI_SCRIPT_MODEL`, the no-markdown ≤90-word contract, storage for
  Phase 6.
  **Done (2026-10-12):** status line → Phase 5; "What the bot does" section
  gains the Scripter bullet (same-update narration, persona via system
  instruction, mechanical ≤90-word / single-paragraph / no-markdown contract,
  `SessionState.script` stored for Phase 6, `script_sent` after successful
  send, re-ask on failed send, light reply after both); Configure lists
  `GEMINI_SCRIPT_MODEL`; logs section adds `script_started`/`script_ready`/
  `script_sent`/`script_failed`/`script_skipped`/`script_resend` + "script text
  never logged" + all-four-adapters redaction; Test + layout + Roadmap updated.

## 5. Manual smoke (live, opt-in — runs only with a working `GEMINI_API_KEY`)

- [x] **Live Gemini smoke (if a working key exists in `.env`):** a witness
  script (not part of the suite, no committed credentials) runs
  `GeminiScripter.write_script(Dossier(summary=…, suggested_animal="house
  cat", animal_reason=…))` once with the real key and asserts a non-empty,
  ≤90-word, single-paragraph script comes back. Never prints or commits the
  key. *Result recorded here.*
  **Result: not reached — key rejected by Google.** `.env` (`GEMINI_API_KEY`)
  holds `406477b9-3368-4bd3-9add-7c795e258726`, a **UUID-shaped** value, not an
  `AIza…` Google AI Studio key (validator: UUID shape, no `AIza` prefix
  anywhere). Per the approved classification this is provider-side/key-schema
  blocking, **not** an adapter failure; no real narration was observed and no
  attempt was made to patch or replace the key. Re-check when the user supplies
  a working `AIza…` key.
  - If the configured key is still provider-blocked/invalid, record "not
    reached — key rejected by Google (HTTP 403/400)", which is not an adapter
    failure, and re-check when the user supplies a working key.
    **Recorded as above (2026-10-12).**
- [x] **Live Telegram run (manual, user-side):** the full "send an image on
  your phone → interview → portrait → narration" sequence cannot be driven
  from this environment; documented as an open manual step for a consenting
  user with a working key.
  **Recorded — open manual step (2026-10-12).**

## 6. Merge gate

- [ ] All of the above checked with evidence in the PR description.
  **Recorded: full evidence set is in this file for the PR description.**
  (Checkbox left for the build agent to tick when the PR is actually opened.)
- [ ] Branch `feature/2026-10-11-scripter` pushed; PR opened against `main`.
- [ ] **User** merges (the build agent does not merge).

## Deviations recorded at verification

- **Additive (non-blocking):** the stored-script re-send path emits a
  `script_resend` **debug** log event carrying only `chat_id` and `word_count`
  (`scripter.py:339-347`; test `tests/unit/test_scripter.py:740-763`). The
  plan's event list named `script_started`/`script_ready`/`script_sent`/
  `script_failed`/`script_skipped`; `script_resend` implements the locked
  re-ask decision verbatim, carries no user text, and was not part of the
  original plan list. No spec text contradicts it.
- **Live smoke:** not reached (invalid UUID-shaped key). Expected by the plan
  and requirements ("currently the configured key is invalid — recorded
  honestly as 'not reached' otherwise").
- No other deviations. Nothing blocking. Final status: `scripts/test` 560
  passed, `scripts/hooks` all checks passed (2026-10-12).