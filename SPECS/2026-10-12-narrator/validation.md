# Validation — The Narrator (Phase 6)

How we know Phase 6 (Revision R1) is correct and safe to merge. A step is
"done" only when the evidence is recorded and the user has approved any spec
reconciliation.

Verifier: **verifier agent**. Date: **2026-10-15**.
Branch: `feature/2026-10-12-narrator`. Format under test: **MP3 audio message
via Telegram `sendAudio`**, synthesised through the **Gemini Interactions API**
(`gemini-3.8-flash-lite-tts`).

> **Revision R1 (2026-10-14).** The pre-revision validation (OGG/`sendVoice`,
> "live smoke: not reached — key rejected") is **superseded**. That design was
> proven impossible against the live API; the rework is MP3 via `sendAudio`.

## 1. Automated checks

- [x] `scripts/test` — full suite green, including the new `narrator` suite and
  the component flow; **all 560 pre-existing tests** stay green except the
  intended narrator/transport/config edits (`send_voice` → `send_audio`; the
  Interactions request/response shape; the new default model/voice).
  **Result: `697 passed`** (Phase 5 baseline was 560; Phase 6 R1 adds
  137 net tests). Working-tree diffstat (R1 rework vs `HEAD`, code + tests):
  16 files changed, 662 insertions(+), 555 deletions(-). (The committed branch
  tip vs `main` — `git diff --shortstat main...HEAD` — is 25 files, 3682
  insertions(+), 110 deletions(-), i.e. the pre-revision implementation plus the
  R1 spec commit.)
- [x] `scripts/hooks` — `compileall` + smoke import + `ruff` + `mypy` green.
  **Result:** `compileall` ok; smoke import ok; `ruff` → "All checks passed!";
  `mypy` → "Success: no issues found in 42 source files"; "all checks passed".
- [x] No test performs network I/O: the full suite is re-run with
  `socket.socket.connect`, `connect_ex`, `socket.create_connection` and
  `socket.getaddrinfo` patched to raise (throwaway plugin, sanity-checked to
  fire) — must stay green. `GeminiNarrator.urlopen` / `UrllibTransport.urlopen`
  stay injectable; the component flow uses the scripted `FakeNarrator`.
  **Result:** throwaway plugin `/tmp/opencode/no_socket_plugin.py` loaded with
  `-p no_socket_plugin` (PYTHONPATH=/tmp/opencode); its sanity test
  `/tmp/opencode/test_socket_patch_sanity.py` **fired (4 passed)** — each of the
  four entry points raises. Full suite with the patch:
  **`697 passed`**.

## 2. Behaviour checklist (maps to acceptance + Revised decisions)

- [x] A playable **audio message** (`sendAudio`, `audio/mpeg`,
  `narration.mp3`) arrives **right after the script message, in the same
  update, with no user input**: teaser → `sendPhoto` → script → `sendAudio`.
  **Evidence:** `tests/component/test_narrator_flow.py::test_full_pass_delivers_the_audio_message_right_after_the_script`
  asserts the single pass yields `sendPhoto`, then script text, then exactly one
  `send_audio(audio/mpeg, narration.mp3)` — before the follow-up turn is polled.
- [x] The raw MP3 payload is **stored on the session** (`state.narration ==
  synthesized bytes`) and `narrator_sent` is False *at* send time, True after.
  **Evidence:** `test_handle_narrating_flag_is_evidence_not_intent` (records the
  flag inside `send_audio`: False at send, True after);
  `test_handle_narrating_synthesises_and_sends_the_audio` (`state.narration ==
  AUDIO`, `narrator_sent is True`); component flow 177-179.
- [x] The request targets `gemini-3.8-flash-lite-tts` by default and is an
  **Interactions** call: `input[0].content[0].text == script` with a
  `speech_metadata.style` annotation; `response_format == {"type":"audio",
  "mime_type":"audio/mp3"}`; `generation_config.speech_config[0].voice ==
  DEFAULT_NARRATOR_VOICE`; the script is **never** duplicated into an
  annotation. **Evidence:** `test_synthesize_posts_the_locked_interactions_request_body`
  (exact body equality), `test_synthesize_keeps_the_raw_script_only_in_the_content_text`
  (`SCRIPT not in annotations`), `test_synthesize_uses_the_default_narrator_model`
  (model in body, not URL), `test_synthesize_posts_to_the_interactions_endpoint`.
