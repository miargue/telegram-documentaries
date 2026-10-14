"""The Phase 6 Narrator: a typed LLM port and its Gemini TTS adapter.

The Bouncer gates a portrait, the Interviewer builds the behavioural dossier,
the Converter fuses them into a hybrid portrait, the Scripter writes the
narration script, and the Narrator renders it as audio — turning the stored
script into a playable voice note in the chat. The port (:class:`NarratorLLM`)
is deliberately narrow and typed so the rest of the pipeline never depends on
a vendor SDK; the Gemini adapter is a ``generateContent`` JSON call over
stdlib ``urllib`` with an injectable ``urlopen`` seam, exactly like the other
adapters.

The request body follows the spec: one ``user`` content holding two text parts
— the voice-over guidance (the posh-British deep-voice persona) and the raw
script verbatim — and ``generationConfig: {"responseModalities": ["AUDIO"]}``.
"""

from __future__ import annotations

import logging
from typing import Protocol

from telegram_documentaries.defaults import (
    DEFAULT_NARRATOR_MODEL,
    DEFAULT_NARRATOR_TIMEOUT,
    DEFAULT_NARRATOR_VOICE,
    MAX_NARRATION_BYTES,
)
from telegram_documentaries.gemini import (
    DEFAULT_BASE_URL,
    AudioPayload,
    GeminiError,
    GeminiJsonClient,
    UrlopenFn,
)
from telegram_documentaries.session import Phase, SessionStore
from telegram_documentaries.transport import Transport

LOGGER = logging.getLogger(__name__)


class NarratorError(Exception):
    """A failed narration synthesis (network, HTTP, or unusable model output).

    Guarantee: the message never contains the API key — the shared Gemini
    client redacts before this class wraps the error, and it is always
    raised with ``from None``.
    """


class NarratorLLM(Protocol):
    """Port: turn a narration script into audio."""

    def synthesize(self, script: str) -> AudioPayload:
        """Return the audio payload for ``script``."""
        ...


