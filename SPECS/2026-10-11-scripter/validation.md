# Validation — The Scripter (Phase 5)

How we know Phase 5 is correct and safe to merge. A step is "done" only when
the evidence is recorded and the user has approved any spec reconciliation.

## 1. Automated checks

- [ ] `scripts/test` — full suite green, including the new `test_scripter`
  suite and the component flow; **all 483 pre-existing tests** stay green
  except the intended dispatch changes (handler signatures gain `scripter`;
  the same-update handoff continues into scripting; `done &&
  portrait_sent && !script_sent` routes to the Scripter before the light
  reply).
- [ ] `scripts/hooks` — `compileall` + smoke import + `ruff` + `mypy` green.
- [ ] No test performs network I/O: the full suite is re-run with
  `socket.socket.connect`, `connect_ex`, `socket.create_connection` and
  `socket.getaddrinfo` patched to raise (throwaway plugin, sanity-checked to
  fire) — must stay green. Every adapter takes an injectable `urlopen`; the
  scripter is the scripted `FakeScripter`.

## 2. Behaviour checklist (maps to ROADMAP acceptance + locked decisions)

- [ ] The narrative script arrives **immediately after the portrait, in the
  same update, with no user input**: portrait `sendPhoto` → script message.
  *Evidence: component flow asserts the exact send sequence (…, teaser,
  sendPhoto, script) ends the one pass.*
- [ ] The raw script string is **captured in the session state**,
  `state.script == generated text`, ready for Phase 6 audio synthesis.
  *Evidence: session tests + component flow assert `state.script` and
  `script_sent`.*
- [ ] The Gemini request enforces the persona: *revered, dramatic British
  wildlife documentary narrator observing a wild beast*; quirks, bedtime
  habits and snacking patterns framed as funny animal behaviours; persona
  ONLY in `systemInstruction`; exactly ONE `user` content citing the dossier
  (`suggested_animal`, `summary`, `animal_reason`); JSON `responseSchema`
  `{"script": str}`; model `gemini-3.1-flash-lite`.
  *Evidence: `test_scripter.py` request-body assertions (persona in
  systemInstruction, one content, schema, dossier text).*
- [ ] Output constraint enforced mechanically: non-empty; ≤ `MAX_SCRIPT_WORDS`
  (90); single paragraph (no `\n\n`); a script exactly at the bound passes.
  *Evidence: `test_scripter.py` validation tests (empty / 91 words / `\n\n` /
  90-word pass).*
- [ ] The script is delivered exactly once: `script_sent` set only after a
  successful send; a failed send re-sends the **stored** script with **no**
  re-generation (the Interviewer's re-ask pattern).
  *Evidence: routing tests (stored-script re-send does not call the scripter)
  + component degraded variant.*
- [ ] Degraded generation (failure / unusable output) → apology, loud
  `script_failed`, no flag, retry on the next update; missing dossier →
  fail loud + reset-and-reprompt; nothing raises into the polling loop.
  *Evidence: `test_scripter.py` degradation tests + component variants.*
- [ ] The script message is plain prose — no markdown, titles or preamble —
  and is bounded by `truncate_message` at the send chokepoint.
  *Evidence: persona + validation tests; send-path tests.*
- [ ] After `script_sent`, non-command input gets the light "observation
  complete" reply; no re-scripting, no state change. `/start` resets
  `script`/`script_sent` with everything else.
  *Evidence: bouncer dispatch tests + session reset test.*

## 3. Contract & safety checks

- [ ] `chat_id` stays `int`; `SESSION_VERSION` bumped to 4; `script` and
  `script_sent` are stored, typed state (never inferred).
- [ ] The script crosses every boundary as typed data (`ScripterLLM` port,
  Pydantic `{"script": str}`), never a raw payload.
- [ ] Single redaction implementations survive: the API key never appears in
  any raised `ScriptError`/`GeminiError` message or log line (plain +
  escaped/control-character forms).
- [ ] The **script text is never written to logs** (user-derived content that
  can echo the dossier's secrets); log events carry only ids, word count and
  the suggested animal. `.env` remains gitignored/untracked.

## 4. Spec reconciliation (surfacing drift)

- [ ] Any deviation from `requirements.md` / `plan.md` recorded here with the
  user's approval (mirrors Phases 2–4).
- [ ] `SPECS/ROADMAP.md` Phase 5 marked complete (only after this checklist).
- [ ] `SPECS/TECH.md` updated: Scripter stage and `ScripterLLM` port, the
  Bouncer-as-router handoff chain ending in scripting, session v4
  (`script` + `script_sent`). With user approval.
- [ ] `SPECS/MISSION.md` still matches (step 5 already describes the
  narration; no scope drift expected).
- [ ] `README.md` reflects Phase 5: status, scripter behaviour,
  `GEMINI_SCRIPT_MODEL`, the no-markdown ≤90-word contract, storage for
  Phase 6.

## 5. Manual smoke (live, opt-in — runs only with a working `GEMINI_API_KEY`)

- [ ] **Live Gemini smoke (if a working key exists in `.env`):** a witness
  script (not part of the suite, no committed credentials) runs
  `GeminiScripter.write_script(Dossier(summary=…, suggested_animal="house
  cat", animal_reason=…))` once with the real key and asserts a non-empty,
  ≤90-word, single-paragraph script comes back. Never prints or commits the
  key. *Result recorded here.*
  - If the configured key is still provider-blocked/invalid, record "not
    reached — key rejected by Google (HTTP 403/400)", which is not an adapter
    failure, and re-check when the user supplies a working key.
- [ ] **Live Telegram run (manual, user-side):** the full "send an image on
  your phone → interview → portrait → narration" sequence cannot be driven
  from this environment; documented as an open manual step for a consenting
  user with a working key.

## 6. Merge gate

- [ ] All of the above checked with evidence in the PR description.
- [ ] Branch `feature/2026-10-11-scripter` pushed; PR opened against `main`.
- [ ] **User** merges (the build agent does not merge).

## Deviations recorded at verification

_To be completed by the verifier._