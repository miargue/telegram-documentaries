"""Behaviour of the echo rule: every incoming message gets one 'hey mate!'."""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import pytest
from conftest import (
    FakeTransport,
    _RecordingHandler,
    messageless_update,
    photo_update,
    text_update,
)

from telegram_documentaries.echo import ECHO_REPLY, handle_update
from telegram_documentaries.models import Update
from telegram_documentaries.polling import poll_once


class FailFirstSendTransport(FakeTransport):
    """First ``sendMessage`` raises a bare ``RuntimeError`` (a transport bug
    that escaped normalisation); every later send behaves normally."""

    def __init__(
        self, batches: list[list[dict[str, Any]]]
    ) -> None:
        super().__init__(batches)
        self.attempts: list[dict[str, Any]] = []
        self._failed = False

    def call(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        if method == "sendMessage":
            self.attempts.append(dict(params or {}))
            if not self._failed:
                self._failed = True
                raise RuntimeError("transport bug: socket exploded")
        return super().call(method, params)


@contextmanager
def _capture_records(
    logger_name: str,
) -> Iterator[tuple[logging.Logger, list[logging.LogRecord]]]:
    """Yield ``(logger, records)`` with every emitted record collected."""
    records: list[logging.LogRecord] = []
    logger = logging.getLogger(logger_name)
    handler = _RecordingHandler(records)
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield logger, records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def test_text_message_gets_exactly_one_hey_mate_reply() -> None:
    transport = FakeTransport()

    sent = handle_update(Update.model_validate(text_update(1, chat_id=7)), transport)

    assert sent is True
    replies = transport.calls_for("sendMessage")
    assert len(replies) == 1
    assert replies[0]["text"] == "hey mate!"
    assert replies[0]["chat_id"] == 7


def test_echo_reply_text_is_exactly_the_hardcoded_string() -> None:
    transport = FakeTransport()
    handle_update(Update.model_validate(text_update(2, chat_id=1)), transport)

    assert transport.calls_for("sendMessage")[0]["text"] == ECHO_REPLY
    assert ECHO_REPLY == "hey mate!"


def test_reply_goes_to_the_messages_own_chat_only() -> None:
    transport = FakeTransport()
    handle_update(Update.model_validate(text_update(3, chat_id=111)), transport)
    handle_update(Update.model_validate(text_update(4, chat_id=222)), transport)

    replies = transport.calls_for("sendMessage")
    assert [reply["chat_id"] for reply in replies] == [111, 222]


def test_non_text_message_still_gets_exactly_one_reply() -> None:
    transport = FakeTransport()

    sent = handle_update(Update.model_validate(photo_update(5, chat_id=8)), transport)

    assert sent is True
    replies = transport.calls_for("sendMessage")
    assert len(replies) == 1
    assert replies[0]["text"] == "hey mate!"
    assert replies[0]["chat_id"] == 8


def test_update_without_message_is_ignored_without_crash_or_reply() -> None:
    transport = FakeTransport()

    sent = handle_update(
        Update.model_validate(messageless_update(6)), transport
    )

    assert sent is False
    assert transport.calls_for("sendMessage") == []


def test_batch_with_mixed_updates_replies_once_per_message() -> None:
    transport = FakeTransport(
        batches=[
            [
                text_update(10, chat_id=1),
                messageless_update(11),
                photo_update(12, chat_id=2),
                text_update(13, chat_id=3),
            ]
        ]
    )

    poll_once(transport, offset=0)

    replies = transport.calls_for("sendMessage")
    assert len(replies) == 3
    assert {reply["chat_id"] for reply in replies} == {1, 2, 3}
    assert {reply["text"] for reply in replies} == {"hey mate!"}


def test_send_failure_is_logged_and_does_not_escape_the_handler() -> None:
    from telegram_documentaries.transport import TelegramApiError

    transport = FakeTransport(
        fail_on={"sendMessage": TelegramApiError("sendMessage", "bot blocked")}
    )

    with_records: list[logging.LogRecord] = []
    logger = logging.getLogger("echo.failure")
    handler = _RecordingHandler(with_records)
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        sent = handle_update(
            Update.model_validate(text_update(20, chat_id=5)),
            transport,
            logger=logger,
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    assert sent is False
    assert any(r.levelno >= logging.ERROR for r in with_records)


def test_unexpected_transport_exception_is_logged_and_swallowed() -> None:
    # TECH.md: degrade gracefully on the user's conversation path — a transport
    # bug that escaped normalisation must not raise into the user's flow.
    transport = FakeTransport(
        fail_on={"sendMessage": RuntimeError("transport bug: socket exploded")}
    )

    with _capture_records("echo.unexpected") as (logger, records):
        sent = handle_update(
            Update.model_validate(text_update(70, chat_id=42)),
            transport,
            logger=logger,
        )

    assert sent is False
    failures = [r for r in records if r.levelno >= logging.ERROR]
    assert failures, "a swallowed failure must still be logged at ERROR"
    failure = failures[0]
    assert getattr(failure, "chat_id", None) == 42
    assert getattr(failure, "update_id", None) == 70
    assert failure.exc_info is not None  # loud means the traceback is kept


def test_loop_survives_a_bare_transport_crash() -> None:
    transport = FailFirstSendTransport(
        [[text_update(80, chat_id=1), text_update(81, chat_id=2)]]
    )

    next_offset = poll_once(transport, offset=0)

    # The batch is fully handled and acked despite the crash on update 80.
    assert next_offset == 82
    assert [attempt["chat_id"] for attempt in transport.attempts] == [1, 2]
    # Only the healthy update produced a send; the survivor got its reply.
    assert transport.calls_for("sendMessage") == [
        {"chat_id": 2, "text": ECHO_REPLY}
    ]


def test_process_control_exceptions_are_not_swallowed() -> None:
    # The catch is broad but not blind: KeyboardInterrupt/SystemExit must
    # still stop the process instead of being degraded into a failed reply.
    transport = FakeTransport(fail_on={"sendMessage": KeyboardInterrupt()})

    with pytest.raises(KeyboardInterrupt):
        handle_update(
            Update.model_validate(text_update(90, chat_id=3)), transport
        )
