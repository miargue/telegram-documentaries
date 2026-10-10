"""Shared test helpers: fake transport and raw Telegram payload builders.

Everything here is deterministic and performs no I/O — tests never touch a
live bot. Raw payload builders emit plain dicts exactly as the Telegram Bot
API would, so they can be fed through the real parsing boundary.
"""

from __future__ import annotations

import http.client
import logging
from collections.abc import Mapping
from typing import Any

from telegram_documentaries.session import Dossier, QaPair
from telegram_documentaries.transport import TelegramApiError
from telegram_documentaries.vision import HumanVerdict


def real_invalid_url(selector: str) -> http.client.InvalidURL:
    """Produce a genuine ``http.client.InvalidURL`` via real path validation.

    ``HTTPConnection.putrequest`` runs the same control-character validation as
    the real ``urllib.request.urlopen`` and raises *before* any socket is
    opened. The resulting exception therefore carries the real message format —
    including the whole selector, which is the path through which a token or API
    key embedded in the URL leaks.
    """
    connection = http.client.HTTPConnection("api.telegram.org")
    try:
        connection.putrequest("GET", selector)
    except http.client.InvalidURL as exc:
        return exc
    raise AssertionError("selector must make path validation fail")


class _RecordingHandler(logging.Handler):
    """Test handler that records every emitted record.

    Overriding ``emit`` (rather than monkey-patching the instance method) keeps
    the handler type-correct, so tests need no ``# type: ignore``.
    """

    def __init__(self, records: list[logging.LogRecord]) -> None:
        super().__init__()
        self.records = records

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def text_update(
    update_id: int,
    chat_id: int,
    text: str = "hello",
    *,
    user_id: int | None = None,
) -> dict[str, Any]:
    """A normal incoming text message update."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "date": 1_700_000_000,
            "chat": {"id": chat_id, "type": "private"},
            "from": {
                "id": user_id if user_id is not None else chat_id,
                "is_bot": False,
                "first_name": "Testy",
                "username": "testy",
            },
            "text": text,
        },
    }


def photo_update(
    update_id: int,
    chat_id: int,
    *,
    photos: list[dict[str, Any]] | None = None,
    caption: str | None = None,
    media_group_id: str | None = None,
) -> dict[str, Any]:
    """An incoming photo message.

    ``photos`` defaults to a single size; pass several sizes to model the real
    Telegram array (smaller first). ``caption`` models a photo with a caption.
    ``media_group_id`` models one member of an album: Telegram tags every photo
    in a single send with the same id (as a string).
    """
    if photos is None:
        photos = [
            {
                "file_id": "abc123",
                "file_unique_id": "uniq123",
                "width": 90,
                "height": 90,
                "file_size": 1024,
            }
        ]
    message: dict[str, Any] = {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "date": 1_700_000_000,
            "chat": {"id": chat_id, "type": "private"},
            "photo": photos,
        },
    }
    if caption is not None:
        message["message"]["caption"] = caption
    if media_group_id is not None:
        message["message"]["media_group_id"] = media_group_id
    return message


def document_update(
    update_id: int,
    chat_id: int,
    *,
    kind: str = "document",
) -> dict[str, Any]:
    """An incoming non-photo content message (document/sticker/voice)."""
    content: dict[str, Any]
    if kind == "sticker":
        content = {
            "sticker": {
                "file_id": "s1",
                "file_unique_id": "su1",
                "width": 1,
                "height": 1,
            }
        }
    elif kind == "voice":
        content = {"voice": {"file_id": "v1", "file_unique_id": "vu1", "duration": 2}}
    else:
        content = {
            "document": {"file_id": "d1", "file_unique_id": "du1", "file_name": "x"}
        }
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "date": 1_700_000_000,
            "chat": {"id": chat_id, "type": "private"},
            **content,
        },
    }


def messageless_update(update_id: int) -> dict[str, Any]:
    """An update that carries no ``message`` at all (e.g. an edited message)."""
    return {
        "update_id": update_id,
        "edited_message": {
            "message_id": 7,
            "date": 1_700_000_000,
            "chat": {"id": 99, "type": "private"},
            "text": "edited later",
        },
    }


class FakeTransport:
    """In-memory transport double.

    Records every call and replays scripted ``getUpdates`` batches, so the
    polling loop can be exercised end-to-end without any network access.
    """

    def __init__(
        self,
        batches: list[list[Any]] | None = None,
        *,
        fail_on: dict[str, BaseException] | None = None,
        me: dict[str, Any] | None = None,
        files: dict[str, tuple[str | None, bytes]] | None = None,
    ) -> None:
        self.batches: list[list[Any]] = list(batches or [])
        self.fail_on = dict(fail_on or {})
        self.me: dict[str, Any] = (
            {"id": 1, "username": "testy_bot", "first_name": "Testy"}
            if me is None
            else me
        )
        # file_id -> (file_path returned by getFile, raw bytes to download).
        self.files: dict[str, tuple[str | None, bytes]] = dict(files or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        recorded = dict(params or {})
        self.calls.append((method, recorded))
        if method in self.fail_on:
            raise self.fail_on[method]
        if method == "getUpdates":
            batch = self.batches.pop(0) if self.batches else []
            return {"ok": True, "result": batch}
        if method == "getMe":
            return {"ok": True, "result": self.me}
        if method == "getFile":
            return {"ok": True, "result": self._file_result(recorded.get("file_id"))}
        return {"ok": True, "result": {"message_id": 1, "chat": {"id": 0}}}

    def download(self, file_path: str) -> bytes:
        """Replay the bytes registered for ``file_path`` (or fail loudly)."""
        self.calls.append(("download", {"file_path": file_path}))
        if "download" in self.fail_on:
            raise self.fail_on["download"]
        for path, data in self.files.values():
            if path == file_path:
                return data
        raise TelegramApiError("download", f"no such file: {file_path}")

    def _file_result(self, file_id: Any) -> dict[str, Any]:
        entry = self.files.get(str(file_id)) if file_id is not None else None
        if entry is None:
            return {"file_id": file_id, "file_unique_id": "unknown", "file_path": None}
        file_path, data = entry
        return {
            "file_id": file_id,
            "file_unique_id": f"{file_id}-unique",
            "file_size": len(data),
            "file_path": file_path,
        }

    def calls_for(self, method: str) -> list[dict[str, Any]]:
        """Parameters of every recorded call to ``method``, in order."""
        return [params for name, params in self.calls if name == method]


class FakeGate:
    """Scripted ``VisionGate`` double.

    ``verdicts`` is a queue of :class:`HumanVerdict` values or exceptions to
    raise; each ``classify`` call consumes the next. When the queue is empty
    the ``default`` verdict is returned, so tests only script the calls they
    care about. The raw images passed in are recorded for assertions.
    """

    def __init__(
        self,
        verdicts: list[Any] | None = None,
        *,
        default: HumanVerdict | None = None,
    ) -> None:
        self.verdicts: list[Any] = list(verdicts or [])
        self.default: HumanVerdict = (
            HumanVerdict(is_human=True) if default is None else default
        )
        self.images: list[bytes] = []
        self.media_types: list[str] = []
        self.calls = 0

    def classify(
        self, image: bytes, *, media_type: str = "image/jpeg"
    ) -> HumanVerdict:
        self.calls += 1
        self.images.append(image)
        self.media_types.append(media_type)
        result = self.verdicts.pop(0) if self.verdicts else self.default
        if isinstance(result, BaseException):
            raise result
        return result


class FakeLLM:
    """Scripted ``InterviewLLM`` double.

    ``next_question`` is deterministic by transcript length — ``questions`` is
    indexed by ``len(transcript)`` — so the router's one next-question call per
    answer is easy to predict. The session stores the pending question, so the
    fake is only ever consulted to produce the *next* question (and the
    dossier), never to re-derive the current one. ``next_errors`` /
    ``summarize_errors`` are FIFO queues of exceptions raised in place of the
    scripted result; ``dossiers`` is a queue that ``summarize`` pops from.
    Every call's transcript argument is recorded for assertions.
    """

    def __init__(
        self,
        *,
        questions: list[str] | None = None,
        dossiers: list[Dossier] | None = None,
    ) -> None:
        self.questions: list[str] = questions or [
            f"Q{number}" for number in range(1, 6)
        ]
        self.next_errors: list[BaseException] = []
        self.summarize_errors: list[BaseException] = []
        self.dossiers: list[Dossier] = list(dossiers or [])
        self.next_calls: list[list[QaPair]] = []
        self.summarize_calls: list[list[QaPair]] = []

    def next_question(self, transcript: list[QaPair]) -> str:
        self.next_calls.append(list(transcript))
        if self.next_errors:
            raise self.next_errors.pop(0)
        return self.questions[len(transcript)]

    def summarize(self, transcript: list[QaPair]) -> Dossier:
        self.summarize_calls.append(list(transcript))
        if self.summarize_errors:
            raise self.summarize_errors.pop(0)
        return self.dossiers.pop(0)
