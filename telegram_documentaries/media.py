"""Photo intake: choose the best resolution and fetch its bytes safely.

Telegram delivers a compressed photo as an array of ``PhotoSize`` resolutions
of the *same* image. We pick the largest (best quality) and fetch it via
``getFile`` → ``download``. Everything here treats Telegram's responses as
untrusted: the returned ``file_path`` is validated as a **safe relative path**
before any URL is built, so a hostile path cannot escape the API host.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any
from urllib import parse as urllib_parse

from pydantic import ValidationError

from telegram_documentaries.models import FileRef, Message, PhotoSize
from telegram_documentaries.transport import TelegramApiError, Transport

# A URI scheme (``http:``, ``file:``) or a Windows drive letter (``C:``).
_SCHEME_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")


class InvalidFilePath(TelegramApiError):
    """``getFile`` returned a missing or unsafe ``file_path``.

    Subclasses ``TelegramApiError`` so callers already degrading on transport
    failures handle it the same way, without a second catch clause.
    """

    def __init__(self, file_path: object) -> None:
        super().__init__("getFile", f"unsafe file_path: {file_path!r}")
        self.file_path = file_path


def largest_photo(message: Message) -> PhotoSize | None:
    """Return the highest-resolution photo on ``message``, or ``None``.

    Largest is by pixel area (``width * height``), with ``file_size`` as the
    tie-break so two same-area sizes resolve to the heavier file. A missing
    ``file_size`` counts as the smallest in a tie.
    """
    photos = message.photo
    if not photos:
        return None
    return max(
        photos,
        key=lambda photo: (photo.width * photo.height, photo.file_size or 0),
    )


def fetch_photo(transport: Transport, photo: PhotoSize) -> bytes:
    """``getFile`` ``photo``, validate the path, and download its bytes."""
    payload = transport.call("getFile", {"file_id": photo.file_id})
    result: Any = payload.get("result") if isinstance(payload, dict) else None
    try:
        file_ref = FileRef.model_validate(result)
    except ValidationError as exc:
        raise InvalidFilePath(None) from exc

    file_path = file_ref.file_path
    if file_path is None or not _is_safe_relative_path(file_path):
        raise InvalidFilePath(file_path)

    return transport.download(file_path)


def _is_safe_relative_path(path: str) -> bool:
    """True only for a non-empty, relative path with no traversal or scheme.

    Telegram's ``file_path`` is untrusted, so it is percent-decoded (repeatedly,
    to defeat double-encoding such as ``%252e%252e``) *before* validating. This
    rejects absolute paths (``/etc``), Windows/UNC paths and drive letters
    (``C:\\``, ``\\\\host``), URI schemes (``http:``, ``file:``), NUL bytes,
    whitespace-only paths, and any ``.`` or ``..`` segment — whether literal
    (``../x``, ``a/./b``) or encoded (``%2e%2e/x``).
    """
    path = _fully_unquote(path)
    if not path.strip():
        return False
    if "\x00" in path or "\\" in path:
        return False
    if path.startswith("/") or _SCHEME_PREFIX.match(path):
        return False
    # Check the raw segments too: PurePosixPath silently collapses "." and
    # empty segments, which would hide a traversal attempt from `parts`.
    if any(part in {".", ".."} for part in path.split("/")):
        return False
    pure = PurePosixPath(path)
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        return False
    return True


def _fully_unquote(path: str) -> str:
    """Percent-decode ``path`` until it stops changing (bounded loop)."""
    for _ in range(5):
        decoded = urllib_parse.unquote(path)
        if decoded == path:
            break
        path = decoded
    return path
