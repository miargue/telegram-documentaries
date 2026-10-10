"""The Phase 2 Bouncer: the vision gate in front of the pipeline.

Routes each incoming Telegram message:

- ``/start`` / ``/restart`` → reset the session and ask for a portrait.
- plain text or any non-photo content → re-prompt (never crash, never lose the
  session).
- a photo → download its largest resolution and ask the :class:`VisionGate`
  whether a human is present. A human advances the chat to ``gate_passed`` and
  keeps the photo; anything else gets a cheeky rejection and a session reset.

Every download/gate/send failure is caught, logged loudly with a traceback and
degraded to an apology/re-prompt — nothing is allowed to raise into the polling
loop (``SPECS/TECH.md`` §Logging & error policy).
"""

from __future__ import annotations

import logging

from telegram_documentaries.logging_config import SKIPPED, Skipped, log_lifecycle
from telegram_documentaries.media import fetch_photo, largest_photo
from telegram_documentaries.models import Message, Update
from telegram_documentaries.session import SessionStore
from telegram_documentaries.transport import Transport
from telegram_documentaries.vision import VisionGate

LOGGER = logging.getLogger(__name__)

PROMPT_PHOTO = (
    "Welcome to The Telegram Documentaries! Send me a portrait photo and "
    "I'll check there's a human in it before we start."
)
SUCCESS_REPLY = (
    "Nice one - you're clearly human. Hold tight, the interview is coming."
)
REJECTION_REPLY = (
    "Hmm, I can't see a human there - and I only interview people, not pets, "
    "food or landscapes. Send me a portrait and let's try again."
)
APOLOGY_REPLY = (
    "Sorry, something went wrong looking at that photo. Send it again and "
    "I'll give it another go."
)

_START_COMMANDS = frozenset({"/start", "/restart"})


@log_lifecycle(LOGGER, "handle_update")
def handle_update(
    update: Update,
    transport: Transport,
    *,
    session: SessionStore,
    gate: VisionGate,
    logger: logging.Logger | None = None,
) -> bool | Skipped:
    """Route one update; return whether a reply was sent.

    ``session`` and ``gate`` are injected so the caller owns their lifetime
    (the CLI builds one store and one gate for the process). A deliberate,
    benign no-op returns :data:`SKIPPED` (so ``log_lifecycle`` logs it as
    ``skipped`` rather than a false ``degraded`` warning); a genuine reply
    failure returns ``False``.
    """
    log = LOGGER if logger is None else logger
    message = update.message
    if message is None:
        log.info(
            "update without message ignored",
            extra={"event": "update_skipped", "update_id": update.update_id},
        )
        return SKIPPED

    chat_id = message.chat.id
    text = (message.text or "").strip()

    if _is_start_command(text):
        session.reset(chat_id)
        return _prompt(transport, chat_id, update, log, reason="command")
    if text:
        return _prompt(transport, chat_id, update, log, reason="text")
    if message.photo:
        return _handle_photo(
            message, transport, chat_id=chat_id, update=update,
            session=session, gate=gate, log=log,
        )
    return _prompt(transport, chat_id, update, log, reason="unsupported_content")


def _is_start_command(text: str) -> bool:
    if not text.startswith("/"):
        return False
    command = text.split(maxsplit=1)[0].split("@", 1)[0]
    return command in _START_COMMANDS


