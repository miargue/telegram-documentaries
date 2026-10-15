"""Long-polling loop: ``getUpdates`` fetching with offset acknowledgement.

Each processed batch advances the offset to one past the highest seen
``update_id``, so Telegram never redelivers what was already handled. An
update that fails validation but carries an integer ``update_id`` is acked
past and skipped, so one poison payload cannot wedge the loop; an update
whose ``update_id`` cannot be parsed as an int has no id to ack with, so it
is skipped and **left unacked** (Telegram will redeliver it). The first poll
starts at ``offset=-1`` to drop the backlog accumulated while the bot was
down.

**Transient** ``getUpdates`` failures — a raw ``OSError``, or a
``TelegramApiError`` with no code (network-normalised), code ``429`` or code
``>= 500`` — are **retried forever with capped exponential backoff** rather
than propagated, so a brief outage no longer kills the process. A
**permanent** coded ``TelegramApiError`` (any other 4xx, e.g. ``401`` revoked
token or ``409`` duplicate poller) is not retried: it propagates so the CLI
exits ``1`` and the fault surfaces. Process-control signals
(``KeyboardInterrupt``/``SystemExit``) and genuine programming errors escape
too. Longer stage calls on the hot path (``handle_update``) run
synchronously — intentionally, single-threaded by decision — and are bounded
by their configured transport/LLM timeouts (see
``SPECS/2026-10-15-resilience``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from pydantic import ValidationError

from telegram_documentaries.bouncer import handle_update
from telegram_documentaries.converter import ConverterLLM
from telegram_documentaries.interviewer import InterviewLLM
from telegram_documentaries.logging_config import log_lifecycle
from telegram_documentaries.models import Update
from telegram_documentaries.narrator import NarratorLLM
from telegram_documentaries.scripter import ScripterLLM
from telegram_documentaries.session import SessionStore
from telegram_documentaries.transport import TelegramApiError, Transport
from telegram_documentaries.vision import VisionGate

# Server-side long-poll window: Telegram holds the request open this long.
LONG_POLL_TIMEOUT = 10

# Transient-failure backoff: the first retry waits RETRY_BASE_DELAY and each
# consecutive failure doubles it, capped at RETRY_MAX_DELAY.
RETRY_BASE_DELAY = 1.0
RETRY_FACTOR = 2.0
RETRY_MAX_DELAY = 30.0

LOGGER = logging.getLogger(__name__)


def _is_transient(exc: TelegramApiError) -> bool:
    """True when ``exc`` is worth retrying: a blip, not a genuine fault.

    The effective code is ``status_code`` when present (HTTP-level failure),
    else ``error_code`` (Telegram's own ``ok: false`` payload). A
    ``TelegramApiError`` with no code at all is network-normalised (the
    transport raises one for dropped connections and timeouts) and counts as
    transient; ``429`` (rate limit) and ``>= 500`` (server fault) are
    retried; any other coded 4xx — e.g. ``401`` revoked token or ``409``
    duplicate poller — is permanent and must propagate so the process exits
    ``1`` instead of looping forever.
    """
    code = exc.status_code if exc.status_code is not None else exc.error_code
    if code is None:
        return True
    return code == 429 or code >= 500


def _get_updates(
    transport: Transport,
    offset: int,
    *,
    log: logging.Logger,
    sleep: Callable[[float], None] = time.sleep,
) -> object:
    """Fetch one ``getUpdates`` batch, retrying transient failures forever.

    A raw ``OSError`` or a *transient* ``TelegramApiError`` (no code,
    ``429`` or ``>= 500``; see :func:`_is_transient`) is logged as a
    structured ``get_updates_retry`` event (``offset``, ``attempt`` and the
    ``delay`` before the next try) and retried after ``sleep(delay)`` with a
    capped exponential backoff. A *permanent* coded ``TelegramApiError`` (any
    other 4xx) propagates so the CLI exits ``1``; ``KeyboardInterrupt`` /
    ``SystemExit`` and any other exception also escape untouched. ``sleep``
    is injected so tests never wait.
    """
    attempt = 1
    delay = RETRY_BASE_DELAY
    while True:
        try:
            return transport.call(
                "getUpdates",
                {"offset": offset, "timeout": LONG_POLL_TIMEOUT},
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except (TelegramApiError, OSError) as exc:
            if isinstance(exc, TelegramApiError) and not _is_transient(exc):
                raise
            log.error(
                "getUpdates failed; retrying with backoff",
                extra={
                    "event": "get_updates_retry",
                    "offset": offset,
                    "attempt": attempt,
                    "delay": delay,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                exc_info=True,
            )
            sleep(delay)
            attempt += 1
            delay = min(delay * RETRY_FACTOR, RETRY_MAX_DELAY)


def poll_once(
    transport: Transport,
    offset: int,
    *,
    session: SessionStore,
    gate: VisionGate,
    llm: InterviewLLM,
    converter: ConverterLLM,
    scripter: ScripterLLM,
    narrator: NarratorLLM | None = None,
    logger: logging.Logger | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Fetch one batch, handle every message in it, return the next offset."""
    log = LOGGER if logger is None else logger
    payload = _get_updates(transport, offset, log=log, sleep=sleep)
    if not isinstance(payload, dict):
        # A transport bug must not escape as an AttributeError into the CLI,
        # which only catches TelegramApiError/OSError.
        log.error(
            "getUpdates returned a non-object payload",
            extra={
                "event": "get_updates_invalid",
                "offset": offset,
                "payload_type": type(payload).__name__,
            },
        )
        return max(offset, 0)

    raw_updates = payload.get("result", [])
    if not isinstance(raw_updates, list):
        log.error(
            "getUpdates result is not a list",
            extra={"event": "get_updates_invalid", "offset": offset},
        )
        raw_updates = []

    # ``-1`` is only a first-poll sentinel ("forget the backlog"); every offset
    # handed back to the caller must be a valid Telegram offset (>= 0).
    next_offset = max(offset, 0)
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
        # Converter generation (up to ~120s) is the first slow call on this
        # polling hot path; the call is intentionally synchronous — bounded
        # by its configured timeout, single-threaded by decision (no worker
        # thread or queue; see SPECS/2026-10-15-resilience).
        handle_update(
            update,
            transport,
            session=session,
            gate=gate,
            llm=llm,
            converter=converter,
            scripter=scripter,
            narrator=narrator,
            logger=log,
        )

    return next_offset


@log_lifecycle(LOGGER, "run_polling")
def run_polling(
    transport: Transport,
    *,
    session: SessionStore,
    gate: VisionGate,
    llm: InterviewLLM,
    converter: ConverterLLM,
    scripter: ScripterLLM,
    narrator: NarratorLLM | None = None,
    offset: int = -1,
    max_polls: int | None = None,
    logger: logging.Logger | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Poll forever (or for ``max_polls`` iterations) and return the offset.

    ``max_polls=None`` means run until the process is stopped; tests and
    smoke runs pass a finite bound. ``sleep`` is forwarded to
    :func:`poll_once` so the ``getUpdates`` retry path is testable
    end-to-end without waiting.

    The first poll deliberately uses ``offset=-1`` (the default): Telegram
    then forgets everything queued before the latest update, so a restart does
    not re-answer a backlog of already-handled messages.
    """
    log = LOGGER if logger is None else logger
    polls = 0
    while max_polls is None or polls < max_polls:
        offset = poll_once(
            transport,
            offset,
            session=session,
            gate=gate,
            llm=llm,
            converter=converter,
            scripter=scripter,
            narrator=narrator,
            logger=log,
            sleep=sleep,
        )
        polls += 1
    return offset
