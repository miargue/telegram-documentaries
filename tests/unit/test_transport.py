"""Behaviour of the stdlib-urllib transport.

``urlopen`` is injected so every case — success, Telegram error payloads,
HTTP failures, network failures, garbage bodies — runs without a live bot
or any socket.
"""

from __future__ import annotations

import http.client
import io
import json
import re
import urllib.error
from typing import Any, cast

import pytest
from conftest import real_invalid_url

from telegram_documentaries.transport import (
    MAX_MESSAGE_LENGTH,
    TelegramApiError,
    UrllibTransport,
    _multipart,
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
        self.headers: list[dict[str, str]] = []

    def __call__(self, request: Any, timeout: float) -> FakeResponse:
        self.requests.append((request.full_url, request.data, timeout))
        self.methods.append(request.get_method())
        self.headers.append(dict(request.headers))
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


# ------------------------------------------------------------ photo upload
#
# ``sendPhoto`` is the only multipart call: the generated hybrid portrait is
# uploaded as a file part (the user's original photo is never sent). The body
# is built by the pure ``_multipart`` helper so its shape is unit-testable
# without a socket, and it goes through the same ``_read``/``_decode``
# normalisation (and token redaction) as every other transport call.

BOUNDARY = "test-boundary-1234567890"
PHOTO_BYTES = b"\xff\xd8\xff\xe0hybrid-portrait-bytes"


def parse_multipart(
    body: bytes, boundary: str
) -> list[tuple[dict[str, str], bytes]]:
    """Parse a multipart/form-data body back into ``(headers, content)`` pairs."""
    delimiter = b"--" + boundary.encode("ascii")
    parts: list[tuple[dict[str, str], bytes]] = []
    for chunk in body.split(delimiter)[1:]:
        if chunk.startswith(b"--"):
            break
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        header_blob, _, content = chunk.partition(b"\r\n\r\n")
        if content.endswith(b"\r\n"):
            content = content[:-2]
        headers: dict[str, str] = {}
        for line in header_blob.split(b"\r\n"):
            name, _, value = line.decode("utf-8").partition(": ")
            headers[name.lower()] = value
        parts.append((headers, content))
    return parts


def disposition_param(header: str, param: str) -> str | None:
    match = re.search(rf'{re.escape(param)}="([^"]*)"', header)
    return match.group(1) if match else None


def header(request_headers: dict[str, str], name: str) -> str | None:
    """Case-insensitive header lookup (urllib capitalises the names)."""
    for key, value in request_headers.items():
        if key.lower() == name.lower():
            return value
    return None


def multipart_boundary(request_headers: dict[str, str]) -> str:
    """Extract the boundary the request announces in its ``Content-Type``."""
    content_type = header(request_headers, "content-type")
    assert content_type is not None
    assert content_type.startswith("multipart/form-data; boundary=")
    return content_type.split("boundary=")[1]


def test_multipart_renders_text_fields_then_the_file_part() -> None:
    body = _multipart(
        boundary=BOUNDARY,
        fields={"chat_id": "5", "caption": "a badger"},
        file_field="photo",
        filename="portrait",
        content_type="image/png",
        content=PHOTO_BYTES,
    )

    assert body.startswith(f"--{BOUNDARY}\r\n".encode())
    assert body.endswith(f"--{BOUNDARY}--\r\n".encode())

    parts = parse_multipart(body, BOUNDARY)
    assert len(parts) == 3

    chat_headers, chat_content = parts[0]
    assert chat_headers["content-disposition"] == 'form-data; name="chat_id"'
    assert chat_content == b"5"

    caption_headers, caption_content = parts[1]
    assert caption_headers["content-disposition"] == 'form-data; name="caption"'
    assert caption_content == b"a badger"

    photo_headers, photo_content = parts[2]
    assert disposition_param(
        photo_headers["content-disposition"], "name"
    ) == "photo"
    assert disposition_param(
        photo_headers["content-disposition"], "filename"
    ) == "portrait"
    assert photo_headers["content-type"] == "image/png"
    assert photo_content == PHOTO_BYTES


def test_multipart_keeps_binary_content_byte_exact() -> None:
    # NUL bytes and CRLFs must survive untouched; only the transport-added
    # trailing CRLF may be stripped when parsing.
    content = b"\x00\r\n\xff\xfe\x00END\r\n"
    body = _multipart(
        boundary=BOUNDARY,
        fields={"chat_id": "1"},
        file_field="photo",
        filename="portrait",
        content_type="image/jpeg",
        content=content,
    )

    parts = parse_multipart(body, BOUNDARY)

    assert parts[-1][1] == content


def test_send_photo_posts_a_multipart_body_to_sendphoto() -> None:
    transport, opener = make_transport(
        [FakeResponse(b'{"ok": true, "result": {"message_id": 9}}')]
    )

    result = transport.send_photo(
        5, PHOTO_BYTES, media_type="image/png", caption="meet your badger"
    )

    url, body, _timeout = opener.requests[0]
    assert url == "https://api.telegram.org/botTEST-TOKEN/sendPhoto"
    assert opener.methods[0] == "POST"
    assert body is not None
    boundary = multipart_boundary(opener.headers[0])

    parts = parse_multipart(body, boundary)
    assert len(parts) == 3
    fields = {
        headers["content-disposition"]: content for headers, content in parts[:2]
    }
    assert fields['form-data; name="chat_id"'] == b"5"
    assert fields['form-data; name="caption"'] == b"meet your badger"

    photo_headers, photo_content = parts[2]
    assert disposition_param(photo_headers["content-disposition"], "name") == "photo"
    assert photo_headers["content-type"] == "image/png"
    assert photo_content == PHOTO_BYTES
    assert result == {"ok": True, "result": {"message_id": 9}}


def test_send_photo_omits_the_caption_field_when_absent() -> None:
    transport, opener = make_transport([FakeResponse(b'{"ok": true, "result": {}}')])

    transport.send_photo(5, PHOTO_BYTES, media_type="image/jpeg")

    _url, body, _timeout = opener.requests[0]
    assert body is not None
    boundary = multipart_boundary(opener.headers[0])
    parts = parse_multipart(body, boundary)

    assert [h["content-disposition"] for h, _ in parts] == [
        'form-data; name="chat_id"',
        'form-data; name="photo"; filename="portrait"',
    ]


def test_send_photo_uses_a_stable_filename_and_the_given_content_type() -> None:
    transport, opener = make_transport([FakeResponse(b'{"ok": true}')])

    transport.send_photo(7, PHOTO_BYTES, media_type="image/webp")

    _url, body, _timeout = opener.requests[0]
    assert body is not None
    boundary = multipart_boundary(opener.headers[0])
    photo_headers, _ = parse_multipart(body, boundary)[-1]

    assert disposition_param(photo_headers["content-disposition"], "filename") == (
        "portrait"
    )
    assert photo_headers["content-type"] == "image/webp"


def test_send_photo_raises_when_the_api_reports_not_ok() -> None:
    body = json.dumps(
        {"ok": False, "error_code": 413, "description": "request entity too large"}
    ).encode()
    transport, _ = make_transport([FakeResponse(body)])

    with pytest.raises(TelegramApiError) as exc_info:
        transport.send_photo(1, PHOTO_BYTES, media_type="image/png")

    error = exc_info.value
    assert error.method == "sendPhoto"
    assert error.description == "request entity too large"
    assert error.error_code == 413


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError("connection refused"),
        urllib.error.HTTPError(
            "https://api.telegram.org/botTEST-TOKEN/sendPhoto",
            502,
            "Bad Gateway",
            cast(Any, None),
            io.BytesIO(b'{"ok": false, "description": "bad gateway"}'),
        ),
        http.client.IncompleteRead(b"half a body"),
        TimeoutError("timed out"),
    ],
)
def test_send_photo_normalises_every_transport_failure(
    failure: BaseException,
) -> None:
    transport, _ = make_transport([failure])

    with pytest.raises(TelegramApiError) as exc_info:
        transport.send_photo(1, PHOTO_BYTES, media_type="image/png")

    error = exc_info.value
    assert error.method == "sendPhoto"
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def test_send_photo_never_leaks_the_token_in_a_failure() -> None:
    endpoint = "https://api.telegram.org/botTEST-TOKEN/sendPhoto"
    transport, _ = make_transport(
        [urllib.error.URLError(f"connection refused: {endpoint}")]
    )

    with pytest.raises(TelegramApiError) as exc_info:
        transport.send_photo(1, PHOTO_BYTES, media_type="image/png")

    error = exc_info.value
    assert "TEST-TOKEN" not in str(error)
    assert "***" in str(error)


