"""Behaviour of photo intake: largest-size selection and safe download.

All I/O goes through ``FakeTransport``; no test touches Telegram.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import FakeTransport

from telegram_documentaries.media import (
    InvalidFilePath,
    fetch_photo,
    largest_photo,
)
from telegram_documentaries.models import Message, PhotoSize
from telegram_documentaries.transport import TelegramApiError


def _size(
    file_id: str,
    width: int,
    height: int,
    file_size: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "file_id": file_id,
        "file_unique_id": f"{file_id}-unique",
        "width": width,
        "height": height,
    }
    if file_size is not None:
        payload["file_size"] = file_size
    return payload


def _message(photo: list[dict[str, Any]]) -> Message:
    return Message.model_validate(
        {"message_id": 1, "chat": {"id": 1}, "photo": photo}
    )


def test_largest_photo_picks_the_biggest_by_pixel_area() -> None:
    message = _message(
        [_size("s", 90, 90), _size("l", 800, 600), _size("m", 320, 240)]
    )

    chosen = largest_photo(message)

    assert chosen is not None
    assert chosen.file_id == "l"


def test_largest_photo_breaks_area_ties_by_file_size() -> None:
    message = _message(
        [_size("a", 100, 100, 500), _size("b", 100, 100, 5000)]
    )

    chosen = largest_photo(message)

    assert chosen is not None
    assert chosen.file_id == "b"


def test_largest_photo_treats_missing_file_size_as_smallest_in_a_tie() -> None:
    message = _message([_size("a", 100, 100), _size("b", 100, 100, 10)])

    chosen = largest_photo(message)

    assert chosen is not None
    assert chosen.file_id == "b"


def test_largest_photo_returns_none_when_the_message_has_no_photo() -> None:
    message = Message.model_validate(
        {"message_id": 1, "chat": {"id": 1}, "text": "hi"}
    )

    assert largest_photo(message) is None


def test_largest_photo_returns_none_for_an_empty_photo_list() -> None:
    assert largest_photo(_message([])) is None


def test_fetch_photo_calls_get_file_then_downloads_the_returned_path() -> None:
    transport = FakeTransport(
        files={"fid": ("photos/file_0.jpg", b"\xff\xd8raw-bytes")}
    )
    photo = PhotoSize.model_validate(_size("fid", 100, 100))

    image = fetch_photo(transport, photo)

    assert image == b"\xff\xd8raw-bytes"
    assert transport.calls_for("getFile") == [{"file_id": "fid"}]
    assert transport.calls_for("download") == [{"file_path": "photos/file_0.jpg"}]


@pytest.mark.parametrize(
    "hostile",
    [
        "..",
        "../etc/passwd",
        "a/../../b",
        "/etc/passwd",
        "/",
        "http://evil.example/x",
        "https://evil.example/x",
        "file:///etc/passwd",
        "",
        "C:\\windows\\system32",
        "a\\b",
        "photos/../../secret",
        # A path is only safe if it survives normalisation; Telegram returning
        # any of these would let a hostile response escape the getFile host.
        ".",
        "./x",
        "a/./b",
        "photos/../secret",  # un-encoded dot segment
        "%2e%2e/x",  # percent-encoded ".."
        "%2e%2e%2fetc/passwd",  # percent-encoded "../"
        "..%2fsecret",
        "photos/%2e%2e/secret",  # encoded ".." buried in the path
        "%252e%252e/x",  # double-encoded ".." (survives one unquote)
        "   ",  # whitespace-only
        "\t",
        "%20%20",  # decodes to whitespace-only
    ],
)
def test_fetch_photo_rejects_a_hostile_file_path_without_downloading(
    hostile: str,
) -> None:
    transport = FakeTransport(files={"fid": (hostile, b"data")})
    photo = PhotoSize.model_validate(_size("fid", 100, 100))

    with pytest.raises(InvalidFilePath):
        fetch_photo(transport, photo)

    assert transport.calls_for("download") == []


def test_fetch_photo_rejects_a_missing_file_path() -> None:
    transport = FakeTransport(files={"fid": (None, b"data")})
    photo = PhotoSize.model_validate(_size("fid", 100, 100))

    with pytest.raises(InvalidFilePath):
        fetch_photo(transport, photo)

    assert transport.calls_for("download") == []


def test_invalid_file_path_is_a_telegram_api_error() -> None:
    # Callers already degrade on TelegramApiError, so reuse that family.
    assert issubclass(InvalidFilePath, TelegramApiError)


def test_fetch_photo_propagates_a_get_file_failure() -> None:
    transport = FakeTransport(
        fail_on={"getFile": TelegramApiError("getFile", "file not found")}
    )
    photo = PhotoSize.model_validate(_size("fid", 100, 100))

    with pytest.raises(TelegramApiError):
        fetch_photo(transport, photo)


def test_fetch_photo_propagates_a_download_failure() -> None:
    transport = FakeTransport(
        files={"fid": ("photos/file_0.jpg", b"data")},
        fail_on={"download": TelegramApiError("download", "network down")},
    )
    photo = PhotoSize.model_validate(_size("fid", 100, 100))

    with pytest.raises(TelegramApiError):
        fetch_photo(transport, photo)
