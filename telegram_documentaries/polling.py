"""Long-polling loop: ``getUpdates`` fetching with offset acknowledgement.

Each processed batch advances the offset to one past the highest seen
``update_id``, so Telegram never redelivers what was already handled —
including malformed/poison updates, which are acked and skipped rather than
replayed forever. Transport failures on ``getUpdates`` itself propagate
loudly (transient-failure resilience is Phase 7, see ``SPECS/ROADMAP.md``).
"""

from __future__ import annotations

import logging

from pydantic import ValidationError

from telegram_documentaries.echo import handle_update
from telegram_documentaries.logging_config import log_lifecycle
from telegram_documentaries.models import Update
from telegram_documentaries.transport import Transport

# Server-side long-poll window: Telegram holds the request open this long.
LONG_POLL_TIMEOUT = 10

LOGGER = logging.getLogger(__name__)


def poll_once(
    transport: Transport,
    offset: int,
    *,
    logger: logging.Logger | None = None,
) -> int:
    """Fetch one batch, handle every message in it, return the next offset."""
    log = LOGGER if logger is None else logger
    payload = transport.call(
        "getUpdates",
        {"offset": offset, "timeout": LONG_POLL_TIMEOUT},
    )
    raw_updates = payload.get("result", [])
    if not isinstance(raw_updates, list):
        log.error(
            "getUpdates result is not a list",
            extra={"event": "get_updates_invalid", "offset": offset},
        )
        raw_updates = []

    next_offset = offset
    for raw in raw_updates:
        if not isinstance(raw, dict):
            log.error(
                "non-object update skipped",
                extra={"event": "update_invalid", "offset": offset},
            )
            continue
        try:
            update = Update.model_validate(raw)
        except ValidationError as exc:
            # Ack past unusable updates (when they carry an id) so one poison
            # payload cannot wedge the loop into replaying it forever.
            update_id = raw.get("update_id")
            if isinstance(update_id, int) and not isinstance(update_id, bool):
                next_offset = max(next_offset, update_id + 1)
            log.error(
                "update failed validation and was skipped",
                extra={
                    "event": "update_invalid",
                    "update_id": update_id
                    if isinstance(update_id, int)
                    else None,
                },
                exc_info=exc,
            )
            continue

        next_offset = max(next_offset, update.update_id + 1)
        handle_update(update, transport, logger=log)

    return next_offset


@log_lifecycle(LOGGER, "run_polling")
def run_polling(
    transport: Transport,
    *,
    offset: int = 0,
    max_polls: int | None = None,
    logger: logging.Logger | None = None,
) -> int:
    """Poll forever (or for ``max_polls`` iterations) and return the offset.

    ``max_polls=None`` means run until the process is stopped; tests and
    smoke runs pass a finite bound.
    """
    log = LOGGER if logger is None else logger
    polls = 0
    while max_polls is None or polls < max_polls:
        offset = poll_once(transport, offset, logger=log)
        polls += 1
    return offset
