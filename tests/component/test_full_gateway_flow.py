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
from conftest import (
    FakeConverter,
    FakeGate,
    FakeLLM,
    FakeNarrator,
    FakeScripter,
    _RecordingHandler,
    messageless_update,
    photo_update,
    text_update,
)

from telegram_documentaries.bouncer import PROMPT_PHOTO
from telegram_documentaries.cli import main
from telegram_documentaries.polling import (
    RETRY_BASE_DELAY,
    poll_once,
    run_polling,
)
from telegram_documentaries.session import Dossier, Phase, SessionStore
from telegram_documentaries.transport import UrllibTransport
from telegram_documentaries.vision import HumanVerdict


class FakeUrlOpen:
    """Scripted stand-in for ``urllib.request.urlopen``.

    Serves ``getUpdates`` from scripted batches, ``getMe``, ``getFile``, photo
    downloads and the ``sendMessage``/``sendPhoto``/``sendAudio`` uploads, so the
    whole gateway can run over the real ``UrllibTransport`` — minus the socket.
    ``fail_get_updates_at`` makes the Nth ``getUpdates`` call raise an ``OSError``
    once, modelling a transient network blip the loop must ride out.
    """

    def __init__(
        self,
        batches: list[list[dict[str, Any]]],
        *,
        file_bytes: bytes = b"PORTRAIT-BYTES",
        fail_get_updates_at: int | None = None,
    ) -> None:
        self.batches = list(batches)
        self.file_bytes = file_bytes
        self.fail_get_updates_at = fail_get_updates_at
        self.methods: list[str] = []
        self.get_updates: list[dict[str, str]] = []
        self.get_updates_calls = 0
        self.send_messages: list[dict[str, str]] = []
        self.uploads: list[str] = []
        self.downloads: list[str] = []

    def __call__(self, request: Any, timeout: float) -> Any:
        url: str = request.full_url
        if "/file/bot" in url:
            self.methods.append("download")
            self.downloads.append(url)
            return _Response(self.file_bytes)
        method = url.rsplit("/", 1)[-1]
        self.methods.append(method)
        if method in ("sendPhoto", "sendAudio"):
            # Multipart bodies carry binary bytes; never decode them as text.
            self.uploads.append(method)
            payload: dict[str, Any] = {"ok": True, "result": {"message_id": 99}}
            return _Response(json.dumps(payload).encode("utf-8"))
        body: dict[str, list[str]] = urllib.parse.parse_qs(
            (request.data or b"").decode("utf-8")
        )
        flat = {key: values[0] for key, values in body.items()}

        if method == "getUpdates":
            self.get_updates_calls += 1
            if self.get_updates_calls == self.fail_get_updates_at:
                raise OSError("temporary network failure")
            self.get_updates.append(flat)
            batch = self.batches.pop(0) if self.batches else []
            payload = {"ok": True, "result": batch}
        elif method == "getMe":
            payload = {"ok": True, "result": {"id": 1, "username": "docs_bot"}}
        elif method == "sendMessage":
            self.send_messages.append(flat)
            payload = {"ok": True, "result": {"message_id": 99}}
        elif method == "getFile":
            payload = {
                "ok": True,
                "result": {
                    "file_id": flat.get("file_id"),
                    "file_unique_id": "uniq",
                    "file_path": "photos/p.jpg",
                },
            }
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
        llm=FakeLLM(),
        converter=FakeConverter(),
        scripter=FakeScripter(),
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


