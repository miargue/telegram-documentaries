"""Regression: the bot token must never appear in formatted log output.

Secrets live in ``.env`` only (see ``SPECS/MISSION.md``). Structured logging
formats whole exception tracebacks, so this locks in that neither the config
loader nor the transport (including the HTTPError/URLError cause chain, where
the token lives inside the request URL) leaks the token into a log line.
"""

from __future__ import annotations

import io
import logging
import sys
import urllib.error
from typing import Any, cast

import pytest
from conftest import FakeTransport, _RecordingHandler

from telegram_documentaries.cli import main
from telegram_documentaries.logging_config import JsonFormatter
from telegram_documentaries.transport import TelegramApiError, UrllibTransport

SECRET_TOKEN = "123456:SECRET-LEAK-CANARY-abcdef"


class _RaisingUrlopen:
    """urlopen double that always fails with a pre-built exception."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    def __call__(self, request: Any, timeout: float) -> Any:
        raise self.error


def _http_error() -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        f"https://api.telegram.org/bot{SECRET_TOKEN}/getUpdates",
        401,
        "Unauthorized",
        cast(Any, None),
        io.BytesIO(b'{"ok": false, "description": "Unauthorized"}'),
    )


def _format_exception(exc_info: Any) -> str:
    record = logging.LogRecord(
        name="token.redaction",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="boom",
        args=(),
        exc_info=exc_info,
    )
    return JsonFormatter().format(record)


@pytest.mark.parametrize("error", [_http_error(), urllib.error.URLError("refused")])
def test_token_never_appears_in_formatted_transport_error_tracebacks(
    error: BaseException,
) -> None:
    transport = UrllibTransport(SECRET_TOKEN, urlopen=_RaisingUrlopen(error))

    try:
        transport.call("getUpdates", {"offset": -1})
    except TelegramApiError:
        line = _format_exception(sys.exc_info())
    else:  # pragma: no cover - the raise is the contract
        raise AssertionError("expected a normalised TelegramApiError")

    assert SECRET_TOKEN not in line


def test_token_never_appears_in_any_formatted_log_line_during_startup_failure() -> None:
    transport = UrllibTransport(SECRET_TOKEN, urlopen=_RaisingUrlopen(_http_error()))
    records: list[logging.LogRecord] = []
    handler = _RecordingHandler(records)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        exit_code = main(
            ["--max-polls", "1"],
            environ={"TELEGRAM_BOT_TOKEN": SECRET_TOKEN},
            env_file=None,
            transport_factory=lambda token: transport,
        )
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)

    assert exit_code == 1
    lines = [JsonFormatter().format(record) for record in records]
    assert lines, "the startup failure must be logged"
    assert all(SECRET_TOKEN not in line for line in lines)


def test_token_never_appears_in_a_healthy_startup_log() -> None:
    records: list[logging.LogRecord] = []
    handler = _RecordingHandler(records)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        exit_code = main(
            ["--max-polls", "1"],
            environ={"TELEGRAM_BOT_TOKEN": SECRET_TOKEN},
            env_file=None,
            transport_factory=lambda token: FakeTransport(batches=[[]]),
        )
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)

    assert exit_code == 0
    lines = [JsonFormatter().format(record) for record in records]
    assert lines
    assert all(SECRET_TOKEN not in line for line in lines)
