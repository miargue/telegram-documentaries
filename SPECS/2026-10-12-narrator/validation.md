# Validation — The Narrator (Phase 6)

How we know Phase 6 is correct and safe to merge. A step is "done" only when
the evidence is recorded and the user has approved any spec reconciliation.

Verifier: opencode (Phase 6 verifier). Date: **2026-10-14**. Branch:
`feature/2026-10-12-narrator` (working tree, uncommitted). Not merged.

## 1. Automated checks

- [x] `scripts/test` — full suite green, including the new `narrator` suite
  and the component flow; **all 560 pre-existing tests** stay green except
  the intended dispatch changes (handler signatures gain `narrator`; the
  same-update handoff continues into narrating; `done && script_sent &&
  !narrator_sent` routes to the Narrator before the light reply).
  **Evidence (2026-10-14):** `scripts/test` → `691 passed in 4.81s` (131 new:
  54 in `tests/unit/test_narrator.py` + 4 in
  `tests/component/test_narrator_flow.py` + extensions in `test_gemini.py`
  (307 lines), `test_transport.py`, `test_session.py`, `test_bouncer.py`,
  `test_config.py`, `test_cli.py`, `test_polling.py`, `conftest.py`). No
  pre-existing test was deleted; dispatcher signature changes are the only
  pre-existing-test edits (`git diff --stat main`: 19 files, 1887 insertions,
  40 deletions).
- [x] `scripts/hooks` — `compileall` + smoke import + `ruff` + `mypy` green.
  **Evidence (2026-10-14):** `[hooks] all checks passed`; `ruff`: `All checks
  passed!`; `mypy`: `Success: no issues found in 42 source files`.
- [x] No test performs network I/O: the full suite is re-run with
  `socket.socket.connect`, `connect_ex`, `socket.create_connection` and
  `socket.getaddrinfo` patched to raise (throwaway plugin, sanity-checked to
  fire) — must stay green. `GeminiNarrator.urlopen` and
  `UrllibTransport.urlopen` stay injectable; the component flow uses the
  scripted `FakeNarrator`.
  **Evidence (2026-10-14):** throwaway plugin
  `/tmp/opencode/socket_blocker.py` (loader:
  `PYTHONPATH=/tmp/opencode ./scripts/test -p socket_blocker`). The plugin's
  own `pytest_configure` self-checks all four entry points fire (asserts 3
  successful refusals), and a canary `/tmp/opencode/test_socket_blocker_sanity.py`
  proves `create_connection` / `getaddrinfo` / raw `connect` all raise
  (`3 passed`). Full suite with the plugin: **`691 passed in 3.21s`** — no
  test touched the network. Adapter seam `GeminiNarrator(urlopen=…)`
  `narrator.py:75` → shared `GeminiJsonClient` `gemini.py:155`;
  `UrllibTransport.send_voice`/`urlopen` `transport.py:270`; `FakeNarrator`
  `tests/conftest.py:437-465` (records calls, never I/O).

## 2. Behaviour checklist (maps to ROADMAP acceptance + locked decisions)

- [x] The playable voice note (**OGG/Opus**) arrives **right after the script
  message, in the same update, with no user input**: … teaser → `sendPhoto`
  → script → `sendVoice`. *Evidence: component flow asserts the exact send
  sequence ends the one pass and `transport.send_voice` is called once with
  the model's `media_type`.*
  **Evidence:** `tests/component/test_narrator_flow.py:124`
  (`test_full_pass_delivers_the_voice_note_right_after_the_script`) drives the
  real `poll_once` loop: `texts[0]==SUCCESS_REPLY`, `texts[1:6]==Q1..Q5`,
  dossier (`"House cat" in texts[6]`), `texts[7]==TEASER_REPLY`, `photos==1`,
  `texts[8]==SCRIPT`, all in one pass (lines 163-170); **exactly one** voice
  note with the synthesised audio (lines 174-176); the follow-up turn only
  earns the light reply (`texts[9]==OBSERVATION_COMPLETE_REPLY`). Unit proof:
  `tests/unit/test_narrator.py:522`
  (`test_handle_narrating_synthesises_and_sends_the_voice_note`, asserts
  `len(transport.voices)==1`, `media_type=="audio/ogg"`,
  `filename=="narration.ogg"`); bouncer router proof
  `tests/unit/test_bouncer.py:1493`
  (`test_same_update_chain_runs_the_narrator_after_the_script`,
  `narrator.calls==[NARRATION_SCRIPT]`, `len(transport.voices)==1`). Note: the
  send uses the **normalised** locked `audio/ogg`, not the model's decorated
  `audio/ogg; codecs=opus` — see deviations.
