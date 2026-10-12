# Validation — The Narrator (Phase 6)

How we know Phase 6 is correct and safe to merge. A step is "done" only when
the evidence is recorded and the user has approved any spec reconciliation.

## 1. Automated checks

- [ ] `scripts/test` — full suite green, including the new `narrator` suite
  and the component flow; **all 560 pre-existing tests** stay green except
  the intended dispatch changes (handler signatures gain `narrator`; the
  same-update handoff continues into narrating; `done && script_sent &&
  !narrator_sent` routes to the Narrator before the light reply).
- [ ] `scripts/hooks` — `compileall` + smoke import + `ruff` + `mypy` green.
- [ ] No test performs network I/O: the full suite is re-run with
  `socket.socket.connect`, `connect_ex`, `socket.create_connection` and
  `socket.getaddrinfo` patched to raise (throwaway plugin, sanity-checked to
  fire) — must stay green. `GeminiNarrator.urlopen` and
  `UrllibTransport.urlopen` stay injectable; the component flow uses the
  scripted `FakeNarrator`.

## 2. Behaviour checklist (maps to ROADMAP acceptance + locked decisions)

- [ ] The playable voice note (**OGG/Opus**) arrives **right after the script
  message, in the same update, with no user input**: … teaser → `sendPhoto`
  → script → `sendVoice`. *Evidence: component flow asserts the exact send
  sequence ends the one pass and `transport.send_voice` is called once with
  the model's `media_type`.*
- [ ] The raw audio payload is **captured in the session state**,
  `state.narration == synthesized bytes`, ready for any later processing, and
  `narrator_sent` is False *at* send time. *Evidence: session tests +
  component flow assert `state.narration` / `narrator_sent`.*
- [ ] The request targets `gemini-3.1-flash-tts-preview` by default, sends
  **one** `user` content whose parts are the voice guidance and the
  **verbatim script**, with `responseModalities: ["AUDIO"]` — no extra turns,
  no second agent. *Evidence: `test_narrator.py` request-body assertions
  (parts[0].text == voice guidance, parts[1].text == the exact script,
  responseModalities == ["AUDIO"], model == DEFAULT_NARRATOR_MODEL).*
- [ ] Voice guidance is the fixed posh-British deep-voice constant by default
  and overridable via `GEMINI_NARRATOR_VOICE` (env-beats-file). *Evidence:
  `test_config.py` override tests + `test_narrator.py` body test.*
- [ ] Payload validated mechanically before storage: `mime_type` starts with
  `audio/`, non-empty bytes, ≤ `MAX_NARRATION_BYTES`; violations →
  `NarratorError` (unusable output) + apology + retry. *Evidence:
  `test_narrator.py` validation tests.*
- [ ] Delivery is exactly-once: `narrator_sent` set only after a successful
  `sendVoice`; a failed send re-sends the **stored bytes** with **no
  re-synthesis**. *Evidence: narrating routing tests (stored-bytes re-send
  does not call the narrator) + component degraded variant.*
- [ ] Degraded synthesis (failure / invalid payload) → apology, loud
  `narrator_failed`, no flag, retry next update; missing script on a `done`
  chat → fail loud + reset-and-reprompt; nothing raises into the polling
  loop. *Evidence: `test_narrator.py` degradation tests + component
  variants.*
- [ ] After `narrator_sent`, non-command input gets the light "observation
  complete" reply; no re-synthesis, no state change. `/start` resets
  `narration`/`narrator_sent` with everything else. *Evidence: bouncer
  dispatch tests + session reset test.*

## 3. Contract & safety checks

- [ ] `chat_id` stays `int`; `SESSION_VERSION` bumped to 5; `narration` and
  `narrator_sent` are stored, typed state (never inferred).
- [ ] The audio crosses every boundary as typed data (`NarratorLLM` port,
  `AudioPayload`), never a raw payload.
- [ ] Single redaction implementations survive: the API key never appears in
  any raised `NarratorError`/`GeminiError` message or log line (plain +
  escaped/control-character forms); the bot token shares the same guarantee
  for `sendVoice`.
- [ ] The **audio bytes and the script are never written to logs**
  (`narrator_ready` carries only byte count + mime). `.env` remains
  gitignored/untracked.

## 4. Spec reconciliation (surfacing drift)

- [ ] Any deviation from `requirements.md` / `plan.md` recorded here with the
  user's approval (mirrors Phases 2–5).
- [ ] `SPECS/ROADMAP.md` Phase 6 marked complete (only after this checklist).
- [ ] `SPECS/TECH.md` updated: Narrator stage and `NarratorLLM` port, the final
  Bouncer-as-router handoff chain (dossier → portrait → script → voice note),
  session v5 (`narration` + `narrator_sent`). With user approval.
- [ ] `SPECS/MISSION.md` still matches (step 6 already describes the narrated
  voice note; no scope drift expected).
- [ ] `README.md` reflects Phase 6: status, narrator behaviour,
  `GEMINI_NARRATOR_MODEL` / `GEMINI_NARRATOR_VOICE` / `GEMINI_NARRATOR_TIMEOUT`,
  the OGG-`sendVoice` contract.

## 5. Manual smoke (live, opt-in — runs only with a working `GEMINI_API_KEY`)

- [ ] **Live Gemini smoke (if a working key exists in `.env`):** a witness
  script (not part of the suite, no committed credentials) runs
  `GeminiNarrator.synthesize("<the Scripter's sample narration>")` once with
  the real key and asserts a non-empty, `audio/*`-mime payload ≤
  `MAX_NARRATION_BYTES` comes back (and writes the note to a temp file for
  the user to play). Never prints or commits the key. *Result recorded here.*
  - If the configured key is still provider-blocked/invalid, record "not
    reached — key rejected by Google (HTTP 403/400)", which is not an adapter
    failure, and re-check when the user supplies a working key.
- [ ] **Live Telegram run (manual, user-side):** the full "send an image on
  your phone → bouncer check → interview → hybrid portrait → script → voiced
  documentary" sequence cannot be driven from this environment; documented as
  an open manual step for a consenting user with a working key.

## 6. Merge gate

- [ ] All of the above checked with evidence in the PR description.
- [ ] Branch `feature/2026-10-12-narrator` pushed; PR opened against `main`.
- [ ] **User** merges (the build agent does not merge).

## Deviations recorded at verification

_To be completed by the verifier._