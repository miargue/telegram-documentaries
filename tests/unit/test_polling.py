"""Behaviour of the long-poll loop: getUpdates fetching and offset acking."""

from __future__ import annotations

import logging

from conftest import FakeTransport, messageless_update, text_update
from telegram_documentaries.polling import LONG_POLL_TIMEOUT, poll_once, run_polling
from telegram_documentaries.transport import TelegramApiError


def test_get_updates_is_long_polling_with_offset_param() -> None:
    transport = FakeTransport(batches=[[]])

    poll_once(transport, offset=0)

    params = transport.calls_for("getUpdates")[0]
    assert params["timeout"] == LONG_POLL_TIMEOUT
    assert params["timeout"] > 0
    assert params["offset"] == 0


def test_offset_ack_advances_to_one_past_the_processed_update() -> None:
    transport = FakeTransport(batches=[[text_update(10, chat_id=1)], []])

    next_offset = poll_once(transport, offset=0)
    assert next_offset == 11

    poll_once(transport, offset=next_offset)
    second_fetch = transport.calls_for("getUpdates")[1]
    assert second_fetch["offset"] == 11


def test_offset_advances_past_the_highest_update_in_a_batch() -> None:
    transport = FakeTransport(
        batches=[[text_update(7, chat_id=1), text_update(9, chat_id=2)]]
    )

    assert poll_once(transport, offset=0) == 10


def test_empty_batch_keeps_the_current_offset() -> None:
    transport = FakeTransport(batches=[[]])

    assert poll_once(transport, offset=5) == 5


def test_offset_never_goes_backwards_when_updates_arrive_unordered() -> None:
    transport = FakeTransport(batches=[[text_update(3, chat_id=1), text_update(8, chat_id=1)]])

    assert poll_once(transport, offset=6) == 9


def test_malformed_update_is_acked_and_skipped_without_crashing() -> None:
    transport = FakeTransport(
        batches=[
            [
                {"message": {"message_id": 1, "chat": {"id": 1}}},  # no update_id
                text_update(15, chat_id=1),
            ]
        ]
    )

    next_offset = poll_once(transport, offset=0)

    assert next_offset == 16  # the good update is acked...
    assert len(transport.calls_for("sendMessage")) == 1  # ...and handled


def test_poison_update_is_acked_so_it_cannot_loop_forever() -> None:
    # update_id present but the payload is unusable: still advance past it,
    # otherwise getUpdates would replay it forever.
    poison = {"update_id": 21, "message": {"message_id": 1}}  # message has no chat
    transport = FakeTransport(batches=[[poison, text_update(22, chat_id=4)]])

    next_offset = poll_once(transport, offset=0)

    assert next_offset == 23
    replies = transport.calls_for("sendMessage")
    assert len(replies) == 1
    assert replies[0]["chat_id"] == 4


def test_non_dict_entry_in_batch_is_ignored_without_crash() -> None:
    transport = FakeTransport(batches=[["garbage", text_update(30, chat_id=9)]])

    next_offset = poll_once(transport, offset=0)

    assert next_offset == 31
    assert len(transport.calls_for("sendMessage")) == 1


def test_messageless_update_is_acked_without_a_reply() -> None:
    transport = FakeTransport(batches=[[messageless_update(35)]])

    next_offset = poll_once(transport, offset=0)

    assert next_offset == 36  # acknowledged...
    assert transport.calls_for("sendMessage") == []  # ...but not answered


def test_send_failure_is_logged_and_the_loop_keeps_acking() -> None:
    transport = FakeTransport(
        batches=[[text_update(40, chat_id=1), text_update(41, chat_id=2)]],
        fail_on={"sendMessage": TelegramApiError("sendMessage", "unauthorized")},
    )
    records: list[logging.LogRecord] = []
    logger = logging.getLogger("polling.failure")
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        next_offset = poll_once(transport, offset=0, logger=logger)
    finally:
        logger.removeHandler(handler)

    assert next_offset == 42
    assert len(transport.calls_for("sendMessage")) == 2
    assert any(r.levelno >= logging.ERROR for r in records)


def test_get_updates_transport_failure_propagates_loudly() -> None:
    transport = FakeTransport(
        fail_on={"getUpdates": TelegramApiError("getUpdates", "network down")}
    )

    try:
        poll_once(transport, offset=0)
    except TelegramApiError:
        pass
    else:  # pragma: no cover - fail loudly is the contract
        raise AssertionError("poll_once swallowed a getUpdates failure")


def test_run_polling_stops_after_max_polls_and_carries_offset() -> None:
    transport = FakeTransport(batches=[[text_update(50, chat_id=1)], []])

    final_offset = run_polling(transport, offset=0, max_polls=2)

    assert final_offset == 51
    fetches = transport.calls_for("getUpdates")
    assert len(fetches) == 2
    assert fetches[1]["offset"] == 51


def test_run_polling_from_a_given_offset() -> None:
    transport = FakeTransport(batches=[[text_update(60, chat_id=1)]])

    final_offset = run_polling(transport, offset=60, max_polls=1)

    assert final_offset == 61
    assert transport.calls_for("getUpdates")[0]["offset"] == 60