- [x] The raw audio payload is **captured in the session state**,
  `state.narration == synthesized bytes`, ready for any later processing, and
  `narrator_sent` is False *at* send time. *Evidence: session tests +
  component flow assert `state.narration` / `narrator_sent`.*
  **Evidence:** `tests/unit/test_narrator.py:522` asserts
  `state.narration == AUDIO` and `state.narrator_sent is True` after the send
  (lines 541-543); `tests/unit/test_narrator.py:572`
  (`test_handle_narrating_flag_is_evidence_not_intent`) records the flag at the
  moment of `send_voice` and asserts `flag_at_send == [False]` (line 598).
  Session: `tests/unit/test_session.py:454`
  (`test_store_narration_sets_the_audio_and_leaves_the_flag_alone`), `:469`
  (`test_mark_narrator_sent_sets_the_flag_without_touching_the_audio`), `:528`
  (per-chat isolation). Component: `test_narrator_flow.py:177-178`
  (`state.narration == narrator.default.data`, `narrator_sent is True`).
  Store-before-send `narrator.py:291` then send `:294`.
- [x] The request targets `gemini-3.1-flash-tts-preview` by default, sends
  **one** `user` content whose parts are the voice guidance and the
  **verbatim script**, with `responseModalities: ["AUDIO"]` — no extra turns,
  no second agent. *Evidence: `test_narrator.py` request-body assertions
  (parts[0].text == voice guidance, parts[1].text == the exact script,
  responseModalities == ["AUDIO"], model == DEFAULT_NARRATOR_MODEL).*
  **Evidence:** `tests/unit/test_narrator.py:142`
  (`test_synthesize_posts_a_single_user_turn_with_two_text_parts` — exactly 1
  content, role `user`, 2 parts, `parts[0]["text"]==DEFAULT_NARRATOR_VOICE`,
  `parts[1]["text"]==SCRIPT`); `:170`
  (`test_synthesize_asks_for_audio_modalities_only` —
  `generationConfig == {"responseModalities": ["AUDIO"]}`);
  `:194` (`test_synthesize_uses_the_default_narrator_model` —
  `DEFAULT_NARRATOR_MODEL == "gemini-3.1-flash-tts-preview"` and the endpoint
  carries it); `:206` (`test_synthesize_returns_the_decoded_audio_payload`);
  `:218` (first audio part taken, text parts skipped). Body built at
  `narrator.py:95-107`.
- [x] Voice guidance is the fixed posh-British deep-voice constant by default
  and overridable via `GEMINI_NARRATOR_VOICE` (env-beats-file). *Evidence:
  `test_config.py` override tests + `test_narrator.py` body test.*
  **Evidence:** `tests/unit/test_config.py:463`
  (`test_get_narrator_voice_defaults_to_the_documentary_persona` — asserts
  `DEFAULT_NARRATOR_VOICE` contains "British" and "male"), `:474`
  (env override wins over file), `:485` (file fallback), `:492` (blank →
  default); `tests/unit/test_narrator.py:158`
  (`test_synthesize_uses_the_configured_voice`). Constants
  `DEFAULT_NARRATOR_VOICE` `defaults.py:28`; `GEMINI_NARRATOR_VOICE` getter
  `config.py:275`; wired into the adapter `narrator.py:71-77`.
- [x] Payload validated mechanically before storage: `mime_type` starts with
  `audio/`, non-empty bytes, ≤ `MAX_NARRATION_BYTES`; violations →
  `NarratorError` (unusable output) + apology + retry. *Evidence:
  `test_narrator.py` validation tests.*
  **Evidence:** `tests/unit/test_narrator.py:253` (non-audio mime via
  `synthesize`), `:264` (`test_validate_audio_rejects_a_non_ogg_payload` —
  `audio/wav`, `audio/mpeg`, `text/plain`, `""`), `:274`
  (`test_validate_audio_accepts_the_locked_ogg_format` — `audio/ogg`,
  `audio/ogg; codecs=opus`, `AUDIO/OGG`), `:283` (oversized), `:291` (exactly
  at the limit passes), `:298` (empty), `:305` (non-base64), `:323` (no audio
  part). Contract enforced `validate_audio` `narrator.py:110-129`;
  `MAX_NARRATION_BYTES = 5 * 1024 * 1024` `defaults.py:56`. **Note:** the
  implementation requires the OGG subtype (`audio/ogg`), stricter than the
  plan's `audio/*` — recorded as a deviation.
