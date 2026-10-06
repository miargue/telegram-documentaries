"""Behaviour of the echo rule: every incoming message gets one 'hey mate!'."""

from __future__ import annotations

import logging

from conftest import FakeTransport, messageless_update, photo_update, text_update
from telegram_documentaries.echo import ECHO_REPLY, handle_update
from telegram_documentaries.models import Update
from telegram_documentaries.polling import poll_once


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
    handler = logging.Handler()
    handler.emit = with_records.append  # type: ignore[method-assign]
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
