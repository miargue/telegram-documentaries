"""Behaviour of the stdlib-urllib transport.

``urlopen`` is injected so every case — success, Telegram error payloads,
HTTP failures, network failures, garbage bodies — runs without a live bot
or any socket.
"""

from __future__ import annotations

import http.client
import io
import json
import urllib.error
from typing import Any, cast

import pytest

from telegram_documentaries.transport import (
    MAX_MESSAGE_LENGTH,
    TelegramApiError,
    UrllibTransport,
    message_length,
    truncate_message,
)


class FakeResponse:
    """Stand-in for the object returned by ``urllib.request.urlopen``."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class RecordingUrlopen:
    """Records requests and replays scripted responses (in order)."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, bytes | None, float]] = []
        self.methods: list[str] = []

    def __call__(self, request: Any, timeout: float) -> FakeResponse:
        self.requests.append((request.full_url, request.data, timeout))
        self.methods.append(request.get_method())
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def make_transport(
    responses: list[Any], **kwargs: Any
) -> tuple[UrllibTransport, RecordingUrlopen]:
    opener = RecordingUrlopen(responses)
    transport = UrllibTransport("TEST-TOKEN", urlopen=opener, **kwargs)
    return transport, opener


def test_call_posts_to_bot_token_url_and_returns_result() -> None:
    payload = {"ok": True, "result": {"message_id": 1}}
    transport, opener = make_transport([FakeResponse(json.dumps(payload).encode())])

    result = transport.call("sendMessage", {"chat_id": 5, "text": "hey mate!"})

    url, body, _timeout = opener.requests[0]
    assert url == "https://api.telegram.org/botTEST-TOKEN/sendMessage"
    assert result == payload
    assert body is not None
    assert b"chat_id=5" in body
    assert b"hey+mate%21" in body


def test_call_sends_params_as_form_urlencoded_body() -> None:
    transport, opener = make_transport(
        [FakeResponse(b'{"ok": true, "result": []}')]
    )

    transport.call("getUpdates", {"offset": 41, "timeout": 10})

    _url, body, timeout = opener.requests[0]
    assert body == b"offset=41&timeout=10"
    assert timeout == pytest.approx(30.0)


def test_call_raises_telegram_api_error_when_ok_is_false() -> None:
    body = json.dumps(
        {"ok": False, "error_code": 403, "description": "bot was blocked"}
    ).encode()
    transport, _ = make_transport([FakeResponse(body)])

    with pytest.raises(TelegramApiError) as exc_info:
        transport.call("sendMessage", {"chat_id": 1, "text": "x"})

    error = exc_info.value
    assert error.method == "sendMessage"
    assert error.description == "bot was blocked"
    assert error.error_code == 403


def test_call_raises_telegram_api_error_on_http_error_response() -> None:
    http_error = urllib.error.HTTPError(
        "https://api.telegram.org/botTEST-TOKEN/getUpdates",
        502,
        "Bad Gateway",
        cast(Any, None),
        io.BytesIO(b"<html>bad gateway</html>"),
    )
    transport, _ = make_transport([http_error])

    with pytest.raises(TelegramApiError) as exc_info:
        transport.call("getUpdates", {"offset": 0})

    assert exc_info.value.status_code == 502
    assert exc_info.value.method == "getUpdates"


def test_call_raises_telegram_api_error_on_network_failure() -> None:
    transport, _ = make_transport(
        [urllib.error.URLError("connection refused")]
    )

    with pytest.raises(TelegramApiError) as exc_info:
        transport.call("getUpdates", {"offset": 0})

    error = exc_info.value
    # The raw cause is suppressed (``from None``) so it can never surface in a
    # formatted traceback; the original type is kept in the message instead.
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert "URLError" in str(error)