class GeminiNarrator:
    """A Gemini ``generateContent`` adapter for :class:`NarratorLLM`.

    ``urlopen`` is injectable so tests never contact Gemini. All failure
    normalisation and API-key redaction is delegated to the shared
    :class:`GeminiJsonClient`; every Gemini failure is wrapped into a redacted
    :class:`NarratorError` raised with ``from None``.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_NARRATOR_MODEL,
        voice: str = DEFAULT_NARRATOR_VOICE,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_NARRATOR_TIMEOUT,
        urlopen: UrlopenFn | None = None,
    ) -> None:
        self._voice = voice
        self._gemini = GeminiJsonClient(
            api_key,
            model=model,
            base_url=base_url,
            timeout=timeout,
            urlopen=urlopen,
            logger=LOGGER,
        )

    def synthesize(self, script: str) -> AudioPayload:
        """Synthesise audio for the script, enforcing output contract."""
        try:
            audio = self._gemini.generate_audio(self._request_body(script))
        except GeminiError as exc:
            raise NarratorError(str(exc)) from None
        return validate_audio(audio)

    def _request_body(self, script: str) -> dict:
        return {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": self._voice},
                        {"text": script},
                    ],
                }
            ],
            "generationConfig": {"responseModalities": ["AUDIO"]},
        }


def validate_audio(audio: AudioPayload) -> AudioPayload:
    """Enforce the model-output contract on a synthesised payload.

    The audio must be a non-empty ``audio/ogg`` payload (case-insensitively)
    no larger than :data:`MAX_NARRATION_BYTES`; any violation is an unusable
    output, raised as a redacted :class:`NarratorError` (``from None``). Used by
    the adapter and re-checked by the routing so a port that returns junk still
    degrades.

    Deviation from spec (recorded): the plan states ``audio/*``; the locked
    format decision is OGG, so we require the OGG subtype explicitly.
    """
    mime = audio.mime_type or ""
    if (
        not mime.lower().startswith("audio/ogg")
        or not audio.data
        or len(audio.data) > MAX_NARRATION_BYTES
    ):
        raise NarratorError("model returned an unusable audio payload") from None
    return audio


_OGG_MEDIA_TYPE = "audio/ogg"

# --------------------------------------------------------------------------- routing

NARRATION_APOLOGY_REPLY = (
    "Sorry, my narrator went quiet for a moment. Send me anything and I'll "
    "have another go."
)

NARRATION_RESET_REPLY = (
    "Something got lost on my page, so let's start fresh - send me a portrait "
    "photo and I'll take it from the top."
)


def _send(
    transport: Transport, chat_id: int, text: str, log: logging.Logger
) -> bool:
    try:
        from telegram_documentaries.transport import truncate_message

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


def _narrator_failed(
    chat_id: int, reason: str, log: logging.Logger, *, with_traceback: bool = False
) -> None:
    log.error(
        "narrator failed",
        extra={
            "event": "narrator_failed",
            "chat_id": chat_id,
            "error": reason,
        },
        exc_info=with_traceback,
    )


def handle_narrating(
    transport: Transport,
    chat_id: int,
    *,
    session: SessionStore,
    narrator: NarratorLLM,
    logger: logging.Logger | None = None,
) -> bool:
    log = LOGGER if logger is None else logger
    state = session.get(chat_id)

    if state.phase is not Phase.done or state.script_sent is not True:
        log.info(
            "narration skipped: chat is not awaiting narration",
            extra={
                "event": "narration_skipped",
                "chat_id": chat_id,
                "phase": state.phase.value,
                "script_sent": state.script_sent,
            },
        )
        return False

    if state.narrator_sent:
        log.info(
            "narration skipped: narrator already sent",
            extra={
                "event": "narration_skipped",
                "chat_id": chat_id,
                "phase": state.phase.value,
                "script_sent": state.script_sent,
                "narrator_sent": state.narrator_sent,
            },
        )
        return False

    if state.narration is not None:
        # Re-send stored bytes
        try:
            transport.send_voice(
                chat_id,
                state.narration,
                media_type=_OGG_MEDIA_TYPE,
                filename="narration.ogg",
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            _narrator_failed(
                chat_id, f"{type(exc).__name__}: {exc}", log, with_traceback=True
            )
            _send(transport, chat_id, NARRATION_APOLOGY_REPLY, log)
            return False
        session.mark_narrator_sent(chat_id)
        log.info(
            "narration sent",
            extra={
                "event": "narration_sent",
                "chat_id": chat_id,
                "byte_count": len(state.narration),
                "mime": _OGG_MEDIA_TYPE,
            },
        )
        return True

    script = state.script
    if script is None:
        _narrator_failed(
            chat_id,
            "ValueError: done chat is missing its script",
            log,
        )
        session.reset(chat_id)
        _send(transport, chat_id, NARRATION_RESET_REPLY, log)
        return False

    log.info(
        "narration started",
        extra={"event": "narration_started", "chat_id": chat_id},
    )
    try:
        audio = validate_audio(narrator.synthesize(script))
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        _narrator_failed(
            chat_id, f"{type(exc).__name__}: {exc}", log, with_traceback=True
        )
        _send(transport, chat_id, NARRATION_APOLOGY_REPLY, log)
        return False

    # Every send and every log reports the clean locked-format media type, never
    # the model's decorated value (e.g. ``audio/ogg; codecs=opus``).
    normalised_mime = _OGG_MEDIA_TYPE
    log.info(
        "narrator ready",
        extra={
            "event": "narrator_ready",
            "chat_id": chat_id,
            "byte_count": len(audio.data),
            "mime": normalised_mime,
        },
    )

    session.store_narration(chat_id, audio.data)

    try:
        transport.send_voice(
            chat_id,
            audio.data,
            media_type=normalised_mime,
            filename="narration.ogg",
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        _narrator_failed(
            chat_id, f"{type(exc).__name__}: {exc}", log, with_traceback=True
        )
        _send(transport, chat_id, NARRATION_APOLOGY_REPLY, log)
        return False

    session.mark_narrator_sent(chat_id)
    log.info(
        "narration sent",
        extra={
            "event": "narration_sent",
            "chat_id": chat_id,
            "byte_count": len(audio.data),
            "mime": normalised_mime,
        },
    )
    return True