def test_gateway_full_chain_restart_mid_interview_and_transient_blip() -> None:
    """One two-chat run over the real transport: full pipeline, reset, blip.

    Chat 1 walks photo → interview → portrait → script → audio; chat 2 is
    ``/restart``ed mid-interview. The reset routine must purge chat 2's
    in-memory photo (and, for chat 1, its narration) and re-prompt, while
    leaving the *other* chat untouched. A transient ``getUpdates`` failure
    between batches is ridden out via the injected (fake) ``sleep``.
    """
    opener = FakeUrlOpen(
        batches=[
            [photo_update(1, chat_id=1)],  # chat 1: portrait through the gate
            [text_update(2, chat_id=1, text="a1")],
            [text_update(3, chat_id=1, text="a2")],
            [text_update(4, chat_id=1, text="a3")],
            [text_update(5, chat_id=1, text="a4")],
            [text_update(6, chat_id=1, text="a5")],  # completion: full chain
            [photo_update(7, chat_id=2)],  # chat 2: portrait through the gate
            [text_update(8, chat_id=2, text="b1")],
            [text_update(9, chat_id=2, text="/restart")],  # reset mid-interview
            [text_update(10, chat_id=1, text="/restart")],  # reset finished chat
        ],
        fail_get_updates_at=4,  # a blip while polling for the 4th batch
    )
    transport = UrllibTransport("COMPONENT-TOKEN", urlopen=opener)
    session = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True), HumanVerdict(is_human=True)])
    llm = FakeLLM(dossiers=[Dossier(summary="S", suggested_animal="Capybara")])
    slept: list[float] = []
    records: list[logging.LogRecord] = []
    logger = logging.getLogger("component.polling")
    handler = _RecordingHandler(records)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    def _poll(offset: int) -> int:
        return poll_once(
            transport,
            offset,
            session=session,
            gate=gate,
            llm=llm,
            converter=FakeConverter(),
            scripter=FakeScripter(),
            narrator=FakeNarrator(),
            logger=logger,
            sleep=slept.append,
        )

    try:
        offset = 0
        offset = _poll(offset)  # batch 1
        assert session.get(1).phase is Phase.interviewing
        assert session.get(1).photo == b"PORTRAIT-BYTES"
        for _ in range(4):  # batches 2-5
            offset = _poll(offset)
        offset = _poll(offset)  # batch 6: the completion handoff runs the chain

        state1 = session.get(1)
        assert state1.phase is Phase.done
        assert state1.photo == b"PORTRAIT-BYTES"
        assert state1.script is not None
        assert state1.narration is not None
        assert state1.portrait_sent is True
        assert state1.script_sent is True
        assert state1.narrator_sent is True
        assert "sendPhoto" in opener.uploads
        assert "sendAudio" in opener.uploads

        offset = _poll(offset)  # batch 7: chat 2 portrait
        offset = _poll(offset)  # batch 8: chat 2 first answer
        assert session.get(2).phase is Phase.interviewing
        assert session.get(2).photo == b"PORTRAIT-BYTES"

        offset = _poll(offset)  # batch 9: chat 2 /restart mid-interview
        state2 = session.get(2)
        assert state2.phase is Phase.awaiting_photo
        assert state2.photo is None
        assert state2.interview == []
        assert opener.send_messages[-1] == {
            "chat_id": "2",
            "text": PROMPT_PHOTO,
        }
        # Isolation: chat 1's completed state (and its media) is untouched.
        state1 = session.get(1)
        assert state1.phase is Phase.done
        assert state1.photo == b"PORTRAIT-BYTES"
        assert state1.narration is not None
        assert state1.script is not None

        offset = _poll(offset)  # batch 10: chat 1 /restart purges narration too
        state1 = session.get(1)
        assert state1.phase is Phase.awaiting_photo
        assert state1.photo is None
        assert state1.narration is None
        assert state1.script is None
        assert opener.send_messages[-1] == {
            "chat_id": "1",
            "text": PROMPT_PHOTO,
        }
    finally:
        logger.removeHandler(handler)

    # The transient blip was ridden out once via the injected sleep.
    assert slept == [RETRY_BASE_DELAY]
    events = [str(getattr(record, "event", "")) for record in records]
    assert events.count("get_updates_retry") == 1
    assert events.count("session_reset") == 2