def test_call_normalises_incomplete_read_from_a_dropped_connection() -> None:
    # A long-poll connection dropped mid-body raises http.client.IncompleteRead
    # out of response.read() — it must come back as TelegramApiError, not raw.
    transport, _ = make_transport(
        [http.client.IncompleteRead(b"half a response")]
    )

    with pytest.raises(TelegramApiError) as exc_info:
        transport.call("getUpdates", {"offset": 0})

    error = exc_info.value
    assert error.method == "getUpdates"
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert "IncompleteRead" in str(error)


def test_call_normalises_bad_status_line_from_a_garbled_response() -> None:
    transport, _ = make_transport([http.client.BadStatusLine("\x00junk")])

    with pytest.raises(TelegramApiError) as exc_info:
        transport.call("getUpdates", {"offset": 0})

    error = exc_info.value
    assert error.method == "getUpdates"
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert "BadStatusLine" in str(error)


def test_call_normalises_any_http_client_exception() -> None:
    # Category lock-in: every http.client.HTTPException subclass is normalised,
    # not just the two that happen to have been reported.

    class ProtocolFailure(http.client.HTTPException):
        pass

    transport, _ = make_transport([ProtocolFailure("protocol blew up")])

    with pytest.raises(TelegramApiError) as exc_info:
        transport.call("sendMessage", {"chat_id": 1, "text": "x"})

    error = exc_info.value
    assert error.method == "sendMessage"
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert "ProtocolFailure" in str(error)


def test_call_normalises_protocol_error_while_reading_the_error_body() -> None:
    # Sibling of the reported bug: reading the HTTP *error* body can itself
    # hit a dropped connection and raise out of the HTTPError handler.

    class UnreadableBody:
        def read(self, *_args: Any, **_kwargs: Any) -> bytes:
            raise http.client.IncompleteRead(b"half a body")

        def close(self) -> None:
            return None

    http_error = urllib.error.HTTPError(
        "https://api.telegram.org/botTEST-TOKEN/getUpdates",
        500,
        "Server Error",
        cast(Any, {}),
        cast(Any, UnreadableBody()),
    )
    transport, _ = make_transport([http_error])

    with pytest.raises(TelegramApiError) as exc_info:
        transport.call("getUpdates", {"offset": 0})

    error = exc_info.value
    assert error.method == "getUpdates"
    assert error.status_code == 500
    # The unreadable body falls back to the HTTP status description instead
    # of letting IncompleteRead escape the HTTPError handler.
    assert error.description == "HTTP 500 Server Error"


def test_call_raises_telegram_api_error_on_invalid_json() -> None:
    transport, _ = make_transport([FakeResponse(b"not json at all")])

    with pytest.raises(TelegramApiError, match="JSON"):
        transport.call("getUpdates", {"offset": 0})


def test_call_raises_telegram_api_error_on_non_object_json() -> None:
    transport, _ = make_transport([FakeResponse(b"[1, 2, 3]")])

    with pytest.raises(TelegramApiError, match="JSON"):
        transport.call("getUpdates", {"offset": 0})


def test_call_raises_on_timeout_style_os_error() -> None:
    transport, _ = make_transport([TimeoutError("timed out")])

    with pytest.raises(TelegramApiError):
        transport.call("getUpdates", {"offset": 0})


def test_http_socket_timeout_outlasts_the_long_poll_hold() -> None:
    # The socket must wait longer than the server-side long-poll window, or
    # every quiet getUpdates would abort before Telegram has a chance to answer.
    from telegram_documentaries.polling import LONG_POLL_TIMEOUT
    from telegram_documentaries.transport import DEFAULT_TIMEOUT

    assert DEFAULT_TIMEOUT >= LONG_POLL_TIMEOUT


def test_download_gets_the_file_url_and_returns_raw_bytes() -> None:
    transport, opener = make_transport([FakeResponse(b"\xff\xd8image-bytes")])

    data = transport.download("photos/file_0.jpg")

    url, body, timeout = opener.requests[0]
    assert url == (
        "https://api.telegram.org/file/botTEST-TOKEN/photos/file_0.jpg"
    )
    assert opener.methods[0] == "GET"
    assert body is None
    assert data == b"\xff\xd8image-bytes"
    assert timeout == pytest.approx(30.0)


