# Mission — The Telegram Documentaries

## Vision

**The Telegram Documentaries** is a Telegram bot that turns a single portrait
photo into a narrated, comedy-wildlife documentary about the person in it. A
user uploads a photo of themselves to **@MiargueBot** and, after a short
interview, receives a hybrid animal portrait and a dramatic British-narrated
MP3 audio message describing their "natural habitat."

## The end-to-end experience

1. User sends `/start` (or just a photo) and uploads one portrait.
2. **The Bouncer** checks the image: is a human present? Yes → move on.
   No → a cheeky rejection message and the conversation resets.
3. **The Interviewer** asks exactly 5 questions about the user, one at a
   time, building a behavioural dossier and suggesting an animal.
4. **The Converter** fuses the original photo with the interview dossier into
   a hybrid animal portrait, sent directly to the chat.
5. **The Scripter** writes a 60–90 word dramatic documentary narration.
6. **The Narrator** (TTS, not an agent) reads the script aloud and delivers an
   MP3 audio message via the Gemini Interactions API.

## In scope

- The five-stage pipeline above, in English, driven over Telegram **long
  polling** with in-memory session state keyed by `chat_id`.
- `/start` and `/restart`: run an **explicit reset routine** without
  restarting the process. It performs three named actions — (1) **cancel
  active sessions**: the requesting chat's session returns to the greeting
  stage (`awaiting_photo`), handled atomically so no interview turn survives
  it; (2) **purge ephemeral assets**: drop the in-memory portrait photo and
  MP3 narration buffers (all media is memory-only — there is no temp media on
  disk); (3) **re-initialize agent memory**: clear the interview log, the
  pending question, the dossier, the stored script, the deliver flags and the
  album dedupe. The routine logs exactly what it discarded (flags and counts
  only — never media bytes or user text). The command fires in any case, with
  or without an `@botname` suffix, as a message or as a photo caption.
- Graceful handling of out-of-order input (e.g. text while waiting for a
  photo, a second photo mid-interview) — the bot re-prompts instead of
  crashing or losing the session.

## Out of scope (YAGNI)

- **No video, animation, or sound effects** — photo + audio message only.
- **No deployment target** — local development via long polling; no webhooks,
  no public URL, no hosting.
- **No long-term storage** — photos, dossiers, and media live in memory only
  and vanish on reset or process exit.
- **No multi-photo uploads** — exactly one portrait at a time; albums are
  rejected or reduced to the first photo.
- Nothing else not listed above counts as in scope.

## Success criteria

- Happy path: photo → interview → hybrid portrait → narrated MP3 audio
  message, all delivered as Telegram messages.
- `/restart` (and `/start`) runs the reset routine correctly: the session is
  cancelled, in-memory media is purged and agent memory is re-initialized,
  with nothing left over and no other chat affected.
- Out-of-order text/media at any stage is handled without a crash or a leaked
  session.

## Non-negotiables

- **Never leak another user's session** — all state is isolated per `chat_id`.
- **Never hardcode or commit secrets** — tokens live in `.env` only.