- [x] The default voice is `en-gb-storyteller-4`, overridable via
  `GEMINI_NARRATOR_VOICE` (env-beats-file); the style is the fixed
  `DEFAULT_NARRATOR_STYLE`. **Evidence:** `test_get_narrator_voice_defaults_to_the_prebuilt_narrator_voice_id`
  (+ env/file/blank tests) in `test_config.py`; `test_synthesize_uses_the_configured_voice`
  / `_style`.
- [x] Payload validated mechanically before storage: `mime_type` starts with
  `audio/mp3` or `audio/mpeg`, non-empty bytes, ≤ `MAX_NARRATION_BYTES`;
  violations → `NarratorError` (unusable output) + apology + retry.
  **Evidence:** `validate_audio` in `narrator.py`; `test_validate_audio_accepts_the_mp3_family`,
  `test_validate_audio_rejects_a_payload_outside_the_mp3_family`,
  `test_synthesize_rejects_an_oversized_audio_payload`,
  `test_synthesize_accepts_a_payload_exactly_at_the_size_limit`,
  `test_synthesize_rejects_an_empty_audio_payload`; component
  `test_degraded_synthesis_apologises_and_retries_on_the_next_update`
  (`NARRATION_APOLOGY_REPLY` + retry).
- [x] Delivery is exactly-once: `narrator_sent` set only after a successful
  `sendAudio`; a failed send re-sends the **stored bytes** with **no
  re-synthesis**. **Evidence:** `test_handle_narrating_stores_the_audio_even_when_the_send_fails`,
  `test_handle_narrating_resends_the_stored_audio_without_resynthesising`
  (`narrator.calls == []`), component
  `test_failed_send_reuses_the_stored_audio_without_resynthesising`.
- [x] Degraded synthesis (failure / invalid payload) → apology, loud
  `narrator_failed`, no flag, retry next update; missing script on a `done`
  chat → fail loud + reset-and-reprompt; nothing raises into the polling loop.
  **Evidence:** `test_handle_narrating_apologises_and_stays_retryable_on_synthesis_failure`,
  `test_handle_narrating_apologises_on_an_invalid_synthesised_payload`,
  `test_handle_narrating_resets_and_reprompts_when_the_script_is_missing`,
  `test_handle_narrating_never_raises_on_an_unexpected_synthesis_error` in `test_narrator.py`.
- [x] After `narrator_sent`, non-command input gets the light "observation
  complete" reply; `/start` resets `narration`/`narrator_sent`.
  **Evidence:** `test_done_and_narrated_keeps_the_light_reply` and
  `test_start_clears_the_narration_and_the_narrator_sent_flag` in `test_bouncer.py`;
  component `test_start_after_the_audio_message_resets_the_narration_state`.

## 3. Contract & safety checks

- [x] `chat_id` stays `int`; `SESSION_VERSION == 5`; `narration`/`narrator_sent`
  typed state. **Evidence:** `session.py` `SESSION_VERSION = 5`; `test_session.py`
  (`state.version == 5`, `narration` bytes / `narrator_sent` bool, rejection of
  `narration=42` / `narrator_sent="banana"`).
- [x] The audio crosses every boundary as typed data (`NarratorLLM` port,
  `AudioPayload`), never a raw payload; the Interactions response is parsed
  into `AudioPayload` at the edge. **Evidence:** `GeminiJsonClient.generate_interaction_audio`
  returns `AudioPayload`; `GeminiNarrator.synthesize -> AudioPayload`;
  `test_generate_interaction_audio_returns_the_decoded_bytes_and_mime_type`.
- [x] Single redaction implementations survive: the API key never appears in
  any raised `NarratorError`/`GeminiError` message or log line (plain +
  escaped/control-character forms); the bot token shares the guarantee for
  `sendAudio`. **Evidence:** `test_every_narrator_failure_redacts_the_api_key`,
  `test_narrator_redacts_a_control_character_key`,
  `test_every_generate_interaction_failure_redacts_the_api_key`,
  `test_send_audio_never_leaks_the_token_in_a_failure`,
  `test_send_audio_redacts_a_control_character_token`.
