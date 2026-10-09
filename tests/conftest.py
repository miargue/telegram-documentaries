"""Shared test helpers: fake transport and raw Telegram payload builders.

Everything here is deterministic and performs no I/O — tests never touch a
live bot. Raw payload builders emit plain dicts exactly as the Telegram Bot
API would, so they can be fed through the real parsing boundary.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any


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


def photo_update(update_id: int, chat_id: int) -> dict[str, Any]:
    """An incoming photo message (a message with no ``text`` field)."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "date": 1_700_000_000,
            "chat": {"id": chat_id, "type": "private"},
            "photo": [
                {
                    "file_id": "abc123",
                    "file_unique_id": "uniq123",
                    "width": 90,
                    "height": 90,
                    "file_size": 1024,
                }
            ],
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
    ) -> None:
        self.batches: list[list[Any]] = list(batches or [])
        self.fail_on = dict(fail_on or {})
        self.me: dict[str, Any] = (
            {"id": 1, "username": "testy_bot", "first_name": "Testy"}
            if me is None
            else me
        )
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
        return {"ok": True, "result": {"message_id": 1, "chat": {"id": 0}}}

    def calls_for(self, method: str) -> list[dict[str, Any]]:
        """Parameters of every recorded call to ``method``, in order."""
        return [params for name, params in self.calls if name == method]
