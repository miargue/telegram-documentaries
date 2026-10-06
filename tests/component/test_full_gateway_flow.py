"""Component: the whole Phase 1 gateway over one faked seam (``urlopen``).

Honest label: no real I/O. The transport, parsing, echo rule and offset
acking all run for real; only the HTTP layer is a scripted double, so the
test exercises the exact code path production uses — minus the socket.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

from conftest import messageless_update, photo_update, text_update
from telegram_documentaries.polling import run_polling
from telegram_documentaries.transport import UrllibTransport


class FakeUrlOpen:
    """Scripted stand-in for ``urllib.request.urlopen``.

    Serves ``getUpdates`` from scripted batches and records ``sendMessage``
    bodies, returning Telegram-shaped JSON for both.
    """

    def __init__(self, batches: list[list[dict[str, Any]]]) -> None:
        self.batches = list(batches)
        self.get_updates: list[dict[str, str]] = []
        self.send_messages: list[dict[str, str]] = []

    def __call__(self, request: Any, timeout: float) -> Any:
        url: str = request.full_url
        body: dict[str, str] = urllib.parse.parse_qs(
            (request.data or b"").decode("utf-8")
        )
        flat = {key: values[0] for key, values in body.items()}

        if url.endswith("/getUpdates"):
            self.get_updates.append(flat)
            batch = self.batches.pop(0) if self.batches else []
            payload = {"ok": True, "result": batch}
        elif url.endswith("/sendMessage"):
            self.send_messages.append(flat)
            payload = {"ok": True, "result": {"message_id": 99}}
        else:  # pragma: no cover - guards against unexpected URLs
            raise AssertionError(f"unexpected request URL: {url}")
        return _Response(json.dumps(payload).encode("utf-8"))


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_gateway_echoes_every_message_and_acks_the_batch() -> None:
    opener = FakeUrlOpen(
        batches=[
            [
                text_update(100, chat_id=7, text="hello"),
                photo_update(101, chat_id=8),
                messageless_update(102),
            ],
            [],
        ]
    )
    transport = UrllibTransport("COMPONENT-TOKEN", urlopen=opener)

    final_offset = run_polling(transport, offset=0, max_polls=2)

    # Exactly one 'hey mate!' per incoming message, to the right chats.
    assert [msg["chat_id"] for msg in opener.send_messages] == ["7", "8"]
    assert {msg["text"] for msg in opener.send_messages} == {"hey mate!"}

    # Token goes in the URL; first fetch starts at 0, second acks the batch.
    assert opener.get_updates[0]["offset"] == "0"
    assert opener.get_updates[1]["offset"] == "103"  # max(update_id) + 1
    assert final_offset == 103


def test_gateway_survives_telegram_reporting_a_failed_send() -> None:
    class FailingSendOpener(FakeUrlOpen):
        def __call__(self, request: Any, timeout: float) -> Any:
            if request.full_url.endswith("/sendMessage"):
                self.send_messages.append({})
                return _Response(
                    json.dumps(
                        {
                            "ok": False,
                            "error_code": 403,
                            "description": "bot was blocked by the user",
                        }
                    ).encode("utf-8")
                )
            return super().__call__(request, timeout)

    opener = FailingSendOpener(batches=[[text_update(7, chat_id=1)], []])
    transport = UrllibTransport("TOKEN", urlopen=opener)

    final_offset = run_polling(transport, offset=0, max_polls=2)

    # The failed send does not kill the loop, and the batch is still acked.
    assert final_offset == 8
    assert len(opener.get_updates) == 2