- [x] The **audio bytes and the script are never written to logs**
  (`narrator_ready` carries only byte count + mime). `.env` remains
  gitignored/untracked. **Evidence:** `test_handle_narrating_never_logs_the_script_or_audio`,
  `test_handle_narrating_ready_event_reports_only_byte_count_and_mime`,
  `test_handle_narrating_stored_resend_never_logs_the_audio`; `git check-ignore
  .env` → `.env`; `git ls-files` shows no `.env`.

## 4. Spec reconciliation (surfacing drift)

- [x] Any deviation from `requirements.md` / `plan.md` (R1) recorded here with
  the user's approval. **See "Deviations recorded at verification" below.**
- [x] `SPECS/ROADMAP.md` Phase 6 marked complete (MP3 audio message).
  **Updated** (Phase 6 status block replaced with the R1 delivery: MP3 via
  `sendAudio`, `generate_interaction_audio`, session v5, 697 tests, live smoke
  passed).
- [x] `SPECS/TECH.md` updated: `NarratorLLM` port, Interactions audio path,
  final handoff chain ending in `sendAudio`, MP3 (no OGG/encoder), session v5.
  **Updated.**
- [x] `SPECS/MISSION.md` step 6 matches (audio message wording). **Updated**
  ("MP3 audio message"; other "voice note" wording reconciled).
- [x] `README.md` reflects Phase 6 R1 (MP3 `sendAudio`, Interactions API,
  `gemini-3.8-flash-lite-tts`, `GEMINI_NARRATOR_MODEL` / `_VOICE` / `_TIMEOUT`).
  **Updated.**

## 5. Manual smoke (live, opt-in — runs with the configured `GEMINI_API_KEY`)

- [x] **Live Gemini smoke:** a witness script (not part of the suite, no
  committed credentials) runs `GeminiNarrator.synthesize("<the Scripter's
  sample narration>")` once with the real key and asserts a non-empty
  `audio/mp3*`/`audio/mpeg` payload ≤ `MAX_NARRATION_BYTES`, writing the note
  to `/tmp/opencode/narration_smoke.mp3`. Never prints or commits the key.
  **Result: PASS.** `python /tmp/opencode/live_narrator_smoke.py` from the repo
  root printed `mime_type = 'audio/mp3; rate=24000; channels=1'`, `bytes =
  339235`, `OK: wrote /tmp/opencode/narration_smoke.mp3` (exit 0). The written
  file carries a valid `ID3` v2 header (the key is never printed).
- [ ] **Live Telegram run (manual, user-side):** the full "send an image on
  your phone → bouncer check → interview → hybrid portrait → script → spoken
  documentary" sequence cannot be driven from this environment; documented as
  an open manual step for a consenting user. **Not driven here — open manual
  step for the user** (unchanged from the plan).

## 6. Merge gate

- [x] All of the above checked with evidence in the PR description. **This
  file is the evidence**; the PR description must be updated to link/embed it
  after the R1 commit is pushed.
- [ ] Branch `feature/2026-10-12-narrator` pushed; PR #7 updated against
  `main`. **Pending — post-verification.** The R1 implementation is still
  uncommitted in the working tree and the remote branch is at the pre-revision
  `007aac7`; PR #7 (open) still describes the superseded OGG design. The build
  agent commits/pushes and updates the PR after this verification.
- [ ] **User** merges (the build agent does not merge). **Pending user action.**

## Deviations recorded at verification

**No deviations from the R1 functional contract were found:** the request body,
endpoint, model, voice, style-as-metadata, MP3 validation window, exact-once
delivery, stored-bytes re-send, redaction and reset semantics all match
`requirements.md` and `plan.md`. Two non-blocking cosmetic notes were recorded;
the verifier made no code change.

1. **`tests/unit/test_session.py` narration fixtures use an OGG-shaped opaque
   byte literal** (`b"OggS\x00..."` at lines 456, 473, 494). This is
   **sanctioned by the R1 plan**, which kept the existing session tests
   unchanged and treated session as a wording-only change (§4: "Existing session
   tests stay green unchanged (keep them, adjust only wording assertions if
   any)"). `session.py`'s own comment was updated to "audio payload (MP3)". The
   bytes are opaque to the store, so the prefix is immaterial — recorded only so
   the residue is not later mistaken for drift.
2. **README test-count was stale** (`test_narrator.py (54)`); the actual
   collected count is **58**. Corrected in the README reconciliation.