- [x] Delivery is exactly-once: `narrator_sent` set only after a successful
  `sendVoice`; a failed send re-sends the **stored bytes** with **no
  re-synthesis**. *Evidence: narrating routing tests (stored-bytes re-send
  does not call the narrator) + component degraded variant.*
  **Evidence:** `tests/unit/test_narrator.py:654`
  (`test_handle_narrating_stores_the_audio_even_when_the_send_fails` —
  `state.narration == AUDIO`, flag False, apology, `narrator_failed`);
  `:678` (`test_handle_narrating_resends_the_stored_audio_without_resynthesising` —
  narrator scripted to raise, `narrator.calls == []`, one voice with the
  stored bytes, flag set); `:706` (resend uses clean `audio/ogg`); `:742`
  (resend that fails also apologises). Component
  `tests/component/test_narrator_flow.py:235`
  (`test_failed_send_reuses_the_stored_voice_without_resynthesising` —
  `len(narrator.calls) == 1` after the retry, `voices[1]["audio"] ==
  narrator.default.data`). Bouncer
  `tests/unit/test_bouncer.py:1469`
  (`test_done_with_a_stored_voice_note_resends_it_without_resynthesising`).
  Store-once/send-once at `narrator.py:223-250` and `:291-309`.
- [x] Degraded synthesis (failure / invalid payload) → apology, loud
  `narrator_failed`, no flag, retry next update; missing script on a `done`
  chat → fail loud + reset-and-reprompt; nothing raises into the polling
  loop. *Evidence: `test_narrator.py` degradation tests + component
  variants.*
  **Evidence:** `tests/unit/test_narrator.py:766`
  (`test_handle_narrating_apologises_and_stays_retryable_on_synthesis_failure`),
  `:790` (invalid payload → apology, no flag), `:803` (unexpected error never
  raises), `:816` / `:826` (only `KeyboardInterrupt`/`SystemExit` escape),
  `:838` / `:856` / `:873` (skip guards), `:894`
  (`test_handle_narrating_resets_and_reprompts_when_the_script_is_missing` —
  loud `narrator_failed`, reset to `awaiting_photo`, `NARRATION_RESET_REPLY`,
  narrator never called). Component
  `tests/component/test_narrator_flow.py:184`
  (`test_degraded_synthesis_apologises_and_retries_on_the_next_update` — no
  voice, retryable, next update completes, `len(narrator.calls) == 2`).
  Bouncer `tests/unit/test_bouncer.py:1572`
  (`test_handoff_narrator_failure_leaves_the_chat_retryable`). Implementation:
  `narrator.py:267-276` (synthesis catch), `:252-261` (missing script),
  `_send` swallows at `:147-170`.
- [x] After `narrator_sent`, non-command input gets the light "observation
  complete" reply; no re-synthesis, no state change. `/start` resets
  `narration`/`narrator_sent` with everything else. *Evidence: bouncer
  dispatch tests + session reset test.*
  **Evidence:** `tests/unit/test_bouncer.py:1548`
  (`test_done_and_narrated_keeps_the_light_reply` — only
  `OBSERVATION_COMPLETE_REPLY`, `narrator.calls == []`, `voices == []`);
  router falls through to the Interviewer only when narrated
  (`bouncer.py:241-250`); component `test_narrator_flow.py:170`
  (`texts[9]==OBSERVATION_COMPLETE_REPLY`). `/start`:
  `tests/unit/test_bouncer.py:1631`
  (`test_start_clears_the_narration_and_the_narrator_sent_flag`),
  `tests/unit/test_session.py:490`
  (`test_reset_clears_the_narration_and_the_narrator_sent_flag`), component
  `tests/component/test_narrator_flow.py:289`
  (`test_start_after_the_voice_note_resets_the_narration_state`).

## 3. Contract & safety checks

- [x] `chat_id` stays `int`; `SESSION_VERSION` bumped to 5; `narration` and
  `narrator_sent` are stored, typed state (never inferred).
  **Evidence:** `SESSION_VERSION = 5` `session.py:35`;
  `SessionState.narration: bytes | None` / `narrator_sent: bool`
  `session.py:87-91`; typed-field rejection tests
  `tests/unit/test_session.py:512` (non-bytes `narration`) and `:520`
  (non-bool `narrator_sent`) → `ValidationError`; version-assert tests
  `tests/unit/test_session.py:102-114` and `:27`; `chat_id` stays `int`
  (`models.py` `Chat.id: int`; `handle_narrating(... chat_id: int ...)`
  `narrator.py:188`; `send_voice(chat_id: int, ...)` `transport.py:271`).
