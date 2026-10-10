"""Component: the whole gateway over one faked seam (``urlopen``).

Honest label: no real I/O. The transport, parsing, Bouncer routing and offset
acking all run for real; only the HTTP layer is a scripted double, so the test
exercises the exact code path production uses — minus the socket. The vision
gate is a ``FakeGate`` (no Gemini).
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Any

import pytest
from conftest import FakeGate, messageless_update, text_update

from telegram_documentaries.bouncer import PROMPT_PHOTO
from telegram_documentaries.cli import main
from telegram_documentaries.polling import run_polling
from telegram_documentaries.session import SessionStore
from telegram_documentaries.transport import UrllibTransport


class FakeUrlOpen:
    """Scripted stand-in for ``urllib.request.urlopen``.

    Serves ``getUpdates`` from scripted batches and records ``sendMessage``
    bodies, returning Telegram-shaped JSON for both.
    """

    def __init__(self, batches: list[list[dict[str, Any]]]) -> None:
        self.batches = list(batches)
        self.methods: list[str] = []
        self.get_updates: list[dict[str, str]] = []
        self.send_messages: list[dict[str, str]] = []

    def __call__(self, request: Any, timeout: float) -> Any:
        url: str = request.full_url
        method = url.rsplit("/", 1)[-1]
        self.methods.append(method)
        body: dict[str, list[str]] = urllib.parse.parse_qs(
            (request.data or b"").decode("utf-8")
        )
        flat = {key: values[0] for key, values in body.items()}

        if method == "getUpdates":
            self.get_updates.append(flat)
            batch = self.batches.pop(0) if self.batches else []
            payload = {"ok": True, "result": batch}
        elif method == "getMe":
            payload = {"ok": True, "result": {"id": 1, "username": "docs_bot"}}
        elif method == "sendMessage":
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


def _run(transport: UrllibTransport, *, max_polls: int) -> int:
    return run_polling(
        transport,
        offset=0,
        session=SessionStore(),
        gate=FakeGate(),
        max_polls=max_polls,
    )


def test_gateway_handles_every_message_and_acks_the_batch() -> None:
    opener = FakeUrlOpen(
        batches=[
            [
                text_update(100, chat_id=7, text="hello"),
                text_update(101, chat_id=8, text="hi"),
                messageless_update(102),
            ],
            [],
        ]
    )
    transport = UrllibTransport("COMPONENT-TOKEN", urlopen=opener)

    final_offset = _run(transport, max_polls=2)

    # Exactly one photo prompt per incoming message, to the right chats.
    assert [msg["chat_id"] for msg in opener.send_messages] == ["7", "8"]
    assert {msg["text"] for msg in opener.send_messages} == {PROMPT_PHOTO}

    # Token goes in the URL; first fetch starts at 0, second acks the batch.
    assert opener.get_updates[0]["offset"] == "0"
    assert opener.get_updates[1]["offset"] == "103"  # max(update_id) + 1
    assert final_offset == 103


def test_startup_probe_calls_get_me_through_the_real_transport(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Same injectable urlopen seam as the poll loop: the startup probe is
    # transport-testable without a live bot.
    opener = FakeUrlOpen(batches=[[]])
    transport = UrllibTransport("COMPONENT-TOKEN", urlopen=opener)

    with caplog.at_level(logging.INFO, logger="telegram_documentaries.cli"):
        exit_code = main(
            ["--max-polls", "1"],
            environ={
                "TELEGRAM_BOT_TOKEN": "COMPONENT-TOKEN",
                "GEMINI_API_KEY": "component-key",
            },
            env_file=None,
            transport_factory=lambda token: transport,
            gate_factory=lambda api_key, model: FakeGate(),
        )

    assert exit_code == 0
    assert opener.methods[0] == "getMe"
    starting = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "gateway_starting"
    ]
    assert starting
    assert getattr(starting[0], "bot_username") == "docs_bot"


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

    final_offset = _run(transport, max_polls=2)

    # The failed send does not kill the loop, and the batch is still acked.
    assert final_offset == 8
    assert len(opener.get_updates) == 2


def test_gateway_survives_a_non_normalised_transport_crash() -> None:
    # A bug inside the transport itself raises something that is neither an
    # HTTP/network error nor a TelegramApiError. The gateway must still live:
    # the failing chat is skipped, everyone else in the batch gets a reply.
    class CrashingSendOpener(FakeUrlOpen):
        def __init__(self, batches: list[list[dict[str, Any]]]) -> None:
            super().__init__(batches)
            self.crashed = False

        def __call__(self, request: Any, timeout: float) -> Any:
            if request.full_url.endswith("/sendMessage") and not self.crashed:
                self.crashed = True
                raise RuntimeError("unexpected bug in the transport layer")
            return super().__call__(request, timeout)

    opener = CrashingSendOpener(
        batches=[[text_update(9, chat_id=1), text_update(10, chat_id=2)], []]
    )
    transport = UrllibTransport("TOKEN", urlopen=opener)

    final_offset = _run(transport, max_polls=2)

    assert final_offset == 11
    assert opener.get_updates[1]["offset"] == "11"  # batch still acked
    assert [msg["chat_id"] for msg in opener.send_messages] == ["2"]
    assert opener.send_messages[0]["text"] == PROMPT_PHOTO
