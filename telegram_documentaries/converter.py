"""The Phase 4 Converter: a typed LLM port and its Gemini image adapter.

The Bouncer gates a portrait, the Interviewer builds the behavioural dossier,
and the Converter closes the loop: it fuses the two into a single hybrid animal
portrait and delivers it to the chat. The port (:class:`ConverterLLM`) is
deliberately narrow and typed so the rest of the pipeline never depends on a
vendor SDK; the Gemini adapter is a ``generateContent`` image call over stdlib
``urllib`` with an injectable ``urlopen`` seam, exactly like the vision and
interview adapters — no third-party HTTP dependencies.

**One multimodal call, no intermediate text step** (per the Phase 4
requirements): the original photo arrives as an inline image part and the
dossier as text in the *same* ``user`` turn, the persona lives only in the
top-level ``systemInstruction``, and ``generationConfig`` asks for image
modalities only (``responseModalities: ["IMAGE"]``, no ``responseSchema``).

Every failure — network, HTTP, malformed or unusable model output (empty,
non-image mime type, larger than ``MAX_PHOTO_BYTES``) — is normalised into a
redacted :class:`ConverterError` raised with ``from None``, so the raw,
key-bearing cause never surfaces in a formatted traceback and user-derived text
(the dossier) is never logged.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Protocol

from pydantic import BaseModel

from telegram_documentaries.defaults import (
    DEFAULT_CONVERTER_MODEL,
    DEFAULT_CONVERTER_TIMEOUT,
    MAX_PHOTO_BYTES,
)
from telegram_documentaries.gemini import (
    DEFAULT_BASE_URL,
    GeminiError,
    GeminiJsonClient,
    InlineImagePayload,
    UrlopenFn,
)
from telegram_documentaries.session import Dossier, Phase, SessionStore
from telegram_documentaries.transport import Transport, truncate_message

LOGGER = logging.getLogger(__name__)


class ConverterError(Exception):
    """A failed hybrid-portrait generation (network, HTTP or unusable output).

    Guarantee: the message never contains the API key (the shared Gemini client
    redacts before this class wraps the error) and it is always raised with
    ``from None``.
    """


class GeneratedImage(BaseModel):
    """The validated image the Converter will deliver to the chat."""

    data: bytes
    mime_type: str


class ConverterLLM(Protocol):
    """Port: fuse an original photo and a dossier into a hybrid portrait."""

    def hybridize(
        self,
        photo: bytes,
        dossier: Dossier,
        *,
        media_type: str = "image/jpeg",
    ) -> GeneratedImage:
        """Return the generated hybrid animal portrait for ``photo``."""
        ...


# The persona is the *artist*, so it lives only in ``systemInstruction`` — never
# in a turn (the Phase 3 alternating-role rule). The directive carries the
# per-subject instructions and the dossier text.
CONVERTER_PERSONA = (
    "You are the portrait artist for a comedy-wildlife documentary. You are "
    "given one real photo of a subject and that subject's behavioural dossier. "
    "Fuse the two into a single believable hybrid animal portrait: the "
    "subject's likeness reimagined as the suggested animal. Reply only with "
    "the finished image."
)

HYBRIDIZE_DIRECTIVE = (
    "Create one hybrid portrait photograph that fuses the subject's likeness "
    "with a {animal}. Keep the subject's facial features, pose, framing and "
    "lighting from the attached photo and translate them into the animal "
    "archetype. Let these behavioural traits from the dossier shine through: "
    "{summary} Reply with only the finished portrait image."
)


class GeminiConverter:
    """A Gemini image adapter for :class:`ConverterLLM`.

    ``urlopen`` is injectable so tests never contact Gemini. Transport failure
    normalisation and API-key redaction are delegated to the shared
    :class:`GeminiJsonClient` (via its image capability); the adapter validates
    the returned image and wraps every failure into a redacted
    :class:`ConverterError` raised with ``from None``.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_CONVERTER_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_CONVERTER_TIMEOUT,
        urlopen: UrlopenFn | None = None,
    ) -> None:
        self._gemini = GeminiJsonClient(
            api_key,
            model=model,
            base_url=base_url,
            timeout=timeout,
            urlopen=urlopen,
        )

    def hybridize(
        self,
        photo: bytes,
        dossier: Dossier,
        *,
        media_type: str = "image/jpeg",
    ) -> GeneratedImage:
        """Generate the hybrid portrait, validating the model's image.

        The model's output is accepted only if it is a non-empty ``image/*``
        payload no larger than :data:`MAX_PHOTO_BYTES`; anything else (and any
        transport failure) raises :class:`ConverterError` so the caller's
        degrade/retry path can apologise and try again on the next update.

        The ``image/jpeg`` default is deliberate: the upload pipeline does not
        retain the original photo's real format (Telegram repackages
        ``sendPhoto`` deliveries as JPEG), so the adapter labels the source
        part as JPEG. A future phase that keeps raw uploads must pass the true
        ``media_type`` rather than relying on this default.
        """
        try:
            payload = self._gemini.generate_image(
                self._request_body(photo, dossier, media_type)
            )
        except GeminiError as exc:
            raise ConverterError(str(exc)) from None
        return self._validate(payload)

    def _request_body(
        self, photo: bytes, dossier: Dossier, media_type: str
    ) -> dict[str, Any]:
        """Build the single-multimodal-call ``generateContent`` body.

        Exactly one ``user`` content: the directive text followed by the photo
        as an ``inline_data`` part. The persona is top-level only, and the
        generation config requests image output with no text schema.
        """
        directive = HYBRIDIZE_DIRECTIVE.format(
            animal=dossier.suggested_animal, summary=dossier.summary
        )
        return {
            "systemInstruction": {"parts": [{"text": CONVERTER_PERSONA}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": directive},
                        {
                            "inline_data": {
                                "mime_type": media_type,
                                "data": base64.b64encode(photo).decode("ascii"),
                            }
                        },
                    ],
                }
            ],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }

    def _validate(self, payload: InlineImagePayload) -> GeneratedImage:
        if not payload.data:
            raise ConverterError("model returned an empty image") from None
        if not payload.mime_type.startswith("image/"):
            raise ConverterError(
                f"model returned a non-image media type: {payload.mime_type!r}"
            ) from None
        if len(payload.data) > MAX_PHOTO_BYTES:
            raise ConverterError(
                f"model returned an oversized image ({len(payload.data)} bytes)"
            ) from None
        return GeneratedImage(data=payload.data, mime_type=payload.mime_type)


