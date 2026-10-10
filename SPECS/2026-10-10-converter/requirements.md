# Requirements — The Converter (Phase 4)

## Context

- **Phase 3 (The Interviewer) is merged to `main`** (PR #3, + PR #4 hardening):
  a typed `InterviewLLM` port, the dossier (`summary` / `suggested_animal` /
  `animal_reason`) stored on the session at `phase == done`, and the original
  portrait bytes retained on the session (`SessionState.photo`) since Phase 2.
  `scripts/test` = 379 passed, `scripts/hooks` green, no test touches the
  network.
- **This is Phase 4** from `SPECS/ROADMAP.md`: "Gemini 3.1 Flash Image:
  native multimodal fusion of original photo + interview dossier into a hybrid
  animal portrait. Result delivered directly as a Telegram photo."
- **Branch:** `feature/2026-10-10-converter` (created off `main`).
- Model per `SPECS/TECH.md`: **Gemini 3.1 Flash Image**
  (`gemini-3.1-flash-image`).
- When Phase 3 ends, the bot has Q5's dossier report in the chat and a
  `done`-phase session holding both artifacts this phase consumes: the photo
  and the dossier. Nothing consumes them yet — this phase closes that gap.

## Goal

A `done` chat receives a **hybrid animal portrait**: the original portrait
photo and the behavioural dossier are fed to Gemini 3.1 Flash Image **in a
single multimodal call** (pure multimodal routing — binary image payload +
personality text together, **no intermediate text step**), and the returned
image is delivered straight to the chat via `sendPhoto`. Generation starts
**automatically in the same update** that finishes the interview.

## Decisions locked with the user (2026-10-10)

1. **Immediate, same-update handoff.** The conversion run starts right after
   the Q5 dossier report has been sent (no user input needed). A short teaser
   message ("painting your portrait…") fires before the slow image call so the
   chat never sits silently. Any failure leaves the chat retryable: a `done`
   chat without a portrait re-attempts conversion on the next update.
2. **Verification scope.** In-process component test (fakes) proves the whole
   photo → 5 answers → dossier → portrait flow deterministically; **one live
   Gemini 3.1 Flash Image smoke call** (a public portrait fetched at
   verification time) proves real image bytes come back. Driving a real
   Telegram chat is impossible from this environment (no client to talk to the
   bot), so Telegram delivery stays fake-verified and a full live manual run is
   documented as an open manual step for a consenting user.
3. **UX / robustness touches (all included).** Teaser before generation; a
   small caption on the sent photo; a 10 MB byte-size guard + `image/*` mime
   check on the generated image before it is sent; a 120 s timeout for the
   image-generation client (Flash Image routinely exceeds the 30 s default).
4. **Framework (drift by precedent).** Consistent with Phases 2–3, the
   Converter is **not** built on Google ADK: it is a **typed `ConverterLLM`
   port** with a Gemini REST adapter. The shared `gemini.py` client gains an
   image-in/image-out capability (`generate_image`) beside the existing
   `generate_json`, keeping the single POST/redaction implementation. ADK
   remains deferred (`SPECS/TECH.md`).
5. **State model.** `done` stays the interview-done phase; the session gains a
   `portrait_sent: bool` flag (`SESSION_VERSION` bumped to 3). A `done` chat
   with `portrait_sent == False` is "interview finished, portrait pending" —
   conversion re-runs on the next update there; `portrait_sent == True` is the
   true terminal. A fifth phase was considered and rejected: the flag is
   stored state (never inferred) and is simpler than an extra
   `converting`/`converted` phase pair (YAGNI).
6. **Router.** The Bouncer remains the router across stages (it already
   dispatches on phase). It sends `done && !portrait_sent` to the Converter
   and, after an interview update completes in `done`, triggers the same-update
   handoff itself — neither the Interviewer nor the Converter knows about the
   other (hub-and-spoke, `SPECS/TECH.md`).

## Scope (in)

- **Session extension** (`session.py`): `SESSION_VERSION = 3`;
  `SessionState.portrait_sent: bool = False`; new store method
  `mark_portrait_sent(chat_id)`; `reset` clears the flag (along with photo,
  interview log, dossier).
- **Shared Gemini client** (`gemini.py`): new method
  `generate_image(request_body) -> InlineImagePayload` (typed: `bytes` +
  `mime_type`) — same POST/failure-normalisation/redaction path as
  `generate_json`, but reads the first `inlineData` part of the first
  candidate instead of text, and decodes the base64 `data`. Missing/empty
  image part is a normalised `GeminiError`. `generate_json` unchanged.
- **`ConverterLLM` port + `GeminiConverter` adapter** (`converter.py`):
  - `hybridize(photo: bytes, dossier: Dossier, *, media_type: str =
    "image/jpeg") -> GeneratedImage` (`GeneratedImage`: `data: bytes`,
    `mime_type: str`).
  - Request body: **one** `user` content; parts `[text-directive,
    inline_data(photo)]`; the persona lives in top-level `systemInstruction`
    (never a turn — the alternation rule from Phase 3); `generationConfig`
    uses `responseModalities: ["IMAGE"]` (no `responseSchema`).
    **No intermediate text step:** photo bytes + dossier text arrive in the
    same call; the model answers with only the image.
  - Directive text fuses the likeness with the dossier: the subject
    "resembles a {suggested_animal}" and the quirks come from the summary —
    the model is told to keep the person's facial features/composition/
    posture and translate it into the animal archetype (e.g. a "caffeinated
    badger" or a "meticulous beaver"), reflecting the dossier's behavioural
    traits.
  - Validation of the returned image before any send: non-empty; `mime_type`
    starts with `image/`; bytes ≤ `MAX_PHOTO_BYTES` (10 MB). Violations raise
    `ConverterError` (unusable model output).
  - Same secret guarantees as Phases 2–3: redacted `ConverterError` raised
    `from None`, adapter logs never contain the API key or dossier text.
- **Transport upload** (`transport.py`): `Transport` Protocol gains
  `send_photo(chat_id, image, *, media_type, caption=None)`. The
  `UrllibTransport` implementation builds a **multipart/form-data** body
  (single pure, unit-testable `_multipart(...)` helper: boundary,
  `chat_id`/`caption` text fields, one `photo` file part with a stable
  filename) and posts it through the existing `_read`/`_decode`
  normalisation, so token redaction and failure behaviour are identical to
  every other call.
- **Conversion routing** (`converter.py`): `handle_conversion` /
  `try_conversion`-style entry (mirrors the routing-in-the-stage-module
  pattern of the Interviewer):
  - teaser → `converter.hybridize(photo, dossier)` → `send_photo` (+ caption)
    → `mark_portrait_sent`; structured events for each step.
  - Degradation: hybridize failure or send failure → apology, loud log,
    **no** `portrait_sent` (retry on next update), nothing raises into the
    polling loop.
  - A `done` chat that is somehow missing its photo or dossier fails loud
    (logged) and degrades to a reset-and-reprompt — never a crash.
- **Bouncer dispatch** (`bouncer.py`): route `done && !portrait_sent` to the
  Converter; after a delegated interview update leaves the chat `done`
  (freshest dossier), run the same-update handoff; `done && portrait_sent`
  keeps the Interviewer's light reply.
- **Config** (`config.py`, `defaults.py`): `DEFAULT_CONVERTER_MODEL =
  gemini-3.1-flash-image`, `DEFAULT_CONVERTER_TIMEOUT = 120.0`,
  `MAX_PHOTO_BYTES = 10 * 1024 * 1024` in `defaults.py`;
  `get_converter_model()` (optional `GEMINI_CONVERTER_MODEL`) and
  `get_converter_timeout()` (optional `GEMINI_CONVERTER_TIMEOUT`),
  env-beats-file like the existing getters.
- **Wiring** (`polling.py`, `cli.py`): build one `GeminiConverter` from the
  resolved key/model/timeout; thread `converter` through the poll loop and
  handler signatures.
- **Structured logging**: events `conversion_started`, `conversion_succeeded`,
  `portrait_sent`, `conversion_failed`, `conversion_skipped`. Never log photo
  bytes, base64 payloads, or dossier text (user-derived; could echo secrets).

## Out of scope (YAGNI)

- Scripter and Narrator (Phases 5–6) — the portrait is delivered and the
  session keeps the dossier for them; nothing else is consumed here.
- Retry/backoff/timeout *policy* (Phase 7) — single-attempt graceful
  degradation is in scope, resilience engineering is not.
- Storing the generated portrait on the session (nothing consumes it yet; the
  dossier + photo stay for Phases 5–6).
- Parameterising the image (seed / aspect ratio / resolution / style knobs) —
  the directive is fixed, the model decides.
- Re-delivery of a portrait after `portrait_sent` (light reply only).
- Live-Telegram end-to-end automation (impractical here; documented as a
  manual smoke step for the user).

## Contracts at boundaries (typed, untrusted input)

| Boundary | Type | Failure behaviour |
| --- | --- | --- |
| Gemini `hybridize` input | photo `bytes` from session + typed `Dossier` | missing photo/dossier → fail loud + reset-reprompt |
| Gemini `hybridize` output | `inlineData` part; base64 → `GeneratedImage` (`bytes`, `image/*`, ≤ 10 MB) | malformed/missing/oversize/HTTP/network → `ConverterError`, apology, retry next update |
| `sendPhoto` upload | multipart body via `urllib` | `TelegramApiError` (redacted) → apology, no `portrait_sent`, retry next update |
| Session | `SessionState` v3 per `chat_id`, `portrait_sent: bool` | unknown chat → fresh default; wrong type → `ValidationError` |
| Telegram photo send | image bytes never exceed `MAX_PHOTO_BYTES` | oversize → `ConverterError` before any send |

`chat_id` stays `int` end-to-end. The portrait competes for nothing: one
generated image per interview, sent exactly once (flag is set only after a
successful `sendPhoto`).

## Non-negotiables

- **Per-`chat_id` isolation** — never leak one user's photo/dossier/portrait
  to another.
- **Never log or commit secrets** — the port, the shared client, and the
  transport keep the single-redaction guarantees for both the API key and the
  bot token.
- **No test contacts Telegram or Gemini** — fakes only (the explicitly opt-in
  live smoke in `validation.md` is the one exception, run by the verifier).
- **The original photo is never re-sent to Telegram** — only the generated
  hybrid is dispatched. The bot never echoes the user's raw portrait.
- **Fail loud, degrade gracefully, never wedge** — a portrait-less `done`
  chat is always retryable.

## Acceptance criteria (from ROADMAP Phase 4)

1. A new **hybrid portrait image arrives in chat**, derived from the user's
   original photo and the interview dossier, right after the dossier report
   (same handoff, no user input).
2. Degraded conversion (LLM failure, upload failure) apologises, leaves the
   session retryable, and a later update completes delivery.
3. `scripts/test` and `scripts/hooks` are green, including all 379 pre-
   existing tests.

## Delivered (filled at verification)

_To be completed by the verifier._