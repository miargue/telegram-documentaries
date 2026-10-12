"""The Phase 5 Scripter: a typed LLM port and its Gemini text adapter.

The Bouncer gates a portrait, the Interviewer builds the behavioural dossier,
the Converter fuses them into a hybrid portrait, and the Scripter closes the
observational arc: it narrates the subject's daily life as one dramatic,
playful British-wildlife-documentary paragraph — the raw narration script that
a later audio-synthesis phase (Phase 6, the Narrator) will read aloud. The
port (:class:`ScripterLLM`) is deliberately narrow and typed so the rest of
the pipeline never depends on a vendor SDK; the Gemini adapter is a
``generateContent`` JSON call over stdlib ``urllib`` with an injectable
``urlopen`` seam, exactly like the vision, interview and converter adapters.

The request body follows the Phase 3 alternating-role rule: the narrator
persona lives *only* in the top-level ``systemInstruction``, never in a turn,
and exactly one ``user`` content cites the dossier (the suggested animal, the
behavioural summary, and the animal-reason when present). Adapters generate a
raw script (JSON ``responseSchema`` ``{"script": str}``), never prose with
markdown, titles or preamble.

**Hard output contract** (the over-long/over-structured bug class): the
adapter strips the model output and rejects anything that is empty, breaks
into paragraphs (a blank line), or exceeds :data:`MAX_SCRIPT_WORDS`. Every
failure — network, HTTP, error payload, malformed or contract-violating output
— is normalised into a redacted :class:`ScriptError` raised with ``from None``
so the raw, key-bearing cause never surfaces in a formatted traceback.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol

from pydantic import BaseModel

from telegram_documentaries.defaults import (
    DEFAULT_SCRIPT_MODEL,
    MAX_SCRIPT_WORDS,
    MIN_SCRIPT_WORDS,
)
from telegram_documentaries.gemini import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT,
    GeminiError,
    GeminiJsonClient,
    UrlopenFn,
)
from telegram_documentaries.session import Dossier, Phase, SessionStore
from telegram_documentaries.transport import Transport, truncate_message

LOGGER = logging.getLogger(__name__)


class ScriptError(Exception):
    """A failed narration (network, HTTP, or unusable model output).

    Guarantee: the message never contains the API key — the shared Gemini
    client redacts before this class wraps the error, and it is always
    raised with ``from None``.
    """


class ScripterLLM(Protocol):
    """Port: turn a behavioural dossier into one spoken narration paragraph."""

    def write_script(self, dossier: Dossier) -> str:
        """Return the plain one-paragraph narration for ``dossier``."""
        ...


# The narrator is the *voice*, so the persona lives only in
# ``systemInstruction`` — never in a turn (the Phase 3 alternating-role
# rule). The directive carries the per-subject instructions and the dossier
# text.
SCRIPT_PERSONA = (
    "You are a revered, dramatic British wildlife documentary narrator "
    "treating this subject's daily quirks — sleeping eighteen hours, hoarding "
    "shiny trinkets, guarding territory, raiding the larder — as "
    "extraordinary wildlife behaviour. Present the subject's routine with "
    "affection and deadpan awe, as though narrating an Attenborough special. "
    "Reply with exactly one paragraph of 60 to 90 words, entirely plain "
    "prose: no markdown, no titles, no preamble, no stage directions. The "
    "narration must read naturally aloud, ready for a voice synthesiser."
)

# ``reason`` formats to a sentence when the dossier carries an animal_reason
# and to an empty string otherwise, so the directive stays grammatical either
# way.
SCRIPT_DIRECTIVE = (
    "Narrate the dramatic daily life of this {animal}, whose behavioural "
    "dossier says: {summary}.{reason} Write one playful, awe-struck paragraph "
    "of {min_words} to {max_words} words presenting these habits as the "
    "extraordinary wildlife of a quietly magnificent creature. Reply only "
    "with the narration text, ready for a voice synthesiser."
)

SCRIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "script": {
            "type": "string",
        },
    },
    "required": ["script"],
}

# A paragraph break is two line endings with nothing but horizontal whitespace
# (or a stray carriage return) between them — covering whitespace-only "blank"
# lines and CRLF output. A single soft line break within a paragraph is fine.
_BLANK_LINE = re.compile(r"\n[ \t\r]*\n")


class _Script(BaseModel):
    script: str


def word_count(text: str) -> int:
    """Count words by whitespace-splitting, the same rule as the contract.

    Hyphenated compounds and unspaced emoji runs each count as one token.
    """
    return len(text.split())


class GeminiScripter:
    """A Gemini ``generateContent`` adapter for :class:`ScripterLLM`.

    ``urlopen`` is injectable so tests never contact Gemini. All failure
    normalisation and API-key redaction is delegated to the shared
    :class:`GeminiJsonClient`; every Gemini failure is wrapped into a redacted
    :class:`ScriptError` raised with ``from None``. The adapter then enforces
    the Phase 5 output contract (non-empty, no paragraph break, ≤
    :data:`MAX_SCRIPT_WORDS` words) on the raw model text.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_SCRIPT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        urlopen: UrlopenFn | None = None,
    ) -> None:
        self._gemini = GeminiJsonClient(
            api_key,
            model=model,
            base_url=base_url,
            timeout=timeout,
            urlopen=urlopen,
            logger=LOGGER,
        )

    def write_script(self, dossier: Dossier) -> str:
        """Narrate the dossier, enforcing the output contract.

        The model's output is accepted only if it strips down to a non-empty
        single paragraph of at most :data:`MAX_SCRIPT_WORDS` words; anything
        else (and any transport failure) raises :class:`ScriptError` so the
        caller's degrade/retry path can apologise and try again on the next
        update.
        """
        try:
            payload = self._gemini.generate_json(
                self._request_body(dossier), _Script
            )
        except GeminiError as exc:
            raise ScriptError(str(exc)) from None
        return self._validate(payload["script"])

    def _request_body(self, dossier: Dossier) -> dict[str, Any]:
        """Build the single ``generateContent`` body Google will accept.

        Exactly one ``user`` content carrying the directive and the dossier
        text; the persona is top-level only.
        """
        return {
            "systemInstruction": {"parts": [{"text": SCRIPT_PERSONA}]},
            "contents": [
                {"role": "user", "parts": [{"text": self._directive(dossier)}]}
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": SCRIPT_SCHEMA,
            },
        }

    def _directive(self, dossier: Dossier) -> str:
        reason = ""
        if dossier.animal_reason:
            reason = (
                f" The subject matches this animal because "
                f"{dossier.animal_reason}."
            )
        return SCRIPT_DIRECTIVE.format(
            animal=dossier.suggested_animal,
            summary=dossier.summary,
            reason=reason,
            min_words=MIN_SCRIPT_WORDS,
            max_words=MAX_SCRIPT_WORDS,
        )

    def _validate(self, script: str) -> str:
        """Enforce the raw-script contract on the model's output."""
        text = script.strip()
        if not text:
            raise ScriptError("model returned an empty script") from None
        if word_count(text) > MAX_SCRIPT_WORDS:
            raise ScriptError(
                f"model returned an over-long script "
                f"({word_count(text)} words)"
            ) from None
        if _BLANK_LINE.search(text):
            raise ScriptError(
                "model returned a script with a paragraph break"
            ) from None
        return text


