# Validation — The Narrator (Phase 6)

How we know Phase 6 (Revision R1) is correct and safe to merge. A step is
"done" only when the evidence is recorded and the user has approved any spec
reconciliation.

Verifier: _(filled at verification)_. Date: _(filled at verification)_.
Branch: `feature/2026-10-12-narrator`. Format under test: **MP3 audio message
via Telegram `sendAudio`**, synthesised through the **Gemini Interactions API**
(`gemini-3.8-flash-lite-tts`).

> **Revision R1 (2026-10-14).** The pre-revision validation (OGG/`sendVoice`,
> "live smoke: not reached — key rejected") is **superseded**. That design was
> proven impossible against the live API; the rework is MP3 via `sendAudio`.

## 1. Automated checks

- [ ] `scripts/test` — full suite green, including the new `narrator` suite and
  the component flow; **all 560 pre-existing tests** stay green except the
  intended narrator/transport/config edits (`send_voice` → `send_audio`; the
  Interactions request/response shape; the new default model/voice). _Evidence
  to be recorded: pass count + diffstat._
- [ ] `scripts/hooks` — `compileall` + smoke import + `ruff` + `mypy` green.
  _Evidence to be recorded._
- [ ] No test performs network I/O: the full suite is re-run with
  `socket.socket.connect`, `connect_ex`, `socket.create_connection` and
  `socket.getaddrinfo` patched to raise (throwaway plugin, sanity-checked to
  fire) — must stay green. `GeminiNarrator.urlopen` / `UrllibTransport.urlopen`
  stay injectable; the component flow uses the scripted `FakeNarrator`.
  _Evidence to be recorded._

## 2. Behaviour checklist (maps to acceptance + Revised decisions)

- [ ] A playable **audio message** (`sendAudio`, `audio/mpeg`,
  `narration.mp3`) arrives **right after the script message, in the same
  update, with no user input**: teaser → `sendPhoto` → script → `sendAudio`.
  _Evidence to be recorded._
- [ ] The raw MP3 payload is **stored on the session** (`state.narration ==
  synthesized bytes`) and `narrator_sent` is False *at* send time, True after.
  _Evidence to be recorded._
- [ ] The request targets `gemini-3.8-flash-lite-tts` by default and is an
  **Interactions** call: `input[0].content[0].text == script` with a
  `speech_metadata.style` annotation; `response_format == {"type":"audio",
  "mime_type":"audio/mp3"}`; `generation_config.speech_config[0].voice ==
  DEFAULT_NARRATOR_VOICE`; the script is **never** duplicated into an
  annotation. _Evidence to be recorded._
- [ ] The default voice is `en-gb-storyteller-4`, overridable via
  `GEMINI_NARRATOR_VOICE` (env-beats-file); the style is the fixed
  `DEFAULT_NARRATOR_STYLE`. _Evidence to be recorded._
- [ ] Payload validated mechanically before storage: `mime_type` starts with
  `audio/mp3` or `audio/mpeg`, non-empty bytes, ≤ `MAX_NARRATION_BYTES`;
  violations → `NarratorError` (unusable output) + apology + retry.
  _Evidence to be recorded._
- [ ] Delivery is exactly-once: `narrator_sent` set only after a successful
  `sendAudio`; a failed send re-sends the **stored bytes** with **no
  re-synthesis**. _Evidence to be recorded._
- [ ] Degraded synthesis (failure / invalid payload) → apology, loud
  `narrator_failed`, no flag, retry next update; missing script on a `done`
  chat → fail loud + reset-and-reprompt; nothing raises into the polling loop.
  _Evidence to be recorded._
- [ ] After `narrator_sent`, non-command input gets the light "observation
  complete" reply; `/start` resets `narration`/`narrator_sent`.
  _Evidence to be recorded._

## 3. Contract & safety checks

- [ ] `chat_id` stays `int`; `SESSION_VERSION == 5`; `narration`/`narrator_sent`
  typed state. _Evidence to be recorded._
- [ ] The audio crosses every boundary as typed data (`NarratorLLM` port,
  `AudioPayload`), never a raw payload; the Interactions response is parsed
  into `AudioPayload` at the edge. _Evidence to be recorded._
- [ ] Single redaction implementations survive: the API key never appears in
  any raised `NarratorError`/`GeminiError` message or log line (plain +
  escaped/control-character forms); the bot token shares the guarantee for
  `sendAudio`. _Evidence to be recorded._
- [ ] The **audio bytes and the script are never written to logs**
  (`narrator_ready` carries only byte count + mime). `.env` remains
  gitignored/untracked. _Evidence to be recorded._

## 4. Spec reconciliation (surfacing drift)

- [ ] Any deviation from `requirements.md` / `plan.md` (R1) recorded here with
  the user's approval. _To be recorded._
- [ ] `SPECS/ROADMAP.md` Phase 6 marked complete (MP3 audio message).
  _To be recorded._
- [ ] `SPECS/TECH.md` updated: `NarratorLLM` port, Interactions audio path,
  final handoff chain ending in `sendAudio`, MP3 (no OGG/encoder), session v5.
  _To be recorded._
- [ ] `SPECS/MISSION.md` step 6 matches (audio message wording). _To be
  recorded._
- [ ] `README.md` reflects Phase 6 R1 (MP3 `sendAudio`, Interactions API,
  `gemini-3.8-flash-lite-tts`, `GEMINI_NARRATOR_MODEL` / `_VOICE` / `_TIMEOUT`).
  _To be recorded._

## 5. Manual smoke (live, opt-in — runs with the configured `GEMINI_API_KEY`)

- [ ] **Live Gemini smoke:** a witness script (not part of the suite, no
  committed credentials) runs `GeminiNarrator.synthesize("<the Scripter's
  sample narration>")` once with the real key and asserts a non-empty
  `audio/mp3*`/`audio/mpeg` payload ≤ `MAX_NARRATION_BYTES`, writing the note
  to `/tmp/opencode/narration_smoke.mp3`. Never prints or commits the key.
  **Result: must PASS** (the pre-revision "not reached" outcome is obsolete —
  the key is valid and the Interactions endpoint returns MP3). _Result to be
  recorded._
- [ ] **Live Telegram run (manual, user-side):** the full "send an image on
  your phone → bouncer check → interview → hybrid portrait → script → spoken
  documentary" sequence cannot be driven from this environment; documented as
  an open manual step for a consenting user. _To be recorded._

## 6. Merge gate

- [ ] All of the above checked with evidence in the PR description.
- [ ] Branch `feature/2026-10-12-narrator` pushed; PR #7 updated against
  `main`.
- [ ] **User** merges (the build agent does not merge).

## Deviations recorded at verification

_To be completed by the verifier for Revision R1._
