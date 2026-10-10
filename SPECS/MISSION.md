# Mission — The Telegram Documentaries

## Vision

**The Telegram Documentaries** is a Telegram bot that turns a single portrait
photo into a narrated, comedy-wildlife documentary about the person in it. A
user uploads a photo of themselves to **@MiargueBot** and, after a short
interview, receives a hybrid animal portrait and a dramatic British-narrated
voice note describing their "natural habitat."

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
   OGG/MP3 voice note.

## In scope

- The five-stage pipeline above, in English, driven over Telegram **long
  polling** with in-memory session state keyed by `chat_id`.
- `/start` and `/restart`: purge session state and temporary media without
  restarting the process.
- Graceful handling of out-of-order input (e.g. text while waiting for a
  photo, a second photo mid-interview) — the bot re-prompts instead of
  crashing or losing the session.

## Out of scope (YAGNI)

- **No video, animation, or sound effects** — photo + voice note only.
- **No deployment target** — local development via long polling; no webhooks,
  no public URL, no hosting.
- **No long-term storage** — photos, dossiers, and media live in memory only
  and vanish on reset or process exit.
- **No multi-photo uploads** — exactly one portrait at a time; albums are
  rejected or reduced to the first photo.
- Nothing else not listed above counts as in scope.

## Success criteria

- Happy path: photo → interview → hybrid portrait → narrated voice note, all
  delivered as Telegram messages.
- `/restart` (and `/start`) resets state and clears temporary media correctly.
- Out-of-order text/media at any stage is handled without a crash or a leaked
  session.

## Non-negotiables

- **Never leak another user's session** — all state is isolated per `chat_id`.
- **Never hardcode or commit secrets** — tokens live in `.env` only.