# --------------------------------------------------------------------------- routing
#
# ``handle_scripting`` is the Scripter's whole turn: for a ``done`` chat whose
# portrait already reached it, generate the narration, send it, then set the
# terminal flag. Failure policy mirrors the Converter and Interviewer: nothing
# raises into the polling loop (except the process-control pair), every
# degraded path leaves the chat retryable (the flag is never set), and the
# raw script text is never logged.

SCRIPT_APOLOGY_REPLY = (
    "Sorry, my narrator went quiet for a moment. Send me anything and I'll "
    "have another go."
)

# Sent when a ``done`` chat is somehow missing the dossier the narration
# needs: loud in the logs, gentle in the chat, and the session is reset so
# the user can start over instead of being wedged.
SCRIPT_RESET_REPLY = (
    "Something got lost on my page, so let's start fresh - send me a portrait "
    "photo and I'll take it from the top."
)


def _send(
    transport: Transport, chat_id: int, text: str, log: logging.Logger
) -> bool:
    """Send one message; degrade a failure to ``False`` with a loud log.

    The text is bounded at this chokepoint for the same reason as every other
    outbound path (the over-long outbound-text bug class).
    """
    try:
        transport.call(
            "sendMessage",
            {"chat_id": chat_id, "text": truncate_message(text)},
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        log.error(
            "reply failed",
            extra={
                "event": "reply_failed",
                "chat_id": chat_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
            exc_info=True,
        )
        return False
    return True


def _script_failed(
    chat_id: int, reason: str, log: logging.Logger, *, with_traceback: bool = False
) -> None:
    """Log a scripting failure; the reason never carries user text."""
    log.error(
        "scripting failed",
        extra={
            "event": "script_failed",
            "chat_id": chat_id,
            "error": reason,
        },
        exc_info=with_traceback,
    )


def handle_scripting(
    transport: Transport,
    chat_id: int,
    *,
    session: SessionStore,
    scripter: ScripterLLM,
    logger: logging.Logger | None = None,
) -> bool:
    """Generate and deliver one narration script for a ``done`` chat.

    Returns ``True`` only when the script was generated (or re-sent) *and*
    delivered *and* the session flag recorded it. Any other outcome (wrong
    phase, portrait not yet sent, already delivered, missing dossier,
    generation failure, send failure) returns ``False`` with the session left
    retryable — a later update re-attempts the scripting — and never raises.

    A stored-but-undelivered script is re-sent exactly as stored with *no* LLM
    call (the Interviewer's re-ask pattern): the user can never be shown two
    different narrations of the same subject, and a failed delivery costs no
    model round-trip.
    """
    log = LOGGER if logger is None else logger
    state = session.get(chat_id)

    if state.phase is not Phase.done or state.portrait_sent is not True:
        log.info(
            "scripting skipped: chat is not awaiting a narration",
            extra={
                "event": "script_skipped",
                "chat_id": chat_id,
                "phase": state.phase.value,
                "portrait_sent": state.portrait_sent,
            },
        )
        return False
    if state.script_sent:
        log.info(
            "scripting skipped: script already delivered",
            extra={
                "event": "script_skipped",
                "chat_id": chat_id,
                "phase": state.phase.value,
                "portrait_sent": state.portrait_sent,
                "script_sent": state.script_sent,
            },
        )
        return False

    stored = state.script
    if stored is not None:
        # Delivery of a previously generated script failed; re-send the exact
        # stored text — never re-generate.
        log.debug(
            "re-sending stored script",
            extra={
                "event": "script_resend",
                "chat_id": chat_id,
                # Only the shape is ever logged: never the stored script text.
                "word_count": word_count(stored),
            },
        )
        if not _send(transport, chat_id, stored, log):
            _script_failed(chat_id, "reply_failed: stored script", log)
            # Apologise on a best-effort basis (a failure here must not mask
            # the already-degraded outcome) and stay retryable.
            _send(transport, chat_id, SCRIPT_APOLOGY_REPLY, log)
            return False
        session.mark_script_sent(chat_id)
        log.info(
            "script sent",
            extra={
                "event": "script_sent",
                "chat_id": chat_id,
                "word_count": word_count(stored),
            },
        )
        return True

    dossier = state.dossier
    if dossier is None:
        # Invariant break: ``done`` implies the dossier was retained. Fail
        # loud and let the user start over rather than wedge the chat.
        _script_failed(
            chat_id,
            "ValueError: done chat is missing its dossier",
            log,
        )
        session.reset(chat_id)
        _send(transport, chat_id, SCRIPT_RESET_REPLY, log)
        return False

    log.info(
        "scripting started",
        extra={"event": "script_started", "chat_id": chat_id},
    )
    try:
        script = scripter.write_script(dossier)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        _script_failed(
            chat_id, f"{type(exc).__name__}: {exc}", log, with_traceback=True
        )
        _send(transport, chat_id, SCRIPT_APOLOGY_REPLY, log)
        return False

    log.info(
        "script ready",
        extra={
            "event": "script_ready",
            "chat_id": chat_id,
            # Only the shape is ever logged: length (word count) and the
            # suggested animal — never any script or dossier text.
            "word_count": word_count(script),
            "suggested_animal": dossier.suggested_animal,
        },
    )
    # Capture the script before the send attempt: a failed delivery re-sends
    # this exact text on the next update (no re-generation, no drift).
    session.store_script(chat_id, script)
    if not _send(transport, chat_id, script, log):
        _script_failed(chat_id, "reply_failed: script", log)
        _send(transport, chat_id, SCRIPT_APOLOGY_REPLY, log)
        return False

    # Flag only after the script actually reached the chat: it is evidence of
    # delivery, not intent, and makes a failed send retryable.
    session.mark_script_sent(chat_id)
    log.info(
        "script sent",
        extra={
            "event": "script_sent",
            "chat_id": chat_id,
            "word_count": word_count(script),
        },
    )
    return True
