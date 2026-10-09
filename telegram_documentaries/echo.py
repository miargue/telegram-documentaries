"""The Phase 1 echo rule.

Every incoming Telegram **message** — text, photo, or any other content type
— gets exactly one reply: the hardcoded string ``hey mate!``. Updates that
carry no ``message`` at all (edited messages, callback queries, ...) are
acknowledged by the poll loop and ignored here without crashing.
"""

from __future__ import annotations

import logging

from telegram_documentaries.logging_config import log_lifecycle
from telegram_documentaries.models import Update
from telegram_documentaries.transport import Transport

ECHO_REPLY = "hey mate!"

LOGGER = logging.getLogger(__name__)


@log_lifecycle(LOGGER, "handle_update")
def handle_update(
    update: Update,
    transport: Transport,
    *,
    logger: logging.Logger | None = None,
) -> bool:
    """Send the echo reply for ``update``; return whether a reply was sent.

    Reply failures are logged loudly and degraded from (``False``): a single
    unreachable chat must not take down the polling loop or the next user's
    conversation.
    """
    log = LOGGER if logger is None else logger
    message = update.message
    if message is None:
        log.info(
            "update without message ignored",
            extra={"event": "update_skipped", "update_id": update.update_id},
        )
        return False

    chat_id = message.chat.id
    try:
        transport.call("sendMessage", {"chat_id": chat_id, "text": ECHO_REPLY})
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        log.error(
            "echo reply failed",
            extra={
                "event": "reply_failed",
                "update_id": update.update_id,
                "chat_id": chat_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
            exc_info=True,
        )
        return False

    log.info(
        "echo reply sent",
        extra={
            "event": "reply_sent",
            "update_id": update.update_id,
            "chat_id": chat_id,
            "message_id": message.message_id,
        },
    )
    return True