- [x] The audio crosses every boundary as typed data (`NarratorLLM` port,
  `AudioPayload`), never a raw payload.
  **Evidence:** typed port `NarratorLLM.synthesize(script) -> AudioPayload`
  `narrator.py:50-55`; `AudioPayload(BaseModel)` (`data: bytes`,
  `mime_type: str`) `gemini.py:77`; decoded by `_candidate_audio`
  `gemini.py:290-320` / `_decode_audio` `:342-360`; routing accepts only an
  `AudioPayload` and re-validates `narrator.py:268`; session stores typed
  `bytes` `session.py:233-243`; transport takes `audio: bytes` +
  `media_type: str` `transport.py:270-294`.
- [x] Single redaction implementations survive: the API key never appears in
  any raised `NarratorError`/`GeminiError` message or log line (plain +
  escaped/control-character forms); the bot token shares the same guarantee
  for `sendVoice`.
  **Evidence:** shared `_redact` in `gemini.py`; narrator canaries
  `tests/unit/test_narrator.py:414`
  (`test_every_narrator_failure_redacts_the_api_key` — five key-bearing
  branches, checks `str(exc)`, formatted traceback, `JsonFormatter` line,
  `__cause__ is None`, `__suppress_context__`) and `:434`
  (`test_narrator_redacts_a_control_character_key` —
  `SECRET-NARRATOR-KEY-\nCANARY`); `:454` (empty key rejected at
  construction). `sendVoice` token: `tests/unit/test_transport.py:740`
  (`test_send_voice_never_leaks_the_token_in_a_failure`) and `:754`
  (`test_send_voice_redacts_a_control_character_token`).
- [x] The **audio bytes and the script are never written to logs**
  (`narrator_ready` carries only byte count + mime). `.env` remains
  gitignored/untracked.
  **Evidence:** `tests/unit/test_narrator.py:602`
  (`test_handle_narrating_never_logs_the_script_or_audio` — scans every log
  record's message and `__dict__` for a script canary and an audio canary),
  `:625` (`test_handle_narrating_ready_event_reports_only_byte_count_and_mime`
  — `byte_count`, `mime`, no script), `:723`
  (resend never logs the stored audio). Production: `narrator_ready` emits
  only `byte_count` + `mime` `narrator.py:281-289`; `narration_sent`
  `:310-318`; no script/audio in any `extra`. `.env` in `.gitignore:7`;
  `git check-ignore -v .env` → ignored; `git ls-files .env` → empty
  (untracked); `.env` absent from `git status`.

## 4. Spec reconciliation (surfacing drift)

- [x] Any deviation from `requirements.md` / `plan.md` recorded here with the
  user's approval (mirrors Phases 2–5).
  **Recorded:** the OGG-subtype validation and the normalised send media type
  — see "Deviations recorded at verification" below. No blocking deviation.
- [x] `SPECS/ROADMAP.md` Phase 6 marked complete (only after this checklist).
  **Done (2026-10-14):** Phase 6 marked complete with date, branch, delivered
  summary, test counts (691), hooks result, network-isolation evidence and
  live smoke status. See `SPECS/ROADMAP.md` §6.
- [x] `SPECS/TECH.md` updated: Narrator stage and `NarratorLLM` port, the final
  Bouncer-as-router handoff chain (dossier → portrait → script → voice note),
  session v5 (`narration` + `narrator_sent`). With user approval.
  **Done (2026-10-14):** Framework + architecture bullets now cover the
  `NarratorLLM` port, `generate_audio`, the multipart `sendVoice`, and the
  dossier → portrait → script → voice note same-update chain; Session state
  gains Version 5 and the `done && script_sent && !narrator_sent` retry phase;
  Models note the Narrator returns OGG (Opus) natively. Approval basis:
  requirements decisions 1–4 (typed port, constant+override, same-update
  stored once-only, component + conditional smoke), locked 2026-10-12.
- [x] `SPECS/MISSION.md` still matches (step 6 already describes the narrated
  voice note; no scope drift expected).
  **Confirmed (2026-10-14):** mission step 6 — "The Narrator (TTS, not an
  agent) reads the script aloud and delivers an OGG/MP3 voice note" — matches
  Phase 6 exactly (Gemini TTS, OGG/Opus via `sendVoice`). No edit needed; no
  drift.
- [x] `README.md` reflects Phase 6: status, narrator behaviour,
  `GEMINI_NARRATOR_MODEL` / `GEMINI_NARRATOR_VOICE` / `GEMINI_NARRATOR_TIMEOUT`,
  the OGG-`sendVoice` contract.
  **Done (2026-10-14):** status line → Phase 6; "What the bot does" gains the
  Narrator bullet (same-update voice note, one `user` content with voice
  guidance + verbatim script, `responseModalities: ["AUDIO"]`, OGG payload
  validated before send, `sendVoice` multipart, `SessionState.narration` +
  `narrator_sent`, exact-once, re-send stored audio, apology/retry) and the
  light reply now requires `narrator_sent`; Configure lists the three
  `GEMINI_NARRATOR_*` variables and the Narrator in the key's consumers; logs
  section adds the narrator events + "audio bytes/script never logged" +
  all-five-adapters redaction; the pydantic-note mentions the OGG/no-encoder
  point; Test + layout + Roadmap updated.