def test_send_photo_redacts_a_control_character_token() -> None:
    token = "TEST-TOKEN-\nCANARY"
    opener = RecordingUrlopen(
        [real_invalid_url(f"/bot{token}/sendPhoto")]
    )
    transport = UrllibTransport(token, urlopen=opener)

    with pytest.raises(TelegramApiError) as exc_info:
        transport.send_photo(1, PHOTO_BYTES, media_type="image/png")

    error = exc_info.value
    assert "CANARY" not in str(error)
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    # The escaped form (``\n`` spelled out) must be redacted too.
    assert "CANARY" not in error.description.encode("unicode_escape").decode()


# ------------------------------------------------------------ voice upload
#
# ``sendVoice`` is the Phase 6 delivery call: the raw OGG synthesised by Gemini
# is uploaded as a file part (field ``voice``). It reuses the pure
# ``_multipart`` helper and the same ``_read``/``_decode`` normalisation (and
# token redaction) as ``send_photo``.

VOICE_BYTES = b"OggS\x00\x02narration-voice-bytes"


def test_send_voice_posts_a_multipart_body_to_sendvoice() -> None:
    transport, opener = make_transport(
        [FakeResponse(b'{"ok": true, "result": {"message_id": 11}}')]
    )

    result = transport.send_voice(5, VOICE_BYTES, media_type="audio/ogg")

    url, body, _timeout = opener.requests[0]
    assert url == "https://api.telegram.org/botTEST-TOKEN/sendVoice"
    assert opener.methods[0] == "POST"
    assert body is not None
    boundary = multipart_boundary(opener.headers[0])

    parts = parse_multipart(body, boundary)
    assert len(parts) == 2
    fields = {
        headers["content-disposition"]: content for headers, content in parts[:1]
    }
    assert fields['form-data; name="chat_id"'] == b"5"

    voice_headers, voice_content = parts[1]
    assert disposition_param(voice_headers["content-disposition"], "name") == "voice"
    assert (
        disposition_param(voice_headers["content-disposition"], "filename")
        == "narration.ogg"
    )
    assert voice_headers["content-type"] == "audio/ogg"
    assert voice_content == VOICE_BYTES
    assert result == {"ok": True, "result": {"message_id": 11}}


