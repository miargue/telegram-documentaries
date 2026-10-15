"""Behaviour of the long-poll loop: getUpdates fetching and offset acking.

``session`` and ``gate`` are injected thread-through dependencies: the loop
must pass the same instances to every ``handle_update`` call so one process
shares one session store and one gate.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import pytest
from conftest import (
    FakeConverter,
    FakeGate,
    FakeLLM,
    FakeNarrator,
    FakeScripter,
    FakeTransport,
    _capture,
    _RecordingHandler,
    messageless_update,
    photo_update,
    text_update,
)

from telegram_documentaries.polling import (
    LONG_POLL_TIMEOUT,
    RETRY_BASE_DELAY,
    RETRY_FACTOR,
    RETRY_MAX_DELAY,
    poll_once,
    run_polling,
)
from telegram_documentaries.session import Dossier, Phase, QaPair, SessionStore
from telegram_documentaries.transport import TelegramApiError
from telegram_documentaries.vision import HumanVerdict


class _NonDictTransport:
    """Transport double that violates its contract by returning a list."""

    def call(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> Any:
        return ["not", "a", "dict"]

    def download(self, file_path: str) -> bytes:
        return b""

    def send_photo(
        self,
        chat_id: int,
        image: bytes,
        *,
        media_type: str,
        caption: str | None = None,
    ) -> Any:
        return {"ok": True}

    def send_audio(
        self,
        chat_id: int,
        audio: bytes,
        *,
        media_type: str,
        filename: str = "narration.mp3",
    ) -> Any:
        return {"ok": True}


class _FlakyGetUpdates(FakeTransport):
    """A transport whose ``getUpdates`` fails ``failures`` times, then works.

    Every other method delegates to the real :class:`FakeTransport`, so a test
    can prove the loop rides out a transient outage and then processes the
    batch normally.
    """

    def __init__(
        self,
        *,
        failures: int,
        error: BaseException | None = None,
        batches: list[list[Any]] | None = None,
        files: dict[str, tuple[str | None, bytes]] | None = None,
    ) -> None:
        super().__init__(batches=batches, files=files)
        self.remaining_failures = failures
        self.error: BaseException = (
            TelegramApiError("getUpdates", "network down")
            if error is None
            else error
        )
        self.get_updates_attempts = 0

    def call(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        if method == "getUpdates":
            self.get_updates_attempts += 1
            if self.remaining_failures > 0:
                self.remaining_failures -= 1
                raise self.error
        return super().call(method, params)


def _retry_records(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    return [r for r in records if getattr(r, "event", "") == "get_updates_retry"]


def _store() -> SessionStore:
    """A throwaway session store, keeping each call isolated."""
    return SessionStore()


def test_non_object_get_updates_payload_is_logged_and_never_crashes() -> None:
    # Regression: ``payload.get(...)`` used to raise AttributeError (which the
    # CLI did not catch) when the transport handed back a non-object.
    records: list[logging.LogRecord] = []
    logger = logging.getLogger("polling.non_dict")
    handler = _RecordingHandler(records)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        offset = poll_once(
            _NonDictTransport(),
            offset=0,
            session=_store(),
            gate=FakeGate(),
            converter=FakeConverter(),
            scripter=FakeScripter(),
            llm=FakeLLM(),
            logger=logger,
        )
    finally:
        logger.removeHandler(handler)

    assert offset == 0
    failures = [r for r in records if r.levelno >= logging.ERROR]
    assert failures, "a malformed transport payload must be logged loudly"
    assert any(getattr(r, "event", None) == "get_updates_invalid" for r in failures)


def test_get_updates_is_long_polling_with_offset_param() -> None:
    transport = FakeTransport(batches=[[]])

    poll_once(

        transport,

        offset=0,

        session=_store(),

        gate=FakeGate(),

        llm=FakeLLM(),

        converter=FakeConverter(),
        scripter=FakeScripter(),

    )

    params = transport.calls_for("getUpdates")[0]
    assert params["timeout"] == LONG_POLL_TIMEOUT
    assert params["timeout"] > 0
    assert params["offset"] == 0


def test_offset_ack_advances_to_one_past_the_processed_update() -> None:
    transport = FakeTransport(batches=[[text_update(10, chat_id=1)], []])

    next_offset = poll_once(
        transport,
        offset=0,
        session=_store(),
        gate=FakeGate(),
        llm=FakeLLM(),
        converter=FakeConverter(),
        scripter=FakeScripter(),
    )
    assert next_offset == 11

    poll_once(
        transport,
        offset=next_offset,
        session=_store(),
        gate=FakeGate(),
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
    )
    second_fetch = transport.calls_for("getUpdates")[1]
    assert second_fetch["offset"] == 11


def test_offset_advances_past_the_highest_update_in_a_batch() -> None:
    transport = FakeTransport(
        batches=[[text_update(7, chat_id=1), text_update(9, chat_id=2)]]
    )

    assert (
        poll_once(
            transport,
            offset=0,
            session=_store(),
            gate=FakeGate(),
            llm=FakeLLM(),
            converter=FakeConverter(),
            scripter=FakeScripter(),
        )
        == 10
    )


def test_empty_batch_keeps_the_current_offset() -> None:
    transport = FakeTransport(batches=[[]])

    assert (
        poll_once(
            transport,
            offset=5,
            session=_store(),
            gate=FakeGate(),
            llm=FakeLLM(),
            converter=FakeConverter(),
            scripter=FakeScripter(),
        )
        == 5
    )


def test_offset_never_goes_backwards_when_updates_arrive_unordered() -> None:
    transport = FakeTransport(
        batches=[[text_update(3, chat_id=1), text_update(8, chat_id=1)]]
    )

    assert (
        poll_once(
            transport,
            offset=6,
            session=_store(),
            gate=FakeGate(),
            llm=FakeLLM(),
            converter=FakeConverter(),
            scripter=FakeScripter(),
        )
        == 9
    )


def test_malformed_update_is_acked_and_skipped_without_crashing() -> None:
    transport = FakeTransport(
        batches=[
            [
                {"message": {"message_id": 1, "chat": {"id": 1}}},  # no update_id
                text_update(15, chat_id=1),
            ]
        ]
    )

    next_offset = poll_once(
        transport,
        offset=0,
        session=_store(),
        gate=FakeGate(),
        llm=FakeLLM(),
        converter=FakeConverter(),
        scripter=FakeScripter(),
    )

    assert next_offset == 16  # the good update is acked...
    assert len(transport.calls_for("sendMessage")) == 1  # ...and handled


def test_poison_update_is_acked_so_it_cannot_loop_forever() -> None:
    # update_id present but the payload is unusable: still advance past it,
    # otherwise getUpdates would replay it forever.
    poison = {"update_id": 21, "message": {"message_id": 1}}  # message has no chat
    transport = FakeTransport(batches=[[poison, text_update(22, chat_id=4)]])

    next_offset = poll_once(
        transport,
        offset=0,
        session=_store(),
        gate=FakeGate(),
        llm=FakeLLM(),
        converter=FakeConverter(),
        scripter=FakeScripter(),
    )

    assert next_offset == 23
    replies = transport.calls_for("sendMessage")
    assert len(replies) == 1
    assert replies[0]["chat_id"] == 4


def test_non_dict_entry_in_batch_is_ignored_without_crash() -> None:
    transport = FakeTransport(batches=[["garbage", text_update(30, chat_id=9)]])

    next_offset = poll_once(
        transport,
        offset=0,
        session=_store(),
        gate=FakeGate(),
        llm=FakeLLM(),
        converter=FakeConverter(),
        scripter=FakeScripter(),
    )

    assert next_offset == 31
    assert len(transport.calls_for("sendMessage")) == 1


def test_messageless_update_is_acked_without_a_reply() -> None:
    transport = FakeTransport(batches=[[messageless_update(35)]])

    next_offset = poll_once(
        transport,
        offset=0,
        session=_store(),
        gate=FakeGate(),
        llm=FakeLLM(),
        converter=FakeConverter(),
        scripter=FakeScripter(),
    )

    assert next_offset == 36  # acknowledged...
    assert transport.calls_for("sendMessage") == []  # ...but not answered


def test_send_failure_is_logged_and_the_loop_keeps_acking() -> None:
    transport = FakeTransport(
        batches=[[text_update(40, chat_id=1), text_update(41, chat_id=2)]],
        fail_on={"sendMessage": TelegramApiError("sendMessage", "unauthorized")},
    )
    records: list[logging.LogRecord] = []
    logger = logging.getLogger("polling.failure")
    handler = _RecordingHandler(records)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        next_offset = poll_once(
            transport,
            offset=0,
            session=_store(),
            gate=FakeGate(),
            converter=FakeConverter(),
            scripter=FakeScripter(),
            llm=FakeLLM(),
            logger=logger,
        )
    finally:
        logger.removeHandler(handler)

    assert next_offset == 42
    assert len(transport.calls_for("sendMessage")) == 2
    assert any(r.levelno >= logging.ERROR for r in records)


def test_transient_get_updates_failure_is_retried_then_succeeds() -> None:
    # A transient getUpdates outage must be ridden out, not propagated: the
    # long-poll loop retries with backoff and processes the batch once the
    # transport recovers.
    transport = _FlakyGetUpdates(
        failures=2, batches=[[text_update(70, chat_id=1)], []]
    )
    slept: list[float] = []

    with _capture("polling.retry") as (logger, records):
        offset = poll_once(
            transport,
            offset=0,
            session=_store(),
            gate=FakeGate(),
            llm=FakeLLM(),
            converter=FakeConverter(),
            scripter=FakeScripter(),
            logger=logger,
            sleep=slept.append,
        )

    assert offset == 71
    assert transport.get_updates_attempts == 3  # two failures + one success
    assert slept == [RETRY_BASE_DELAY, RETRY_BASE_DELAY * RETRY_FACTOR]
    retries = _retry_records(records)
    assert [getattr(r, "attempt") for r in retries] == [1, 2]
    assert [getattr(r, "delay") for r in retries] == slept
    assert all(getattr(r, "offset") == 0 for r in retries)
    assert all(r.levelno >= logging.ERROR for r in retries)
    assert len(transport.calls_for("sendMessage")) == 1  # batch was handled


def test_get_updates_backoff_grows_exponentially_and_is_capped() -> None:
    transport = _FlakyGetUpdates(failures=8, batches=[[]])
    slept: list[float] = []

    with _capture("polling.backoff") as (logger, records):
        poll_once(
            transport,
            offset=0,
            session=_store(),
            gate=FakeGate(),
            llm=FakeLLM(),
            converter=FakeConverter(),
            scripter=FakeScripter(),
            logger=logger,
            sleep=slept.append,
        )

    assert slept == [
        RETRY_BASE_DELAY,
        RETRY_BASE_DELAY * RETRY_FACTOR,
        RETRY_BASE_DELAY * RETRY_FACTOR**2,
        RETRY_BASE_DELAY * RETRY_FACTOR**3,
        RETRY_BASE_DELAY * RETRY_FACTOR**4,
        RETRY_MAX_DELAY,
        RETRY_MAX_DELAY,
        RETRY_MAX_DELAY,
    ]
    assert max(slept) == RETRY_MAX_DELAY
    delays = [getattr(r, "delay") for r in _retry_records(records)]
    assert delays == slept
    assert all(isinstance(d, float) and d <= RETRY_MAX_DELAY for d in delays)


def test_oserror_on_get_updates_is_retried_like_a_transport_error() -> None:
    # The real transport normalises network faults into TelegramApiError, but a
    # raw OSError must be treated the same way (they share the retry contract).
    transport = _FlakyGetUpdates(
        failures=1, error=OSError("connection reset"), batches=[[]]
    )
    slept: list[float] = []

    with _capture("polling.oserror") as (logger, records):
        offset = poll_once(
            transport,
            offset=0,
            session=_store(),
            gate=FakeGate(),
            llm=FakeLLM(),
            converter=FakeConverter(),
            scripter=FakeScripter(),
            logger=logger,
            sleep=slept.append,
        )

    assert offset == 0
    assert slept == [RETRY_BASE_DELAY]
    retries = _retry_records(records)
    assert len(retries) == 1
    assert "OSError" in str(getattr(retries[0], "error"))


@pytest.mark.parametrize("code", [429, 500])
def test_transient_telegram_codes_are_retried(code: int) -> None:
    # Rate limiting (429) and server faults (>= 500) are transient: the loop
    # must ride them out with backoff, never propagate them.
    error = TelegramApiError("getUpdates", "transient", error_code=code)
    transport = _FlakyGetUpdates(failures=1, error=error, batches=[[]])
    slept: list[float] = []

    offset = poll_once(
        transport,
        offset=0,
        session=_store(),
        gate=FakeGate(),
        llm=FakeLLM(),
        converter=FakeConverter(),
        scripter=FakeScripter(),
        sleep=slept.append,
    )

    assert offset == 0
    assert transport.get_updates_attempts == 2
    assert slept == [RETRY_BASE_DELAY]


def test_http_status_code_takes_precedence_over_the_error_code() -> None:
    # HTTPError normalisation sets both codes; the HTTP status is authoritative.
    error = TelegramApiError(
        "getUpdates", "http 503", status_code=503, error_code=400
    )
    transport = _FlakyGetUpdates(failures=1, error=error, batches=[[]])
    slept: list[float] = []

    offset = poll_once(
        transport,
        offset=0,
        session=_store(),
        gate=FakeGate(),
        llm=FakeLLM(),
        converter=FakeConverter(),
        scripter=FakeScripter(),
        sleep=slept.append,
    )

    assert offset == 0
    assert slept == [RETRY_BASE_DELAY]


@pytest.mark.parametrize("code", [400, 401, 403, 404, 409])
def test_permanent_coded_errors_propagate_unretried(code: int) -> None:
    # A permanent 4xx (revoked token, duplicate poller, bad request, ...) must
    # escape poll_once unhandled so the CLI exits 1 — never loop forever.
    error = TelegramApiError("getUpdates", "permanent", error_code=code)
    transport = _FlakyGetUpdates(failures=1, error=error, batches=[[]])
    slept: list[float] = []

    with pytest.raises(TelegramApiError) as exc_info:
        poll_once(
            transport,
            offset=0,
            session=_store(),
            gate=FakeGate(),
            llm=FakeLLM(),
            converter=FakeConverter(),
            scripter=FakeScripter(),
            sleep=slept.append,
        )

    assert exc_info.value is error
    assert transport.get_updates_attempts == 1  # failed once, never retried
    assert slept == []


def test_run_polling_forwards_the_injected_sleep_to_the_retry_path() -> None:
    # run_polling must thread sleep down to poll_once/_get_updates so the
    # retry path is testable end-to-end without waiting.
    transport = _FlakyGetUpdates(failures=1, batches=[[]])
    slept: list[float] = []

    run_polling(
        transport,
        session=_store(),
        gate=FakeGate(),
        llm=FakeLLM(),
        converter=FakeConverter(),
        scripter=FakeScripter(),
        max_polls=1,
        sleep=slept.append,
    )

    assert slept == [RETRY_BASE_DELAY]
    assert transport.get_updates_attempts == 2


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
def test_process_control_signals_are_not_retried(signal: type[BaseException]) -> None:
    transport = _FlakyGetUpdates(failures=1, error=signal(), batches=[[]])
    slept: list[float] = []

    with pytest.raises(signal):
        poll_once(
            transport,
            offset=0,
            session=_store(),
            gate=FakeGate(),
            llm=FakeLLM(),
            converter=FakeConverter(),
            scripter=FakeScripter(),
            sleep=slept.append,
        )

    assert slept == []


def test_non_transport_error_from_get_updates_propagates() -> None:
    # Only transport-level faults are transient; a programming error must be
    # surfaced, not silently retried forever.
    transport = _FlakyGetUpdates(failures=1, error=RuntimeError("bug"), batches=[[]])
    slept: list[float] = []

    with pytest.raises(RuntimeError):
        poll_once(
            transport,
            offset=0,
            session=_store(),
            gate=FakeGate(),
            llm=FakeLLM(),
            converter=FakeConverter(),
            scripter=FakeScripter(),
            sleep=slept.append,
        )

    assert slept == []


def test_poll_once_threads_the_injected_session_and_gate_to_the_handler() -> None:
    # A photo passing the gate mutates *this* store: proof the loop uses the
    # injected store rather than silently building its own.
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True)])
    transport = FakeTransport(
        batches=[[photo_update(45, chat_id=45)]],
        files={"abc123": ("photos/p.jpg", b"portrait")},
    )

    poll_once(

        transport,

        offset=0,

        session=store,

        gate=gate,

        llm=FakeLLM(),

        converter=FakeConverter(),
        scripter=FakeScripter(),

    )

    state = store.get(45)
    assert state.phase is Phase.interviewing
    assert state.photo == b"portrait"


def test_poll_once_threads_the_injected_llm_to_the_handler() -> None:
    # An update during the interview must reach *this* llm: proof the loop uses
    # the injected adapter rather than silently building its own.
    store = SessionStore()
    store.record_pass(chat_id=46, photo=b"portrait")
    store.start_interview(46, question="Q1")
    llm = FakeLLM(questions=["Q1", "Q2", "Q3", "Q4", "Q5"])
    transport = FakeTransport(batches=[[text_update(46, chat_id=46, text="a1")]])

    poll_once(
        transport,
        offset=0,
        session=store,
        gate=FakeGate(),
        llm=llm,
        converter=FakeConverter(),
        scripter=FakeScripter(),
    )

    # Exactly one model call per answer: the pending question is read from the
    # session, never re-derived, so the injected llm only produces Q2.
    assert llm.next_calls == [
        [QaPair(question="Q1", answer="a1")],
    ]
    assert store.get(46).interview == [QaPair(question="Q1", answer="a1")]


def test_run_polling_starts_at_offset_minus_one_to_drop_the_backlog() -> None:
    # On startup we must discard updates that piled up while the bot was down,
    # otherwise every restart re-answers the backlog. Telegram treats the first
    # getUpdates with offset=-1 as "forget everything before the latest update".
    transport = FakeTransport(batches=[[]])

    run_polling(
        transport,
        session=_store(),
        gate=FakeGate(),
        llm=FakeLLM(),
        converter=FakeConverter(),
        scripter=FakeScripter(),
        max_polls=1,
    )

    assert transport.calls_for("getUpdates")[0]["offset"] == -1


def test_run_polling_returns_to_a_valid_offset_after_the_backlog_drop() -> None:
    # -1 is only the first-poll sentinel: if the batch is empty the next poll
    # must use a normal offset, never keep asking for the latest update.
    transport = FakeTransport(batches=[[], []])

    run_polling(
        transport,
        session=_store(),
        gate=FakeGate(),
        llm=FakeLLM(),
        converter=FakeConverter(),
        scripter=FakeScripter(),
        max_polls=2,
    )

    assert [fetch["offset"] for fetch in transport.calls_for("getUpdates")] == [-1, 0]


def test_run_polling_stops_after_max_polls_and_carries_offset() -> None:
    transport = FakeTransport(batches=[[text_update(50, chat_id=1)], []])

    final_offset = run_polling(
        transport,
        offset=0,
        session=_store(),
        gate=FakeGate(),
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
        max_polls=2,
    )

    assert final_offset == 51
    fetches = transport.calls_for("getUpdates")
    assert len(fetches) == 2
    assert fetches[1]["offset"] == 51


def test_run_polling_from_a_given_offset() -> None:
    transport = FakeTransport(batches=[[text_update(60, chat_id=1)]])

    final_offset = run_polling(
        transport,
        offset=60,
        session=_store(),
        gate=FakeGate(),
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
        max_polls=1,
    )

    assert final_offset == 61
    assert transport.calls_for("getUpdates")[0]["offset"] == 60


def test_poll_once_threads_the_injected_converter_to_the_handler() -> None:
    # A finished chat whose portrait was never delivered retries the
    # conversion through the loop: proof poll_once passes the injected
    # converter down rather than silently building its own.
    store = SessionStore()
    store.record_pass(chat_id=47, photo=b"portrait")
    store.start_interview(47, question="Q1")
    for number in range(1, 6):
        store.record_interview_answer(47, f"Q{number}", f"a{number}")
    store.appoint_dossier(
        47, Dossier(summary="S", suggested_animal="Capybara")
    )
    converter = FakeConverter()
    transport = FakeTransport(batches=[[text_update(47, chat_id=47, text="hi")]])

    poll_once(
        transport,
        offset=0,
        session=store,
        gate=FakeGate(),
        llm=FakeLLM(),
        converter=converter,
        scripter=FakeScripter(),
    )

    assert len(transport.photos) == 1
    assert store.get(47).portrait_sent is True
    assert len(converter.calls) == 1


def test_poll_once_threads_the_injected_narrator_to_the_handler() -> None:
    # A finished chat with a delivered script and no audio message drives the
    # Narrator through the loop: proof poll_once passes the injected narrator
    # down rather than silently building its own.
    store = SessionStore()
    store.record_pass(chat_id=48, photo=b"portrait")
    store.start_interview(48, question="Q1")
    for number in range(1, 6):
        store.record_interview_answer(48, f"Q{number}", f"a{number}")
    store.appoint_dossier(
        48, Dossier(summary="S", suggested_animal="Capybara")
    )
    store.mark_portrait_sent(48)
    store.store_script(48, "A dramatic nature documentary script.")
    store.mark_script_sent(48)
    narrator = FakeNarrator()
    transport = FakeTransport(batches=[[text_update(48, chat_id=48, text="hi")]])

    poll_once(
        transport,
        offset=0,
        session=store,
        gate=FakeGate(),
        llm=FakeLLM(),
        converter=FakeConverter(),
        scripter=FakeScripter(),
        narrator=narrator,
    )

    assert narrator.calls == ["A dramatic nature documentary script."]
    assert len(transport.audios) == 1
    assert transport.audios[0]["media_type"] == "audio/mpeg"
    assert transport.audios[0]["filename"] == "narration.mp3"
    assert store.get(48).narrator_sent is True