def test_download_normalises_an_http_error() -> None:
    http_error = urllib.error.HTTPError(
        "https://api.telegram.org/file/botTEST-TOKEN/photos/missing.jpg",
        404,
        "Not Found",
        cast(Any, None),
        io.BytesIO(b'{"ok": false, "description": "file not found"}'),
    )
    transport, _ = make_transport([http_error])

    with pytest.raises(TelegramApiError) as exc_info:
        transport.download("photos/missing.jpg")

    error = exc_info.value
    assert error.method == "download"
    assert error.status_code == 404
    assert error.description == "file not found"


def test_download_normalises_a_network_failure() -> None:
    transport, _ = make_transport([urllib.error.URLError("connection refused")])

    with pytest.raises(TelegramApiError) as exc_info:
        transport.download("photos/file_0.jpg")

    error = exc_info.value
    assert error.method == "download"
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert "URLError" in str(error)


def test_download_normalises_an_os_error() -> None:
    transport, _ = make_transport([TimeoutError("timed out")])

    with pytest.raises(TelegramApiError):
        transport.download("photos/file_0.jpg")


def test_download_normalises_a_protocol_failure() -> None:
    transport, _ = make_transport([http.client.IncompleteRead(b"half")])

    with pytest.raises(TelegramApiError) as exc_info:
        transport.download("photos/file_0.jpg")

    error = exc_info.value
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert "IncompleteRead" in str(error)


# ---------------------------------------------------- outbound message length
#
# Telegram rejects ``sendMessage`` text longer than 4096 *UTF-16 code units*
# (not Python characters), and the chokepoints rely on these helpers to bound
# every outbound message. The helpers are the mechanism-level guard for the
# whole "over-long outbound text" bug class.


def test_message_length_counts_ascii_characters_as_one_unit() -> None:
    assert message_length("hello") == 5


def test_message_length_counts_an_astral_emoji_as_two_utf16_units() -> None:
    # Telegram counts UTF-16 code units, so a single astral emoji is two units
    # even though Python sees one character.
    assert message_length("😀") == 2
    assert message_length("a😀") == 3


def test_truncate_message_leaves_a_message_within_the_limit_untouched() -> None:
    text = "x" * MAX_MESSAGE_LENGTH

    assert truncate_message(text) == text


def test_truncate_message_trims_ascii_to_the_limit() -> None:
    text = "x" * (MAX_MESSAGE_LENGTH + 100)

    truncated = truncate_message(text)

    assert message_length(truncated) == MAX_MESSAGE_LENGTH
    assert truncated == "x" * MAX_MESSAGE_LENGTH


def test_truncate_message_never_splits_a_surrogate_pair() -> None:
    # 4095 ASCII + one astral emoji = 4097 code units: the cut lands *inside*
    # the emoji's surrogate pair, and the orphaned high surrogate must go.
    text = "a" * (MAX_MESSAGE_LENGTH - 1) + "😀"

    truncated = truncate_message(text)

    assert message_length(truncated) <= MAX_MESSAGE_LENGTH
    assert truncated == "a" * (MAX_MESSAGE_LENGTH - 1)
    # A lone surrogate would make this re-encode raise.
    assert message_length(truncated.encode("utf-16-le").decode("utf-16-le")) == (
        MAX_MESSAGE_LENGTH - 1
    )


def test_truncate_message_trims_an_all_emoji_message_to_whole_units() -> None:
    text = "😀" * (MAX_MESSAGE_LENGTH // 2 + 1)  # 4098 code units

    truncated = truncate_message(text)

    assert message_length(truncated) == MAX_MESSAGE_LENGTH
    assert truncated == "😀" * (MAX_MESSAGE_LENGTH // 2)


def test_truncate_message_accepts_a_custom_limit() -> None:
    assert truncate_message("abcdef", limit=3) == "abc"


def test_truncate_message_with_a_zero_limit_returns_empty() -> None:
    assert truncate_message("anything", limit=0) == ""