def _handle_photo(
    message: Message,
    transport: Transport,
    *,
    chat_id: int,
    update: Update,
    session: SessionStore,
    gate: VisionGate,
    log: logging.Logger,
) -> bool | Skipped:
    """Gate the first photo of an album; ignore the album's other members.

    Telegram delivers each photo of a single send as its own update sharing a
    ``media_group_id``. Classifying every member would waste calls and could
    produce contradictory verdicts, so only the first is processed and the id is
    remembered once a verdict has been recorded (see ``remember_media_group``).
    An ignored follow-up returns :data:`SKIPPED`, not ``False``.
    """
    media_group_id = message.media_group_id
    if (
        media_group_id is not None
        and session.get(chat_id).last_media_group_id == media_group_id
    ):
        log.info(
            "album follow-up ignored",
            extra={
                "event": "bouncer_album_skipped",
                "update_id": update.update_id,
                "chat_id": chat_id,
                "media_group_id": media_group_id,
            },
        )
        return SKIPPED

    sent = _process_photo(
        message,
        transport,
        chat_id=chat_id,
        update=update,
        session=session,
        gate=gate,
        log=log,
    )
    if media_group_id is not None:
        session.remember_media_group(chat_id, media_group_id)
    return sent


def _process_photo(
    message: Message,
    transport: Transport,
    *,
    chat_id: int,
    update: Update,
    session: SessionStore,
    gate: VisionGate,
    log: logging.Logger,
) -> bool:
    photo = largest_photo(message)
    if photo is None:
        return _prompt(
            transport, chat_id, update, log, reason="empty_photo"
        )

    try:
        image = fetch_photo(transport, photo)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        log.error(
            "photo fetch failed",
            extra={
                "event": "photo_fetch_failed",
                "update_id": update.update_id,
                "chat_id": chat_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
            exc_info=True,
        )
        return _send_apology(transport, chat_id, update, log)

    if not image:
        log.error(
            "downloaded photo was empty",
            extra={
                "event": "photo_fetch_failed",
                "update_id": update.update_id,
                "chat_id": chat_id,
                "error": "empty image",
            },
            exc_info=True,
        )
        return _send_apology(transport, chat_id, update, log)

    try:
        verdict = gate.classify(image)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        log.error(
            "vision classification failed",
            extra={
                "event": "vision_failed",
                "update_id": update.update_id,
                "chat_id": chat_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
            exc_info=True,
        )
        return _send_apology(transport, chat_id, update, log)

    log.info(
        "gate verdict received",
        extra={
            "event": "bouncer_verdict",
            "update_id": update.update_id,
            "chat_id": chat_id,
            "is_human": verdict.is_human,
            "reason": verdict.reason,
        },
    )

    if verdict.is_human:
        session.record_pass(chat_id, image)
        log.info(
            "portrait passed the gate",
            extra={
                "event": "bouncer_passed",
                "update_id": update.update_id,
                "chat_id": chat_id,
            },
        )
        return _send(transport, chat_id, SUCCESS_REPLY, update, log)

    session.reset(chat_id)
    log.info(
        "portrait rejected and session reset",
        extra={
            "event": "bouncer_rejected",
            "update_id": update.update_id,
            "chat_id": chat_id,
            "reason": verdict.reason,
        },
    )
    return _send(transport, chat_id, REJECTION_REPLY, update, log)


def _prompt(
    transport: Transport,
    chat_id: int,
    update: Update,
    log: logging.Logger,
    *,
    reason: str,
) -> bool:
    log.info(
        "prompted for a portrait",
        extra={
            "event": "bouncer_prompted",
            "update_id": update.update_id,
            "chat_id": chat_id,
            "reason": reason,
        },
    )
    return _send(transport, chat_id, PROMPT_PHOTO, update, log)


def _send_apology(
    transport: Transport,
    chat_id: int,
    update: Update,
    log: logging.Logger,
) -> bool:
    return _send(transport, chat_id, APOLOGY_REPLY, update, log)


def _send(
    transport: Transport,
    chat_id: int,
    text: str,
    update: Update,
    log: logging.Logger,
) -> bool:
    """Send one message; degrade a failure to ``False`` with a loud log."""
    try:
        transport.call("sendMessage", {"chat_id": chat_id, "text": text})
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        log.error(
            "reply failed",
            extra={
                "event": "reply_failed",
                "update_id": update.update_id,
                "chat_id": chat_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
            exc_info=True,
        )
        return False
    return True
