"""Regression: the bot token must never appear in formatted log output.

Secrets live in ``.env`` only (see ``SPECS/MISSION.md``). Structured logging
formats whole exception tracebacks, so this locks in that neither the config
loader nor the transport (including the HTTPError/URLError cause chain, where
the token lives inside the request URL) leaks the token into a log line.
"""

from __future__ import annotations

import io
import json
import logging
import sys
import traceback
import urllib.error
from collections.abc import Callable
from typing import Any, cast

import pytest
from conftest import FakeGate, FakeTransport, _RecordingHandler, real_invalid_url

from telegram_documentaries.cli import main
from telegram_documentaries.logging_config import JsonFormatter
from telegram_documentaries.transport import TelegramApiError, UrllibTransport

SECRET_TOKEN = "123456:SECRET-LEAK-CANARY-abcdef"


def _transport_failures() -> dict[str, Callable[[], BaseException]]:
    """One fresh, token-bearing failure per normalised transport branch.

    Each message embeds the token exactly as the real error would: the token
    lives in the request URL (``/bot<token>/...``), so any error whose text
    includes the selector — a real path-validation ``InvalidURL``, a
    ``URLError``/``OSError`` reason, or an ``HTTPError`` body/reason — carries
    it too.
    """
    token_path = f"/bot{SECRET_TOKEN}/getUpdates"
    return {
        "invalid_url": lambda: real_invalid_url(f"{token_path}\ncontrol"),
        "url_error": lambda: urllib.error.URLError(
            f"connection refused: {token_path}"
        ),
        "os_error": lambda: OSError(f"timed out talking to {token_path}"),
        "http_error": lambda: urllib.error.HTTPError(
            f"https://api.telegram.org{token_path}",
            500,
            f"Server Error for {SECRET_TOKEN}",
            cast(Any, None),
            io.BytesIO(
                json.dumps(
                    {
                        "ok": False,
                        "error_code": 500,
                        "description": f"boom for {SECRET_TOKEN}",
                    }
                ).encode()
            ),
        ),
        "non_urllib": lambda: ValueError(f"unknown url type: {token_path}"),
    }


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


@pytest.mark.parametrize(
    "error",
    [
        # A malformed URL surfaces as a plain ValueError that embeds the URL
        # (and therefore the token) — it must not escape raw.
        ValueError(
            f"unknown url type: https://api.telegram.org/bot{SECRET_TOKEN}/getUpdates"
        ),
        RuntimeError(f"transport exploded for token {SECRET_TOKEN}"),
    ],
)
def test_unexpected_urlopen_error_is_normalised_and_never_leaks_the_token(
    error: BaseException,
) -> None:
    transport = UrllibTransport(SECRET_TOKEN, urlopen=_RaisingUrlopen(error))

    try:
        transport.call("getUpdates", {"offset": -1})
    except TelegramApiError as exc:
        message = str(exc)
        line = _format_exception(sys.exc_info())
    else:  # pragma: no cover - normalising is the contract
        raise AssertionError("expected an unexpected error to be normalised")

    assert error.__class__.__name__ in message
    assert SECRET_TOKEN not in message
    assert SECRET_TOKEN not in line


def test_unexpected_urlopen_error_on_download_is_normalised_too() -> None:
    error = ValueError(f"unknown url type: https://api.telegram.org/bot{SECRET_TOKEN}")
    transport = UrllibTransport(SECRET_TOKEN, urlopen=_RaisingUrlopen(error))

    with pytest.raises(TelegramApiError) as exc_info:
        transport.download("photos/a.jpg")

    line = "".join(
        traceback.format_exception(
            exc_info.type, exc_info.value, exc_info.tb
        )
    )
    assert exc_info.value.method == "download"
    assert SECRET_TOKEN not in str(exc_info.value)
    assert SECRET_TOKEN not in line