def test_send_voice_uses_a_custom_filename_and_content_type() -> None:
    transport, opener = make_transport([FakeResponse(b'{"ok": true}')])

    transport.send_voice(
        7, VOICE_BYTES, media_type="audio/ogg; codecs=opus", filename="story.ogg"
    )

    _url, body, _timeout = opener.requests[0]
    assert body is not None
    boundary = multipart_boundary(opener.headers[0])
    voice_headers, _ = parse_multipart(body, boundary)[-1]

    assert (
        disposition_param(voice_headers["content-disposition"], "filename")
        == "story.ogg"
    )
    assert voice_headers["content-type"] == "audio/ogg; codecs=opus"


def test_send_voice_raises_when_the_api_reports_not_ok() -> None:
    body = json.dumps(
        {"ok": False, "error_code": 400, "description": "VOICE_MESSAGES_FORBIDDEN"}
    ).encode()
    transport, _ = make_transport([FakeResponse(body)])

    with pytest.raises(TelegramApiError) as exc_info:
        transport.send_voice(1, VOICE_BYTES, media_type="audio/ogg")

    error = exc_info.value
    assert error.method == "sendVoice"
    assert error.description == "VOICE_MESSAGES_FORBIDDEN"
    assert error.error_code == 400


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError("connection refused"),
        urllib.error.HTTPError(
            "https://api.telegram.org/botTEST-TOKEN/sendVoice",
            502,
            "Bad Gateway",
            cast(Any, None),
            io.BytesIO(b'{"ok": false, "description": "bad gateway"}'),
        ),
        http.client.IncompleteRead(b"half a body"),
        TimeoutError("timed out"),
    ],
)
def test_send_voice_normalises_every_transport_failure(
    failure: BaseException,
) -> None:
    transport, _ = make_transport([failure])

    with pytest.raises(TelegramApiError) as exc_info:
        transport.send_voice(1, VOICE_BYTES, media_type="audio/ogg")

    error = exc_info.value
    assert error.method == "sendVoice"
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def test_send_voice_never_leaks_the_token_in_a_failure() -> None:
    endpoint = "https://api.telegram.org/botTEST-TOKEN/sendVoice"
    transport, _ = make_transport(
        [urllib.error.URLError(f"connection refused: {endpoint}")]
    )

    with pytest.raises(TelegramApiError) as exc_info:
        transport.send_voice(1, VOICE_BYTES, media_type="audio/ogg")

    error = exc_info.value
    assert "TEST-TOKEN" not in str(error)
    assert "***" in str(error)


def test_send_voice_redacts_a_control_character_token() -> None:
    token = "TEST-TOKEN-\nCANARY"
    opener = RecordingUrlopen([real_invalid_url(f"/bot{token}/sendVoice")])
    transport = UrllibTransport(token, urlopen=opener)

    with pytest.raises(TelegramApiError) as exc_info:
        transport.send_voice(1, VOICE_BYTES, media_type="audio/ogg")

    error = exc_info.value
    assert "CANARY" not in str(error)
    assert error.__cause__ is None
    assert error.__suppress_context__ is True
    assert "CANARY" not in error.description.encode("unicode_escape").decode()
