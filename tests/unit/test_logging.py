"""Behaviour of the structured (JSON-lines) logging support."""

from __future__ import annotations

import json
import logging

from conftest import _RecordingHandler

from telegram_documentaries.logging_config import SKIPPED, JsonFormatter, log_lifecycle


def make_record(
    logger_name: str = "telegram_documentaries.test",
    level: int = logging.INFO,
    message: str = "something happened",
    **extra: object,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name=logger_name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_formatter_emits_one_json_object_with_core_fields() -> None:
    line = JsonFormatter().format(make_record(message="reply sent", level=logging.INFO))

    payload = json.loads(line)
    assert payload["message"] == "reply sent"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "telegram_documentaries.test"
    assert payload["ts"].endswith("Z") or "+" in payload["ts"]


def test_formatter_includes_structured_extra_fields() -> None:
    line = JsonFormatter().format(
        make_record(message="reply sent", chat_id=42, update_id=7)
    )

    payload = json.loads(line)
    assert payload["chat_id"] == 42
    assert payload["update_id"] == 7


def test_formatter_omits_irrelevant_log_record_internals() -> None:
    line = JsonFormatter().format(make_record(message="hello"))

    payload = json.loads(line)
    assert "args" not in payload
    assert "exc_info" not in payload
    assert "pathname" not in payload


def test_formatter_serialises_exception_info() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="t",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )

    payload = json.loads(JsonFormatter().format(record))

    assert "ValueError: boom" in payload["exc"]


def test_formatter_does_not_explode_on_non_serialisable_extra_values() -> None:
    class Weird:
        def __str__(self) -> str:
            return "weird-value"

    line = JsonFormatter().format(make_record(message="x", thing=Weird()))

    assert json.loads(line)["thing"] == "weird-value"


def test_log_lifecycle_logs_success_and_propagates_return_value() -> None:
    logger = logging.getLogger("lifecycle-green")
    records: list[logging.LogRecord] = []
    handler = _RecordingHandler(records)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    @log_lifecycle(logger, "do_thing")
    def do_thing(value: int) -> int:
        return value * 2

    assert do_thing(21) == 42
    events = [getattr(record, "event", None) for record in records]
    assert "do_thing" in events
    assert all(getattr(r, "event", None) == "do_thing" for r in records)


def test_log_lifecycle_logs_degraded_when_the_callable_returns_false() -> None:
    logger = logging.getLogger("lifecycle-degraded")
    records: list[logging.LogRecord] = []
    handler = _RecordingHandler(records)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    @log_lifecycle(logger, "do_thing")
    def do_thing() -> bool:
        return False

    assert do_thing() is False
    stages = [getattr(record, "stage", None) for record in records]
    assert "success" not in stages
    degraded = [r for r in records if getattr(r, "stage", None) == "degraded"]
    assert degraded, "a False result must be logged as degraded"
    assert degraded[0].levelno == logging.WARNING
    assert getattr(degraded[0], "event") == "do_thing"


def test_log_lifecycle_logs_the_skipped_sentinel_at_info_not_degraded() -> None:
    # A deliberate, benign no-op is not a degraded outcome: it must be logged
    # as ``skipped`` at INFO so it never produces a false operational warning.
    logger = logging.getLogger("lifecycle-skipped")
    records: list[logging.LogRecord] = []
    handler = _RecordingHandler(records)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    @log_lifecycle(logger, "handle_update")
    def handle_update() -> object:
        return SKIPPED

    assert handle_update() is SKIPPED
    stages = [getattr(record, "stage", None) for record in records]
    assert "skipped" in stages
    assert "degraded" not in stages
    assert "success" not in stages
    skipped = [r for r in records if getattr(r, "stage", None) == "skipped"]
    assert skipped[0].levelno == logging.INFO
    assert getattr(skipped[0], "event") == "handle_update"


def test_log_lifecycle_treats_zero_as_success_not_degraded() -> None:
    # ``run_polling`` returns an int offset, which is legitimately 0 on the
    # first empty poll; only the boolean ``False`` signals a degraded result.
    logger = logging.getLogger("lifecycle-zero")
    records: list[logging.LogRecord] = []
    handler = _RecordingHandler(records)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    @log_lifecycle(logger, "run_polling")
    def run_polling() -> int:
        return 0

    assert run_polling() == 0
    stages = [getattr(record, "stage", None) for record in records]
    assert "success" in stages
    assert "degraded" not in stages


def test_log_lifecycle_logs_failure_and_reraises() -> None:
    logger = logging.getLogger("lifecycle-red")
    records: list[logging.LogRecord] = []
    handler = _RecordingHandler(records)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    @log_lifecycle(logger, "do_thing")
    def do_thing() -> int:
        raise RuntimeError("nope")

    try:
        do_thing()
    except RuntimeError as exc:
        assert str(exc) == "nope"
    else:  # pragma: no cover - the raise is the contract
        raise AssertionError("lifecycle decorator swallowed the exception")

    failures = [r for r in records if r.levelno >= logging.ERROR]
    assert failures, "expected a failure record"
    assert failures[0].exc_info is not None
    assert getattr(failures[0], "event") == "do_thing"
