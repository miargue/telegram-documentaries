"""The Phase 6 Narrator: a typed LLM port and its Gemini TTS adapter.

The Bouncer gates a portrait, the Interviewer builds the behavioural dossier,
the Converter fuses them into a hybrid portrait, the Scripter writes the
narration script, and the Narrator renders it as audio — turning the stored
script into a playable MP3 audio message in the chat. The port
(:class:`NarratorLLM`) is deliberately narrow and typed so the rest of the
pipeline never depends on a vendor SDK; the Gemini adapter is a Gemini
**Interactions API** JSON call over stdlib ``urllib`` with an injectable
``urlopen`` seam, exactly like the other adapters.

The request body follows the spec: the model travels in the body, the raw
script is a single ``user_input`` text content, and the fixed delivery style
is carried as a ``speech_metadata`` annotation — never prepended text, which
Gemini's TTS would read aloud. Audio is requested at ``audio/mp3`` with the
prebuilt ``speech_config`` voice.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from telegram_documentaries.defaults import (
    DEFAULT_NARRATOR_MODEL,
    DEFAULT_NARRATOR_STYLE,
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
from telegram_documentaries.transport import AUDIO_FILENAME, Transport

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
    """A Gemini Interactions API adapter for :class:`NarratorLLM`.

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
        style: str = DEFAULT_NARRATOR_STYLE,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_NARRATOR_TIMEOUT,
        urlopen: UrlopenFn | None = None,
    ) -> None:
        self._model = model
        self._voice = voice
        self._style = style
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
            audio = self._gemini.generate_interaction_audio(
                self._request_body(script)
            )
        except GeminiError as exc:
            raise NarratorError(str(exc)) from None
        return validate_audio(audio)

    def _request_body(self, script: str) -> dict[str, Any]:
        return {
            "model": self._model,
            "input": [
                {
                    "type": "user_input",
                    "content": [
                        {
                            "type": "text",
                            "text": script,
                            "annotations": [
                                {"type": "speech_metadata", "style": self._style}
                            ],
                        }
                    ],
                }
            ],
            "response_format": {"type": "audio", "mime_type": "audio/mp3"},
            "generation_config": {
                "speech_config": [{"voice": self._voice}]
            },
        }


# The locked output format for a narration: a clean MP3 media type, never the
# model's decorated value (e.g. ``audio/mp3; rate=24000; channels=1``).
AUDIO_MEDIA_TYPE = "audio/mpeg"

# Accepted model-declared MP3-family mime prefixes (case-insensitive). Both
# spellings are in the wild; anything else (OGG, WAV, ...) is unusable output.
_MP3_MIME_PREFIXES = ("audio/mp3", "audio/mpeg")


def validate_audio(audio: AudioPayload) -> AudioPayload:
    """Enforce the model-output contract on a synthesised payload.

    The audio must be a non-empty payload whose media type is in the MP3 family
    — ``audio/mp3*`` or ``audio/mpeg*`` (case-insensitively) — and no larger
    than :data:`MAX_NARRATION_BYTES`; any violation is an unusable output,
    raised as a redacted :class:`NarratorError` (``from None``). Used by the
    adapter and re-checked by the routing so a port that returns junk still
    degrades.
    """
    mime = (audio.mime_type or "").lower()
    if (
        not mime.startswith(_MP3_MIME_PREFIXES)
        or not audio.data
        or len(audio.data) > MAX_NARRATION_BYTES
    ):
        raise NarratorError("model returned an unusable audio payload") from None
    return audio

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
            transport.send_audio(
                chat_id,
                state.narration,
                media_type=AUDIO_MEDIA_TYPE,
                filename=AUDIO_FILENAME,
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
                "mime": AUDIO_MEDIA_TYPE,
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
        session.purge(chat_id)
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
    # the model's decorated value (e.g. ``audio/mp3; rate=24000; channels=1``).
    normalised_mime = AUDIO_MEDIA_TYPE
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
        transport.send_audio(
            chat_id,
            audio.data,
            media_type=normalised_mime,
            filename=AUDIO_FILENAME,
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