@pytest.mark.parametrize("label", sorted(_transport_failures()))
def test_every_normalised_call_failure_redacts_the_token(label: str) -> None:
    transport = UrllibTransport(
        SECRET_TOKEN, urlopen=_RaisingUrlopen(_transport_failures()[label]())
    )

    with pytest.raises(TelegramApiError) as exc_info:
        transport.call("getUpdates", {"offset": -1})

    error = exc_info.value
    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    json_line = _format_exception((type(error), error, error.__traceback__))

    assert SECRET_TOKEN not in str(error), f"{label} leaked in str(exc)"
    assert SECRET_TOKEN not in error.description, f"{label} leaked in description"
    assert SECRET_TOKEN not in formatted, f"{label} leaked via traceback"
    assert SECRET_TOKEN not in json_line, f"{label} leaked via JsonFormatter"
    # The raw cause is suppressed so the token-bearing original never appears in
    # a formatted traceback.
    assert error.__cause__ is None, f"{label} leaked via __cause__"
    assert error.__suppress_context__ is True, f"{label} leaked via __context__"


@pytest.mark.parametrize("label", sorted(_transport_failures()))
def test_every_normalised_download_failure_redacts_the_token(label: str) -> None:
    transport = UrllibTransport(
        SECRET_TOKEN, urlopen=_RaisingUrlopen(_transport_failures()[label]())
    )

    with pytest.raises(TelegramApiError) as exc_info:
        transport.download("photos/a.jpg")

    error = exc_info.value
    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    json_line = _format_exception((type(error), error, error.__traceback__))

    assert error.method == "download"
    assert SECRET_TOKEN not in str(error), f"{label} leaked in str(exc)"
    assert SECRET_TOKEN not in error.description, f"{label} leaked in description"
    assert SECRET_TOKEN not in formatted, f"{label} leaked via traceback"
    assert SECRET_TOKEN not in json_line, f"{label} leaked via JsonFormatter"
    assert error.__cause__ is None, f"{label} leaked via __cause__"
    assert error.__suppress_context__ is True, f"{label} leaked via __context__"


def test_token_never_appears_in_captured_log_records_with_exc_info() -> None:
    error = real_invalid_url(f"/bot{SECRET_TOKEN}/getUpdates\ncontrol")
    transport = UrllibTransport(SECRET_TOKEN, urlopen=_RaisingUrlopen(error))

    records: list[logging.LogRecord] = []
    handler = _RecordingHandler(records)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("token.redaction.records")
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        try:
            transport.call("getUpdates", {"offset": -1})
        except TelegramApiError:
            # Mirrors production: a caller logs the failure with its traceback.
            logger.error("caller logged the failure", exc_info=True)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    assert records, "the caller's error must be logged"
    lines = [JsonFormatter().format(record) for record in records]
    assert all(SECRET_TOKEN not in line for line in lines)


def test_token_with_a_control_character_is_still_redacted() -> None:
    # A control character *inside* the token makes urlopen's InvalidURL
    # repr-escape the selector, so the raw token no longer appears verbatim in
    # the message. Redaction must still remove its escaped form.
    token = "123456:SECRET-LEAK-\nCANARY-abcdef"
    transport = UrllibTransport(
        token,
        urlopen=_RaisingUrlopen(real_invalid_url(f"/bot{token}/sendMessage")),
    )

    with pytest.raises(TelegramApiError) as exc_info:
        transport.call("sendMessage", {"chat_id": 1, "text": "x"})

    error = exc_info.value
    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    json_line = _format_exception((type(error), error, error.__traceback__))

    assert "CANARY" not in str(error)
    assert "CANARY" not in error.description
    assert "CANARY" not in formatted
    assert "CANARY" not in json_line
    assert error.__cause__ is None


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
            environ={
                "TELEGRAM_BOT_TOKEN": SECRET_TOKEN,
                "GEMINI_API_KEY": "gemini-key",
            },
            env_file=None,
            transport_factory=lambda token: transport,
            gate_factory=lambda api_key, model: FakeGate(),
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
            environ={
                "TELEGRAM_BOT_TOKEN": SECRET_TOKEN,
                "GEMINI_API_KEY": "gemini-key",
            },
            env_file=None,
            transport_factory=lambda token: FakeTransport(batches=[[]]),
            gate_factory=lambda api_key, model: FakeGate(),
        )
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)

    assert exit_code == 0
    lines = [JsonFormatter().format(record) for record in records]
    assert lines
    assert all(SECRET_TOKEN not in line for line in lines)
