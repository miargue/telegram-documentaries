"""Behaviour of the Phase 2 Bouncer: route messages and gate portraits.

No test contacts Telegram or Gemini: the transport is ``FakeTransport`` and the
gate is ``FakeGate``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from conftest import (
    FakeGate,
    FakeTransport,
    _RecordingHandler,
    document_update,
    messageless_update,
    photo_update,
    text_update,
)

from telegram_documentaries.bouncer import (
    APOLOGY_REPLY,
    PROMPT_PHOTO,
    REJECTION_REPLY,
    SUCCESS_REPLY,
    handle_update,
)
from telegram_documentaries.logging_config import SKIPPED
from telegram_documentaries.models import Update
from telegram_documentaries.session import Phase, SessionStore
from telegram_documentaries.transport import TelegramApiError
from telegram_documentaries.vision import HumanVerdict, VisionError


def _update(payload: dict[str, Any]) -> Update:
    return Update.model_validate(payload)


def _size(
    file_id: str, width: int, height: int, file_size: int | None
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


def _photo_transport() -> FakeTransport:
    return FakeTransport(files={"abc123": ("photos/a.jpg", b"PORTRAIT-BYTES")})


@contextmanager
def _capture(
    logger_name: str,
) -> Iterator[tuple[logging.Logger, list[logging.LogRecord]]]:
    records: list[logging.LogRecord] = []
    logger = logging.getLogger(logger_name)
    handler = _RecordingHandler(records)
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield logger, records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def _events(records: list[logging.LogRecord]) -> list[str]:
    return [str(getattr(record, "event", "")) for record in records]


def test_start_resets_the_session_and_prompts_for_a_photo() -> None:
    store = SessionStore()
    store.record_pass(chat_id=1, photo=b"old-photo")
    gate = FakeGate()
    transport = FakeTransport()

    sent = handle_update(
        _update(text_update(1, chat_id=1, text="/start")),
        transport,
        session=store,
        gate=gate,
    )

    assert sent is True
    replies = transport.calls_for("sendMessage")
    assert len(replies) == 1
    assert replies[0]["text"] == PROMPT_PHOTO
    assert replies[0]["chat_id"] == 1
    assert gate.calls == 0
    assert store.get(1).phase is Phase.awaiting_photo
    assert store.get(1).photo is None


def test_restart_behaves_like_start() -> None:
    store = SessionStore()
    store.record_pass(chat_id=2, photo=b"old")
    gate = FakeGate()
    transport = FakeTransport()

    sent = handle_update(
        _update(text_update(2, chat_id=2, text="/restart")),
        transport,
        session=store,
        gate=gate,
    )

    assert sent is True
    assert transport.calls_for("sendMessage")[0]["text"] == PROMPT_PHOTO
    assert gate.calls == 0
    assert store.get(2).phase is Phase.awaiting_photo


def test_start_with_a_bot_mention_is_recognised() -> None:
    store = SessionStore()
    transport = FakeTransport()

    handle_update(
        _update(text_update(3, chat_id=3, text="/start@MiargueBot")),
        transport,
        session=store,
        gate=FakeGate(),
    )

    assert transport.calls_for("sendMessage")[0]["text"] == PROMPT_PHOTO


def test_plain_text_re_prompts_without_calling_the_gate() -> None:
    store = SessionStore()
    gate = FakeGate()
    transport = FakeTransport()

    sent = handle_update(
        _update(text_update(4, chat_id=4, text="hello there")),
        transport,
        session=store,
        gate=gate,
    )

    assert sent is True
    assert transport.calls_for("sendMessage")[0]["text"] == PROMPT_PHOTO
    assert gate.calls == 0
    assert store.get(4).phase is Phase.awaiting_photo


@pytest.mark.parametrize("kind", ["document", "sticker", "voice"])
def test_other_content_re_prompts_without_calling_the_gate(kind: str) -> None:
    store = SessionStore()
    gate = FakeGate()
    transport = FakeTransport()

    sent = handle_update(
        _update(document_update(5, chat_id=5, kind=kind)),
        transport,
        session=store,
        gate=gate,
    )

    assert sent is True
    assert transport.calls_for("sendMessage")[0]["text"] == PROMPT_PHOTO
    assert gate.calls == 0


def test_photo_without_a_human_is_rejected_and_resets_the_session() -> None:
    store = SessionStore()
    store.record_pass(chat_id=6, photo=b"stale")
    gate = FakeGate([HumanVerdict(is_human=False, reason="a dog")])
    transport = _photo_transport()

    sent = handle_update(
        _update(photo_update(6, chat_id=6)),
        transport,
        session=store,
        gate=gate,
    )

    assert sent is True
    assert transport.calls_for("sendMessage")[0]["text"] == REJECTION_REPLY
    assert gate.calls == 1
    assert store.get(6).phase is Phase.awaiting_photo
    assert store.get(6).photo is None


def test_photo_of_a_human_passes_and_retains_the_image() -> None:
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True, reason="a face")])
    transport = _photo_transport()

    sent = handle_update(
        _update(photo_update(7, chat_id=7)),
        transport,
        session=store,
        gate=gate,
    )

    assert sent is True
    assert transport.calls_for("sendMessage")[0]["text"] == SUCCESS_REPLY
    assert gate.calls == 1
    assert gate.images == [b"PORTRAIT-BYTES"]
    assert store.get(7).phase is Phase.gate_passed
    assert store.get(7).photo == b"PORTRAIT-BYTES"


def test_photo_with_a_caption_is_still_treated_as_a_photo() -> None:
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True)])
    transport = _photo_transport()

    sent = handle_update(
        _update(photo_update(8, chat_id=8, caption="it's me!")),
        transport,
        session=store,
        gate=gate,
    )

    assert sent is True
    assert transport.calls_for("sendMessage")[0]["text"] == SUCCESS_REPLY
    assert gate.calls == 1


def test_album_first_photo_is_gated_and_its_siblings_are_ignored() -> None:
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True)])
    transport = _photo_transport()

    first = handle_update(
        _update(photo_update(20, chat_id=20, media_group_id="album-a")),
        transport,
        session=store,
        gate=gate,
    )
    second = handle_update(
        _update(photo_update(21, chat_id=20, media_group_id="album-a")),
        transport,
        session=store,
        gate=gate,
    )

    assert first is True
    assert second is SKIPPED
    assert gate.calls == 1
    assert [call["text"] for call in transport.calls_for("sendMessage")] == [
        SUCCESS_REPLY
    ]


def test_album_follow_up_after_a_rejection_is_still_ignored() -> None:
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=False, reason="a dog")])
    transport = _photo_transport()

    handle_update(
        _update(photo_update(22, chat_id=22, media_group_id="album-b")),
        transport,
        session=store,
        gate=gate,
    )
    sent = handle_update(
        _update(photo_update(23, chat_id=22, media_group_id="album-b")),
        transport,
        session=store,
        gate=gate,
    )

    assert sent is SKIPPED
    assert gate.calls == 1
    assert [call["text"] for call in transport.calls_for("sendMessage")] == [
        REJECTION_REPLY
    ]


@pytest.mark.parametrize(
    ("first_file", "second_file"),
    [("portrait", "landscape"), ("landscape", "portrait")],
)
def test_album_is_gated_once_whichever_member_arrives_first(
    first_file: str, second_file: str
) -> None:
    files: dict[str, tuple[str | None, bytes]] = {
        "portrait": ("photos/p.jpg", b"PORTRAIT"),
        "landscape": ("photos/l.jpg", b"LANDSCAPE"),
    }
    transport = FakeTransport(files=files)
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True)])
    uploads = [
        _update(
            photo_update(
                30,
                chat_id=30,
                media_group_id="album-c",
                photos=[_size(first_file, 100, 100, 100)],
            )
        ),
        _update(
            photo_update(
                31,
                chat_id=30,
                media_group_id="album-c",
                photos=[_size(second_file, 100, 100, 100)],
            )
        ),
    ]

    results = [
        handle_update(update, transport, session=store, gate=gate)
        for update in uploads
    ]

    assert results[0] is True
    assert results[1] is SKIPPED
    assert gate.calls == 1
    assert gate.images == [files[first_file][1]]


def test_distinct_albums_are_each_gated() -> None:
    store = SessionStore()
    gate = FakeGate()
    transport = _photo_transport()

    for update_id, group in ((32, "album-d"), (33, "album-e")):
        handle_update(
            _update(photo_update(update_id, chat_id=33, media_group_id=group)),
            transport,
            session=store,
            gate=gate,
        )

    assert gate.calls == 2


def test_photos_without_a_media_group_are_always_gated() -> None:
    store = SessionStore()
    gate = FakeGate()
    transport = _photo_transport()

    for update_id in (34, 35):
        handle_update(
            _update(photo_update(update_id, chat_id=34)),
            transport,
            session=store,
            gate=gate,
        )

    assert gate.calls == 2


def test_start_clears_the_album_dedupe() -> None:
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True), HumanVerdict(is_human=True)])
    transport = _photo_transport()

    handle_update(
        _update(photo_update(40, chat_id=40, media_group_id="album-f")),
        transport,
        session=store,
        gate=gate,
    )
    handle_update(
        _update(photo_update(41, chat_id=40, media_group_id="album-f")),
        transport,
        session=store,
        gate=gate,
    )
    handle_update(
        _update(text_update(42, chat_id=40, text="/start")),
        transport,
        session=store,
        gate=gate,
    )
    sent = handle_update(
        _update(photo_update(43, chat_id=40, media_group_id="album-f")),
        transport,
        session=store,
        gate=gate,
    )

    assert sent is True
    assert gate.calls == 2


def test_album_skip_is_logged() -> None:
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True)])
    transport = _photo_transport()

    with _capture("bouncer.album_skip") as (logger, records):
        handle_update(
            _update(photo_update(50, chat_id=50, media_group_id="album-g")),
            transport,
            session=store,
            gate=gate,
            logger=logger,
        )
        handle_update(
            _update(photo_update(51, chat_id=50, media_group_id="album-g")),
            transport,
            session=store,
            gate=gate,
            logger=logger,
        )

    assert "bouncer_album_skipped" in _events(records)


def test_multi_size_photo_is_gated_once_with_the_largest_resolution() -> None:
    # Per the user's decision: `largest_photo` picks by pixel area, so a real
    # Telegram size array is reduced to one (best-quality) download.
    photos = [
        _size("small", 90, 90, 1000),
        _size("large", 800, 600, 20_000),
        _size("medium", 320, 240, 8000),
    ]
    transport = FakeTransport(
        files={
            "small": ("photos/s.jpg", b"SMALL"),
            "large": ("photos/l.jpg", b"LARGE"),
            "medium": ("photos/m.jpg", b"MEDIUM"),
        }
    )
    gate = FakeGate([HumanVerdict(is_human=True)])

    handle_update(
        _update(photo_update(9, chat_id=9, photos=photos)),
        transport,
        session=SessionStore(),
        gate=gate,
    )

    assert gate.calls == 1
    assert gate.images == [b"LARGE"]
    assert transport.calls_for("getFile") == [{"file_id": "large"}]


def test_empty_photo_list_re_prompts_without_gating() -> None:
    gate = FakeGate()
    transport = FakeTransport()

    handle_update(
        _update(photo_update(10, chat_id=10, photos=[])),
        transport,
        session=SessionStore(),
        gate=gate,
    )

    assert transport.calls_for("sendMessage")[0]["text"] == PROMPT_PHOTO
    assert gate.calls == 0


def test_messageless_update_is_skipped() -> None:
    transport = FakeTransport()

    sent = handle_update(
        _update(messageless_update(11)),
        transport,
        session=SessionStore(),
        gate=FakeGate(),
    )

    assert sent is SKIPPED
    assert transport.calls_for("sendMessage") == []


def test_messageless_update_logs_a_skip_lifecycle_not_a_degraded_warning() -> None:
    transport = FakeTransport()

    with _capture("telegram_documentaries.bouncer") as (_logger, records):
        sent = handle_update(
            _update(messageless_update(70)),
            transport,
            session=SessionStore(),
            gate=FakeGate(),
        )

    assert sent is SKIPPED
    stages = [getattr(record, "stage", None) for record in records]
    assert "skipped" in stages
    assert "degraded" not in stages


def test_album_follow_up_logs_a_skip_lifecycle_not_a_degraded_warning() -> None:
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True)])
    transport = _photo_transport()

    with _capture("telegram_documentaries.bouncer") as (_logger, records):
        handle_update(
            _update(photo_update(71, chat_id=71, media_group_id="album-z")),
            transport,
            session=store,
            gate=gate,
        )
        sent = handle_update(
            _update(photo_update(72, chat_id=71, media_group_id="album-z")),
            transport,
            session=store,
            gate=gate,
        )

    assert sent is SKIPPED
    stages = [getattr(record, "stage", None) for record in records]
    assert "skipped" in stages
    assert "degraded" not in stages


def test_vision_error_becomes_an_apology_and_leaves_the_session_usable() -> None:
    store = SessionStore()
    gate = FakeGate(
        [
            VisionError("gemini exploded"),
            HumanVerdict(is_human=True),
        ]
    )
    transport = _photo_transport()

    with _capture("bouncer.vision_error") as (logger, records):
        sent = handle_update(
            _update(photo_update(12, chat_id=12)),
            transport,
            session=store,
            gate=gate,
            logger=logger,
        )

    assert sent is True
    assert transport.calls_for("sendMessage")[0]["text"] == APOLOGY_REPLY
    assert store.get(12).phase is Phase.awaiting_photo
    failures = [r for r in records if r.levelno >= logging.ERROR]
    assert any("vision_failed" in _events([r]) for r in failures)
    assert any(r.exc_info is not None for r in failures)

    # The session is still usable for a later, healthy photo.
    handle_update(
        _update(photo_update(13, chat_id=12)),
        transport,
        session=store,
        gate=gate,
        logger=logger,
    )
    assert store.get(12).phase is Phase.gate_passed


def test_photo_fetch_failure_becomes_an_apology() -> None:
    store = SessionStore()
    gate = FakeGate()
    transport = FakeTransport(
        fail_on={"getFile": TelegramApiError("getFile", "file not found")}
    )

    with _capture("bouncer.fetch_error") as (logger, records):
        sent = handle_update(
            _update(photo_update(14, chat_id=14)),
            transport,
            session=store,
            gate=gate,
            logger=logger,
        )

    assert sent is True
    assert transport.calls_for("sendMessage")[0]["text"] == APOLOGY_REPLY
    assert gate.calls == 0
    assert "photo_fetch_failed" in _events(records)
    assert any(r.exc_info is not None for r in records)


def test_download_failure_becomes_an_apology() -> None:
    transport = FakeTransport(
        files={"abc123": ("photos/a.jpg", b"data")},
        fail_on={"download": TelegramApiError("download", "network down")},
    )

    sent = handle_update(
        _update(photo_update(15, chat_id=15)),
        transport,
        session=SessionStore(),
        gate=FakeGate(),
    )

    assert sent is True
    assert transport.calls_for("sendMessage")[0]["text"] == APOLOGY_REPLY


def test_hostile_file_path_is_rejected_without_downloading_or_gating() -> None:
    # The media safety check (reject traversal/absolute/scheme paths) must
    # degrade through the same apology path and never reach the gate.
    transport = FakeTransport(files={"abc123": ("../../etc/passwd", b"secret")})
    gate = FakeGate()

    with _capture("bouncer.hostile_path") as (logger, records):
        sent = handle_update(
            _update(photo_update(21, chat_id=21)),
            transport,
            session=SessionStore(),
            gate=gate,
            logger=logger,
        )

    assert sent is True
    assert transport.calls_for("sendMessage")[0]["text"] == APOLOGY_REPLY
    assert transport.calls_for("download") == []
    assert gate.calls == 0
    assert "photo_fetch_failed" in _events(records)
    assert any(r.exc_info is not None for r in records)


def test_empty_downloaded_image_is_treated_as_a_gate_failure() -> None:
    transport = FakeTransport(files={"abc123": ("photos/empty.jpg", b"")})
    gate = FakeGate()

    with _capture("bouncer.empty_image") as (logger, records):
        sent = handle_update(
            _update(photo_update(16, chat_id=16)),
            transport,
            session=SessionStore(),
            gate=gate,
            logger=logger,
        )

    assert sent is True
    assert transport.calls_for("sendMessage")[0]["text"] == APOLOGY_REPLY
    assert gate.calls == 0
    assert "photo_fetch_failed" in _events(records)


def test_send_failure_is_logged_loudly_and_returns_false() -> None:
    transport = FakeTransport(
        fail_on={"sendMessage": TelegramApiError("sendMessage", "bot blocked")}
    )

    with _capture("bouncer.send_failure") as (logger, records):
        sent = handle_update(
            _update(text_update(17, chat_id=17)),
            transport,
            session=SessionStore(),
            gate=FakeGate(),
            logger=logger,
        )

    assert sent is False
    failures = [r for r in records if r.levelno >= logging.ERROR]
    assert any("reply_failed" in _events([r]) for r in failures)
    assert any(getattr(r, "chat_id", None) == 17 for r in failures)
    assert any(r.exc_info is not None for r in failures)


def test_send_failure_marks_the_lifecycle_degraded_not_success() -> None:
    # The decorator lives on the module logger, so capture that (the handler's
    # own ``logger=`` only affects per-event logs).
    transport = FakeTransport(
        fail_on={"sendMessage": TelegramApiError("sendMessage", "bot blocked")}
    )

    with _capture("telegram_documentaries.bouncer") as (_logger, records):
        sent = handle_update(
            _update(text_update(60, chat_id=60)),
            transport,
            session=SessionStore(),
            gate=FakeGate(),
        )

    assert sent is False
    stages = [getattr(record, "stage", None) for record in records]
    assert "degraded" in stages
    assert "success" not in stages


def test_unexpected_send_exception_is_swallowed_and_logged() -> None:
    transport = FakeTransport(
        fail_on={"sendMessage": RuntimeError("transport bug: socket exploded")}
    )

    with _capture("bouncer.unexpected_send") as (logger, records):
        sent = handle_update(
            _update(text_update(18, chat_id=18)),
            transport,
            session=SessionStore(),
            gate=FakeGate(),
            logger=logger,
        )

    assert sent is False
    assert any(r.levelno >= logging.ERROR for r in records)


def test_process_control_exceptions_are_not_swallowed() -> None:
    transport = FakeTransport(fail_on={"sendMessage": KeyboardInterrupt()})

    with pytest.raises(KeyboardInterrupt):
        handle_update(
            _update(text_update(19, chat_id=19)),
            transport,
            session=SessionStore(),
            gate=FakeGate(),
        )


def test_sessions_never_leak_between_chats() -> None:
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True)])
    transport = _photo_transport()

    handle_update(
        _update(photo_update(20, chat_id=20)),
        transport,
        session=store,
        gate=gate,
    )

    other = store.get(21)
    assert other.phase is Phase.awaiting_photo
    assert other.photo is None
    assert store.get(20).photo == b"PORTRAIT-BYTES"
