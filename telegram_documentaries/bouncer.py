"""The Bouncer: the vision gate in front of the pipeline.

Routes each incoming Telegram message:

- ``/start`` / ``/restart`` → reset the session and ask for a portrait.
- ``awaiting_photo``: plain text or any non-photo content re-prompts (never
  crash, never lose the session); a photo is downloaded at its largest
  resolution and the :class:`VisionGate` decides whether a human is present.
  A human is retained and — since Phase 3 — the chat immediately hands off to
  the Interviewer, which asks question 1 right after the success line. A
  non-human gets a cheeky rejection and a session reset.
- ``gate_passed`` / ``interviewing`` / ``done``: every update (except a
  ``/start`` command or an album sibling already handled) is delegated to the
  Interviewer, which owns the turn-taking.

Every download/gate/send failure is caught, logged loudly with a traceback and
degraded to an apology/re-prompt — nothing is allowed to raise into the polling
loop (``SPECS/TECH.md`` §Logging & error policy).
"""

from __future__ import annotations

import logging

from telegram_documentaries.interviewer import (
    InterviewLLM,
    start_interview,
)
from telegram_documentaries.interviewer import (
    handle_update as handle_interview_update,
)
from telegram_documentaries.logging_config import SKIPPED, Skipped, log_lifecycle
from telegram_documentaries.media import fetch_photo, largest_photo
from telegram_documentaries.models import Message, Update
from telegram_documentaries.session import Phase, SessionStore
from telegram_documentaries.transport import Transport, truncate_message
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
    llm: InterviewLLM,
    logger: logging.Logger | None = None,
) -> bool | Skipped:
    """Route one update; return whether a reply was sent.

    ``session``, ``gate`` and ``llm`` are injected so the caller owns their
    lifetime (the CLI builds one store, one gate and one interviewer for the
    process). A ``/start`` command always resets, whatever the phase. Once a
    portrait has passed, the chat is delegated to the Interviewer for every
    other update. A deliberate, benign no-op returns :data:`SKIPPED` (so
    ``log_lifecycle`` logs it as ``skipped`` rather than a false ``degraded``
    warning); a genuine reply failure returns ``False``.
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

    if _is_album_follow_up(session, chat_id, message):
        log.info(
            "album follow-up ignored",
            extra={
                "event": "bouncer_album_skipped",
                "update_id": update.update_id,
                "chat_id": chat_id,
                "media_group_id": message.media_group_id,
            },
        )
        return SKIPPED

    if session.get(chat_id).phase is not Phase.awaiting_photo:
        # The portrait has passed (or a degraded handoff left the chat in
        # ``gate_passed``): the Interviewer owns every remaining update.
        # Remember the album id for *any* grouped photo here too — otherwise a
        # (photo) album sent mid-interview or after completion would reach the
        # Interviewer once per member and send one identical re-ask per photo.
        if message.media_group_id is not None and message.photo:
            session.remember_media_group(chat_id, message.media_group_id)
        return handle_interview_update(
            update, transport, session=session, llm=llm, logger=log
        )

    if text:
        return _prompt(transport, chat_id, update, log, reason="text")
    if message.photo:
        return _handle_photo(
            message, transport, chat_id=chat_id, update=update,
            session=session, gate=gate, llm=llm, log=log,
        )
    return _prompt(transport, chat_id, update, log, reason="unsupported_content")


def _is_album_follow_up(
    session: SessionStore, chat_id: int, message: Message
) -> bool:
    """True when this photo is a sibling of an album already handled.

    Telegram delivers each photo of a single send as its own update sharing a
    ``media_group_id``. Only the first member is processed; the id is
    remembered once a verdict has been recorded (see ``remember_media_group``),
    so the rest are dropped silently — even after the first member has handed
    the chat off to the Interviewer.
    """
    media_group_id = message.media_group_id
    return (
        media_group_id is not None
        and session.get(chat_id).last_media_group_id == media_group_id
    )


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
    llm: InterviewLLM,
    log: logging.Logger,
) -> bool:
    """Gate the first photo of an album; remember its id once handled.

    The album sibling check happens before dispatch (see ``_is_album_follow_up``)
    so a follow-up is dropped even after the first member handed off to the
    Interviewer. Here we only remember the id *after* a verdict has been
    recorded, so the rest of the album is ignored.
    """
    media_group_id = message.media_group_id
    sent = _process_photo(
        message,
        transport,
        chat_id=chat_id,
        update=update,
        session=session,
        gate=gate,
        llm=llm,
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
    llm: InterviewLLM,
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
        if not _send(transport, chat_id, SUCCESS_REPLY, update, log):
            # The success line never landed: stay retryable (``gate_passed``)
            # and hand off on the next update instead.
            return False
        return start_interview(
            transport, chat_id, session=session, llm=llm, logger=log
        )

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
    """Send one message; degrade a failure to ``False`` with a loud log.

    Every outbound message is defensively bounded to Telegram's limit at this
    chokepoint (mechanism-level guard for the over-long outbound-text bug
    class): the reply is truncated before it can be rejected by ``sendMessage``.
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
                "update_id": update.update_id,
                "chat_id": chat_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
            exc_info=True,
        )
        return False
    return True