# --------------------------------------------------------------------------- routing
#
# ``handle_conversion`` is the Converter's whole turn: a short teaser so the
# chat is never silent during the slow image call, the single multimodal
# generation, one ``sendPhoto`` and then the terminal flag. Failure policy
# mirrors the Interviewer: nothing raises into the polling loop, every degraded
# path leaves the chat retryable (the flag is never set), and user-derived text
# (the dossier) is never logged.

TEASER_REPLY = (
    "Painting your portrait... one moment, this takes a little while."
)

APOLOGY_REPLY = (
    "Sorry, my brushes slipped and your portrait didn't come out. Send me "
    "anything and I'll have another go."
)

# Sent when a ``done`` chat is somehow missing the photo or dossier the
# conversion needs: loud in the logs, gentle in the chat, and the session is
# reset so the user can start over instead of being wedged.
RESET_REPLY = (
    "Something got lost on my easel, so let's start fresh - send me a portrait "
    "photo and I'll take it from the top."
)

# Small caption on the delivered portrait (Telegram photos allow long captions;
# this stays deliberately tiny). Only the model-suggested animal goes in — never
# any user-derived dossier text.
PORTRAIT_CAPTION = (
    "Meet your hybrid self: a {animal}. Only slightly wilder than the original."
)


def _send_message(
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


def _conversion_failed(
    chat_id: int, reason: str, log: logging.Logger, *, with_traceback: bool = False
) -> None:
    """Log a conversion failure; the reason never carries user text."""
    log.error(
        "conversion failed",
        extra={
            "event": "conversion_failed",
            "chat_id": chat_id,
            "error": reason,
        },
        exc_info=with_traceback,
    )


def handle_conversion(
    transport: Transport,
    chat_id: int,
    *,
    session: SessionStore,
    converter: ConverterLLM,
    logger: logging.Logger | None = None,
) -> bool:
    """Generate and deliver one hybrid portrait for a ``done`` chat.

    Returns ``True`` only when the portrait was generated *and* uploaded *and*
    the session flag recorded it. Any other outcome (wrong phase, already sent,
    missing artifacts, generation failure, upload failure) returns ``False``
    with the session left retryable — a later update re-attempts the conversion
    — and never raises.
    """
    log = LOGGER if logger is None else logger
    state = session.get(chat_id)
    if state.phase is not Phase.done or state.portrait_sent:
        log.info(
            "conversion skipped: chat is not awaiting a portrait",
            extra={
                "event": "conversion_skipped",
                "chat_id": chat_id,
                "phase": state.phase.value,
                "portrait_sent": state.portrait_sent,
            },
        )
        return False

    photo = state.photo
    dossier = state.dossier
    if photo is None or dossier is None:
        # Invariant break: ``done`` implies both artifacts were retained. Fail
        # loud and let the user start over rather than wedge the chat.
        _conversion_failed(
            chat_id,
            "ValueError: done chat is missing its photo or dossier",
            log,
        )
        session.reset(chat_id)
        _send_message(transport, chat_id, RESET_REPLY, log)
        return False

    log.info(
        "conversion started",
        extra={"event": "conversion_started", "chat_id": chat_id},
    )
    if not _send_message(transport, chat_id, TEASER_REPLY, log):
        # Could not even say we are working: stay retryable. A best-effort
        # apology is attempted in case only the first send was lost.
        _conversion_failed(chat_id, "reply_failed: teaser", log)
        _send_message(transport, chat_id, APOLOGY_REPLY, log)
        return False

    try:
        # The photo's real format is not retained by the upload pipeline, so
        # the adapter's deliberate JPEG default is used here (see ``hybridize``).
        image = converter.hybridize(photo, dossier)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        _conversion_failed(
            chat_id, f"{type(exc).__name__}: {exc}", log, with_traceback=True
        )
        _send_message(transport, chat_id, APOLOGY_REPLY, log)
        return False

    log.info(
        "conversion succeeded",
        extra={
            "event": "conversion_succeeded",
            "chat_id": chat_id,
            "mime_type": image.mime_type,
        },
    )
    caption = PORTRAIT_CAPTION.format(animal=dossier.suggested_animal)
    try:
        transport.send_photo(
            chat_id,
            image.data,
            media_type=image.mime_type,
            caption=caption,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        _conversion_failed(
            chat_id, f"{type(exc).__name__}: {exc}", log, with_traceback=True
        )
        _send_message(transport, chat_id, APOLOGY_REPLY, log)
        return False

    # Flag only after the portrait actually reached the chat: it is evidence of
    # delivery, not intent, and makes a failed upload retryable.
    session.mark_portrait_sent(chat_id)
    log.info(
        "portrait sent",
        extra={"event": "portrait_sent", "chat_id": chat_id},
    )
    return True