## 5. Manual smoke (live, opt-in — runs only with a working `GEMINI_API_KEY`)

- [x] **Live Gemini smoke (if a working key exists in `.env`):** a witness
  script (not part of the suite, no committed credentials) runs
  `GeminiNarrator.synthesize("<the Scripter's sample narration>")` once with
  the real key and asserts a non-empty, `audio/*`-mime payload ≤
  `MAX_NARRATION_BYTES` comes back (and writes the note to a temp file for
  the user to play). Never prints or commits the key. *Result recorded here.*
  **Result: not reached — key rejected by Google.** Witness
  `/tmp/opencode/live_narrator_smoke.py` loads `GEMINI_API_KEY` from `.env`,
  calls `GeminiNarrator.synthesize(sample_script)`, asserts `audio/ogg`
  non-empty ≤ `MAX_NARRATION_BYTES` and would write
  `/tmp/opencode/narration_smoke.ogg`. Real run output:
  `NarratorError: http error: HTTP 400 Bad Request: API key not valid. Please
  pass a valid API key.` The witness additionally asserts the key never leaks
  into the error (it does not; no key was printed). Per the approved
  classification this is provider-side/key-schema blocking, **not** an adapter
  failure; no real audio bytes were observed and no attempt was made to patch
  or replace the key. Re-check when the user supplies a working `AIza…` key.
  - If the configured key is still provider-blocked/invalid, record "not
    reached — key rejected by Google (HTTP 403/400)", which is not an adapter
    failure, and re-check when the user supplies a working key.
    **Recorded as above (2026-10-14, HTTP 400).**
- [x] **Live Telegram run (manual, user-side):** the full "send an image on
  your phone → bouncer check → interview → hybrid portrait → script → voiced
  documentary" sequence cannot be driven from this environment; documented as
  an open manual step for a consenting user with a working key.
  **Recorded — open manual step (2026-10-14).**

## 6. Merge gate

- [ ] All of the above checked with evidence in the PR description.
  **Recorded: full evidence set is in this file for the PR description.**
  (Checkbox left for the build agent to tick when the PR is actually opened.)
- [ ] Branch `feature/2026-10-12-narrator` pushed; PR opened against `main`.
- [ ] **User** merges (the build agent does not merge).

## Deviations recorded at verification

- **OGG-subtype validation (recorded deviation).** The plan (`plan.md` §2) and
  requirements (`requirements.md` Scope, boundary table) specify the audio
  contract as `mime_type` starts with `audio/*`; the implementation requires
  the **OGG subtype** explicitly — `mime.lower().startswith("audio/ogg")`
  (`narrator.py:122-128`), rejecting `audio/wav` / `audio/mpeg` /
  `text/plain` / empty (`tests/unit/test_narrator.py:264`). This follows the
  locked format decision (decision 1: Gemini TTS returns OGG/Opus, the format
  `sendVoice` accepts; MP3 would force an encoder in) and prevents a
  valid-looking non-OGG payload from being relabelled `audio/ogg` downstream.
  No spec text contradicts the stricter bound; it is strictly safer. The
  deviation is documented in `narrator.py:110-121`.
- **Normalised send media type (related, non-blocking).** The validation
  checklist text anticipated `send_voice` being called "with the model's
  `media_type`"; the implementation normalises every send to the clean locked
  `audio/ogg` (`_OGG_MEDIA_TYPE`, `narrator.py:132`, `:280`, `:297`), so the
  model's decorated `audio/ogg; codecs=opus` is never forwarded
  (`tests/unit/test_narrator.py:551`
  `test_handle_narrating_normalises_the_model_mime_before_send_voice`). This
  matches the locked OGG decision and keeps the `narrator_ready`/`narration_sent`
  logs to a single clean mime; it does not change the bytes or the delivery
  count.
- **Live smoke:** not reached (provider-blocked key, HTTP 400). Expected by
  the plan/requirements ("currently the configured key is invalid — recorded
  honestly as 'not reached' otherwise").
- No other deviations. Nothing blocking. Final status: `scripts/test` 691
  passed (also 691 with all four socket entry points patched to raise),
  `scripts/hooks` all checks passed (2026-10-14).
