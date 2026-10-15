"""Behaviour of the Phase 2 Bouncer: route messages and gate portraits.

No test contacts Telegram or Gemini: the transport is ``FakeTransport`` and the
gate is ``FakeGate``.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from conftest import (
    FakeConverter,
    FakeGate,
    FakeLLM,
    FakeNarrator,
    FakeScripter,
    FakeTransport,
    _capture,
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
    _command,
    _send,
    handle_update,
    reset_chat,
)
from telegram_documentaries.converter import TEASER_REPLY, ConverterError
from telegram_documentaries.gemini import AudioPayload
from telegram_documentaries.interviewer import OBSERVATION_COMPLETE_REPLY
from telegram_documentaries.logging_config import SKIPPED
from telegram_documentaries.models import Message, Update
from telegram_documentaries.narrator import NARRATION_APOLOGY_REPLY, NarratorError
from telegram_documentaries.scripter import SCRIPT_APOLOGY_REPLY, ScriptError
from telegram_documentaries.session import (
    Dossier,
    Phase,
    PurgeReport,
    QaPair,
    SessionStore,
)
from telegram_documentaries.transport import (
    MAX_MESSAGE_LENGTH,
    TelegramApiError,
    message_length,
)
from telegram_documentaries.vision import HumanVerdict, VisionError


def _update(payload: dict[str, Any]) -> Update:
    return Update.model_validate(payload)


def _message(payload: dict[str, Any]) -> Message:
    message = _update(payload).message
    assert message is not None
    return message


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
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
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
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
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
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
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
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
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
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
    )

    assert sent is True
    assert transport.calls_for("sendMessage")[0]["text"] == PROMPT_PHOTO
    assert gate.calls == 0


def test_photo_without_a_human_is_rejected_and_resets_the_session() -> None:
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=False, reason="a dog")])
    transport = _photo_transport()

    sent = handle_update(
        _update(photo_update(6, chat_id=6)),
        transport,
        session=store,
        gate=gate,
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
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
    llm = FakeLLM()

    sent = handle_update(
        _update(photo_update(7, chat_id=7)),
        transport,
        session=store,
        gate=gate,
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=llm,
    )

    assert sent is True
    # The handoff is automatic: the success line is immediately followed by
    # exactly one question (Q1), and the chat lands in ``interviewing``.
    assert [call["text"] for call in transport.calls_for("sendMessage")] == [
        SUCCESS_REPLY,
        "Q1",
    ]
    assert llm.next_calls == [[]]
    assert gate.calls == 1
    assert gate.images == [b"PORTRAIT-BYTES"]
    assert store.get(7).phase is Phase.interviewing
    assert store.get(7).photo == b"PORTRAIT-BYTES"


def test_text_during_the_interview_is_delegated_to_the_interviewer() -> None:
    # Once a chat is interviewing, the Bouncer must hand every update to the
    # interviewer (the interviewer owns turn-taking) and never re-gate content.
    store = SessionStore()
    store.record_pass(chat_id=81, photo=b"PORTRAIT")
    store.start_interview(81, question="Q1")
    gate = FakeGate()
    transport = FakeTransport()
    llm = FakeLLM(questions=["Q1", "Q2", "Q3", "Q4", "Q5"])

    sent = handle_update(
        _update(text_update(81, chat_id=81, text="a1")),
        transport,
        session=store,
        gate=gate,
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=llm,
    )

    assert sent is True
    assert transport.calls_for("sendMessage")[-1]["text"] == "Q2"
    assert store.get(81).interview == [QaPair(question="Q1", answer="a1")]
    assert gate.calls == 0


def test_text_after_completion_is_delegated_to_the_interviewer() -> None:
    store = SessionStore()
    store.record_pass(chat_id=82, photo=b"PORTRAIT")
    store.start_interview(82, question="Q1")
    for number in range(1, 6):
        store.record_interview_answer(82, f"Q{number}", f"a{number}")
    store.appoint_dossier(
        82, Dossier(summary="S", suggested_animal="Capybara")
    )
    store.mark_portrait_sent(82)
    store.mark_script_sent(82)  # fully complete: portrait and script delivered
    gate = FakeGate()
    transport = FakeTransport()

    sent = handle_update(
        _update(text_update(82, chat_id=82, text="hello again")),
        transport,
        session=store,
        gate=gate,
        llm=FakeLLM(),
        converter=FakeConverter(),
        scripter=FakeScripter(),
    )

    assert sent is True
    assert transport.calls_for("sendMessage")[-1]["text"] == (
        OBSERVATION_COMPLETE_REPLY
    )
    assert transport.photos == []
    assert gate.calls == 0


def test_start_mid_interview_resets_the_whole_session() -> None:
    store = SessionStore()
    store.record_pass(chat_id=83, photo=b"PORTRAIT")
    store.start_interview(83, question="Q1")
    store.record_interview_answer(83, "Q1", "a1")
    transport = FakeTransport()

    sent = handle_update(
        _update(text_update(83, chat_id=83, text="/start")),
        transport,
        session=store,
        gate=FakeGate(),
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
    )

    assert sent is True
    assert transport.calls_for("sendMessage")[0]["text"] == PROMPT_PHOTO
    state = store.get(83)
    assert state.phase is Phase.awaiting_photo
    assert state.photo is None
    assert state.interview == []
    assert state.dossier is None


def test_photo_with_a_caption_is_still_treated_as_a_photo() -> None:
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True)])
    transport = _photo_transport()

    sent = handle_update(
        _update(photo_update(8, chat_id=8, caption="it's me!")),
        transport,
        session=store,
        gate=gate,
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
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
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
    )
    second = handle_update(
        _update(photo_update(21, chat_id=20, media_group_id="album-a")),
        transport,
        session=store,
        gate=gate,
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
    )

    assert first is True
    assert second is SKIPPED
    assert gate.calls == 1
    assert [call["text"] for call in transport.calls_for("sendMessage")] == [
        SUCCESS_REPLY,
        "Q1",
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
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
    )
    sent = handle_update(
        _update(photo_update(23, chat_id=22, media_group_id="album-b")),
        transport,
        session=store,
        gate=gate,
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
    )

    assert sent is SKIPPED
    assert gate.calls == 1
    assert [call["text"] for call in transport.calls_for("sendMessage")] == [
        REJECTION_REPLY
    ]


def test_album_sent_while_interviewing_gets_exactly_one_reply() -> None:
    # A photo album sent mid-interview must hit the Interviewer once, not once
    # per member (which would send one identical re-ask per photo): the media
    # group id is remembered for *any* photo message, not only the gate path.
    store = SessionStore()
    store.record_pass(chat_id=90, photo=b"PORTRAIT")
    store.start_interview(90, question="Q1")
    gate = FakeGate()
    transport = FakeTransport()
    llm = FakeLLM(questions=["Q1", "Q2", "Q3", "Q4", "Q5"])

    first = handle_update(
        _update(photo_update(90, chat_id=90, media_group_id="album-mid")),
        transport,
        session=store,
        gate=gate,
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=llm,
    )
    second = handle_update(
        _update(photo_update(91, chat_id=90, media_group_id="album-mid")),
        transport,
        session=store,
        gate=gate,
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=llm,
    )

    assert first is True
    assert second is SKIPPED
    assert gate.calls == 0
    # One photo → one re-ask of the pending question; the sibling is dropped.
    assert [call["text"] for call in transport.calls_for("sendMessage")] == [
        "Q1"
    ]


def test_album_sent_after_completion_gets_exactly_one_reply() -> None:
    store = SessionStore()
    store.record_pass(chat_id=91, photo=b"PORTRAIT")
    store.start_interview(91, question="Q1")
    for number in range(1, 6):
        store.record_interview_answer(91, f"Q{number}", f"a{number}")
    store.appoint_dossier(
        91, Dossier(summary="S", suggested_animal="Capybara")
    )
    store.mark_portrait_sent(91)
    store.mark_script_sent(91)  # fully complete: portrait and script delivered
    gate = FakeGate()
    transport = FakeTransport()
    llm = FakeLLM()

    first = handle_update(
        _update(photo_update(92, chat_id=91, media_group_id="album-done")),
        transport,
        session=store,
        gate=gate,
        llm=llm,
        converter=FakeConverter(),
        scripter=FakeScripter(),
    )
    second = handle_update(
        _update(photo_update(93, chat_id=91, media_group_id="album-done")),
        transport,
        session=store,
        gate=gate,
        llm=llm,
        converter=FakeConverter(),
        scripter=FakeScripter(),
    )

    assert first is True
    assert second is SKIPPED
    assert [call["text"] for call in transport.calls_for("sendMessage")] == [
        OBSERVATION_COMPLETE_REPLY
    ]
    assert transport.photos == []


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
        handle_update(
            update,
            transport,
            session=store,
            gate=gate,
            llm=FakeLLM(),
            converter=FakeConverter(),
            scripter=FakeScripter(),
        )
        for update in uploads
    ]

    assert results[0] is True
    assert results[1] is SKIPPED
    assert gate.calls == 1
    assert gate.images == [files[first_file][1]]


def test_distinct_albums_are_each_gated() -> None:
    # A rejection returns the chat to ``awaiting_photo``, so a second, distinct
    # album in the same chat is gated again: the dedupe keys on the media-group
    # id, not merely "the first album this chat ever sent".
    store = SessionStore()
    gate = FakeGate(
        [HumanVerdict(is_human=False), HumanVerdict(is_human=False)]
    )
    transport = _photo_transport()

    for update_id, group in ((32, "album-d"), (33, "album-e")):
        handle_update(
            _update(photo_update(update_id, chat_id=33, media_group_id=group)),
            transport,
            session=store,
            gate=gate,
            converter=FakeConverter(),
            scripter=FakeScripter(),
            llm=FakeLLM(),
        )

    assert gate.calls == 2
    assert store.get(33).phase is Phase.awaiting_photo


def test_photos_without_a_media_group_are_always_gated() -> None:
    # No media-group id means no dedupe: every such photo is gated, even after
    # a rejection in the same chat.
    store = SessionStore()
    gate = FakeGate(
        [HumanVerdict(is_human=False), HumanVerdict(is_human=False)]
    )
    transport = _photo_transport()

    for update_id in (34, 35):
        handle_update(
            _update(photo_update(update_id, chat_id=34)),
            transport,
            session=store,
            gate=gate,
            converter=FakeConverter(),
            scripter=FakeScripter(),
            llm=FakeLLM(),
        )

    assert gate.calls == 2
    assert store.get(34).phase is Phase.awaiting_photo


def test_start_clears_the_album_dedupe() -> None:
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True), HumanVerdict(is_human=True)])
    transport = _photo_transport()

    handle_update(
        _update(photo_update(40, chat_id=40, media_group_id="album-f")),
        transport,
        session=store,
        gate=gate,
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
    )
    handle_update(
        _update(photo_update(41, chat_id=40, media_group_id="album-f")),
        transport,
        session=store,
        gate=gate,
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
    )
    handle_update(
        _update(text_update(42, chat_id=40, text="/start")),
        transport,
        session=store,
        gate=gate,
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
    )
    sent = handle_update(
        _update(photo_update(43, chat_id=40, media_group_id="album-f")),
        transport,
        session=store,
        gate=gate,
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
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
            converter=FakeConverter(),
            scripter=FakeScripter(),
            llm=FakeLLM(),
            logger=logger,
        )
        handle_update(
            _update(photo_update(51, chat_id=50, media_group_id="album-g")),
            transport,
            session=store,
            gate=gate,
            converter=FakeConverter(),
            scripter=FakeScripter(),
            llm=FakeLLM(),
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
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
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
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
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
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
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
            converter=FakeConverter(),
            scripter=FakeScripter(),
            llm=FakeLLM(),
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
            converter=FakeConverter(),
            scripter=FakeScripter(),
            llm=FakeLLM(),
        )
        sent = handle_update(
            _update(photo_update(72, chat_id=71, media_group_id="album-z")),
            transport,
            session=store,
            gate=gate,
            converter=FakeConverter(),
            scripter=FakeScripter(),
            llm=FakeLLM(),
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
            converter=FakeConverter(),
            scripter=FakeScripter(),
            llm=FakeLLM(),
            logger=logger,
        )

    assert sent is True
    assert transport.calls_for("sendMessage")[0]["text"] == APOLOGY_REPLY
    assert store.get(12).phase is Phase.awaiting_photo
    failures = [r for r in records if r.levelno >= logging.ERROR]
    assert any("vision_failed" in _events([r]) for r in failures)
    assert any(r.exc_info is not None for r in failures)

    # The session is still usable for a later, healthy photo: it passes the
    # gate and the automatic handoff moves it straight into ``interviewing``.
    handle_update(
        _update(photo_update(13, chat_id=12)),
        transport,
        session=store,
        gate=gate,
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
        logger=logger,
    )
    assert store.get(12).phase is Phase.interviewing


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
            converter=FakeConverter(),
            scripter=FakeScripter(),
            llm=FakeLLM(),
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
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
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
            converter=FakeConverter(),
            scripter=FakeScripter(),
            llm=FakeLLM(),
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
            converter=FakeConverter(),
            scripter=FakeScripter(),
            llm=FakeLLM(),
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
            converter=FakeConverter(),
            scripter=FakeScripter(),
            llm=FakeLLM(),
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
            converter=FakeConverter(),
            scripter=FakeScripter(),
            llm=FakeLLM(),
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
            converter=FakeConverter(),
            scripter=FakeScripter(),
            llm=FakeLLM(),
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
            converter=FakeConverter(),
            scripter=FakeScripter(),
            llm=FakeLLM(),
        )


def test_oversized_text_is_truncated_at_the_bouncer_boundary() -> None:
    # Mechanism guard for the over-long outbound-text bug class: Telegram
    # rejects sendMessage over 4096 UTF-16 code units, so the bouncer's send
    # chokepoint must bound every reply — even one that is (defensively) over
    # the limit. All current replies are short constants, so the chokepoint is
    # probed directly to prove the guard fires.
    transport = FakeTransport()

    with _capture("bouncer.truncate") as (logger, _records):
        sent = _send(
            transport,
            5,
            "x" * (MAX_MESSAGE_LENGTH + 1),
            update=_update(text_update(900, chat_id=5)),
            log=logger,
        )

    assert sent is True
    (call,) = transport.calls_for("sendMessage")
    assert message_length(call["text"]) <= MAX_MESSAGE_LENGTH


def test_sessions_never_leak_between_chats() -> None:
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True)])
    transport = _photo_transport()

    handle_update(
        _update(photo_update(20, chat_id=20)),
        transport,
        session=store,
        gate=gate,
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
    )

    other = store.get(21)
    assert other.phase is Phase.awaiting_photo
    assert other.photo is None
    assert store.get(20).photo == b"PORTRAIT-BYTES"


def _completed_store(chat_id: int, *, portrait_sent: bool = False) -> SessionStore:
    """A store whose chat has finished the interview (``done`` phase)."""
    store = SessionStore()
    store.record_pass(chat_id=chat_id, photo=b"PORTRAIT-BYTES")
    store.start_interview(chat_id, question="Q1")
    for number in range(1, 6):
        store.record_interview_answer(chat_id, f"Q{number}", f"a{number}")
    store.appoint_dossier(
        chat_id, Dossier(summary="S", suggested_animal="Capybara")
    )
    if portrait_sent:
        store.mark_portrait_sent(chat_id)
    return store


def test_done_without_a_portrait_routes_the_update_to_the_converter() -> None:
    # A ``done`` chat that never received its portrait (e.g. the image call
    # failed at handoff time) must retry the conversion on the next update
    # rather than fall through to the Interviewer's light completion reply.
    store = _completed_store(100)
    transport = FakeTransport()
    converter = FakeConverter()

    sent = handle_update(
        _update(text_update(100, chat_id=100, text="hello again")),
        transport,
        session=store,
        gate=FakeGate(),
        llm=FakeLLM(),
        converter=converter,
        scripter=FakeScripter(),
    )

    assert sent is True
    assert len(transport.photos) == 1
    assert transport.photos[0]["image"] == converter.default.data
    assert store.get(100).portrait_sent is True
    assert TEASER_REPLY in [c["text"] for c in transport.calls_for("sendMessage")]


def test_retried_portrait_is_followed_by_the_script_in_the_same_update() -> None:
    # A ``done`` chat whose portrait never landed is routed to the Converter on
    # the next update; when that retry succeeds the *same* update must continue
    # into scripting, exactly like the fresh-completion handoff, so the
    # narration follows the retried portrait with no user input in between and
    # the Scripter is called exactly once.
    store = _completed_store(102)  # done with no portrait delivered yet
    transport = FakeTransport()
    converter = FakeConverter()
    scripter = FakeScripter()

    sent = handle_update(
        _update(text_update(102, chat_id=102, text="hello again")),
        transport,
        session=store,
        gate=FakeGate(),
        llm=FakeLLM(),
        converter=converter,
        scripter=scripter,
    )

    assert sent is True
    state = store.get(102)
    assert state.portrait_sent is True
    assert len(transport.photos) == 1
    # Portrait then narration, both in this one update, with a single script
    # generation (never a re-generation on the retry).
    texts = [call["text"] for call in transport.calls_for("sendMessage")]
    assert TEASER_REPLY in texts
    assert texts[-1] == "A dramatic nature documentary script."
    assert len(scripter.calls) == 1
    assert state.script == "A dramatic nature documentary script."
    assert state.script_sent is True


def test_final_answer_hands_off_to_the_converter_in_the_same_update() -> None:
    # The fifth answer produces the dossier report *and*, in the same update,
    # the hybrid portrait: the bouncer delegates to the Interviewer and then
    # completes the handoff itself.
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True)])
    llm = FakeLLM(
        dossiers=[Dossier(summary="S", suggested_animal="Capybara")]
    )
    converter = FakeConverter()
    transport = _photo_transport()

    handle_update(
        _update(photo_update(1, chat_id=1)),
        transport,
        session=store,
        gate=gate,
        llm=llm,
        converter=converter,
        scripter=FakeScripter(),
    )
    assert store.get(1).phase is Phase.interviewing
    for number in range(1, 5):
        handle_update(
            _update(text_update(10 + number, chat_id=1, text=f"a{number}")),
            transport,
            session=store,
            gate=gate,
            llm=llm,
            converter=converter,
            scripter=FakeScripter(),
        )
    assert store.get(1).phase is Phase.interviewing

    sent = handle_update(
        _update(text_update(20, chat_id=1, text="a5")),
        transport,
        session=store,
        gate=gate,
        llm=llm,
        converter=converter,
        scripter=FakeScripter(),
    )

    assert sent is True
    state = store.get(1)
    assert state.phase is Phase.done
    assert state.portrait_sent is True
    assert len(transport.photos) == 1
    assert len(converter.calls) == 1
    texts = [call["text"] for call in transport.calls_for("sendMessage")]
    assert any("behavioural dossier is ready" in text for text in texts)
    assert TEASER_REPLY in texts
    # The script rides in the SAME update as the dossier and the portrait:
    # no user message in between.
    assert texts[-1] == "A dramatic nature documentary script."
    assert state.script == "A dramatic nature documentary script."
    assert state.script_sent is True


def test_handoff_conversion_failure_leaves_the_chat_retryable() -> None:
    # If the image call fails right after the dossier, the flag is never set;
    # a later update retries and delivers the portrait exactly once.
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True)])
    llm = FakeLLM(
        dossiers=[Dossier(summary="S", suggested_animal="Capybara")]
    )
    converter = FakeConverter(results=[ConverterError("boom")])
    transport = _photo_transport()

    handle_update(
        _update(photo_update(30, chat_id=30)),
        transport,
        session=store,
        gate=gate,
        llm=llm,
        converter=converter,
        scripter=FakeScripter(),
    )
    for number in range(1, 6):
        sent = handle_update(
            _update(text_update(30 + number, chat_id=30, text=f"a{number}")),
            transport,
            session=store,
            gate=gate,
            llm=llm,
            converter=converter,
            scripter=FakeScripter(),
        )

    assert sent is False
    assert store.get(30).phase is Phase.done
    assert store.get(30).portrait_sent is False
    assert transport.photos == []

    retried = handle_update(
        _update(text_update(40, chat_id=30, text="try again")),
        transport,
        session=store,
        gate=gate,
        llm=llm,
        converter=converter,
        scripter=FakeScripter(),
    )

    assert retried is True
    assert store.get(30).portrait_sent is True
    assert len(transport.photos) == 1


def _scripted_store(
    chat_id: int, *, portrait_sent: bool = True, script_sent: bool = False
) -> SessionStore:
    """A store whose chat has a portrait but no (or an unsent) narration."""
    store = _completed_store(chat_id, portrait_sent=portrait_sent)
    if script_sent:
        store.store_script(chat_id, "Once spoken.")
        store.mark_script_sent(chat_id)
    return store


def test_done_with_a_portrait_but_no_script_routes_the_update_to_the_scripter() -> None:
    # A ``done`` chat that received its portrait but never its narration (e.g.
    # the script call failed at handoff time) must retry the scripting on the
    # next update rather than fall through to the Interviewer's light reply.
    store = _scripted_store(101)
    transport = FakeTransport()
    scripter = FakeScripter()

    sent = handle_update(
        _update(text_update(101, chat_id=101, text="hello again")),
        transport,
        session=store,
        gate=FakeGate(),
        llm=FakeLLM(),
        converter=FakeConverter(),
        scripter=scripter,
    )

    assert sent is True
    assert len(scripter.calls) == 1
    texts = [call["text"] for call in transport.calls_for("sendMessage")]
    assert texts[-1] == "A dramatic nature documentary script."
    assert store.get(101).script_sent is True


def test_done_with_a_stored_script_resends_it_without_calling_the_scripter() -> None:
    # A failed narration *send* leaves the script stored but undelivered; the
    # next update re-sends that exact text through the Bouncer route and never
    # asks the Scripter to generate a second narration.
    store = _scripted_store(103)
    store.store_script(103, "The stored narration, verbatim.")
    transport = FakeTransport()
    scripter = FakeScripter()

    sent = handle_update(
        _update(text_update(103, chat_id=103, text="hello again")),
        transport,
        session=store,
        gate=FakeGate(),
        llm=FakeLLM(),
        converter=FakeConverter(),
        scripter=scripter,
    )

    assert sent is True
    assert scripter.calls == []
    texts = [call["text"] for call in transport.calls_for("sendMessage")]
    assert texts[-1] == "The stored narration, verbatim."
    assert store.get(103).script == "The stored narration, verbatim."
    assert store.get(103).script_sent is True


def test_handoff_scripting_failure_leaves_the_chat_retryable() -> None:
    # If the script call fails right after the portrait handoff, the flag is
    # never set; a later update retries and delivers the narration exactly once.
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True)])
    llm = FakeLLM(
        dossiers=[Dossier(summary="S", suggested_animal="Capybara")]
    )
    scripter = FakeScripter(scripts=[ScriptError("boom")])
    transport = _photo_transport()

    handle_update(
        _update(photo_update(31, chat_id=31)),
        transport,
        session=store,
        gate=gate,
        llm=llm,
        converter=FakeConverter(),
        scripter=scripter,
    )
    for number in range(1, 6):
        sent = handle_update(
            _update(text_update(31 + number, chat_id=31, text=f"a{number}")),
            transport,
            session=store,
            gate=gate,
            llm=llm,
            converter=FakeConverter(),
            scripter=scripter,
        )

    assert sent is False
    state = store.get(31)
    assert state.phase is Phase.done
    assert state.portrait_sent is True
    assert state.script_sent is False
    assert state.script is None
    # The failure apologises in-band and asks the user to prompt the retry.
    texts = [call["text"] for call in transport.calls_for("sendMessage")]
    assert texts[-1] == SCRIPT_APOLOGY_REPLY
    assert "A dramatic nature documentary script." not in texts

    retried = handle_update(
        _update(text_update(41, chat_id=31, text="go")),
        transport,
        session=store,
        gate=gate,
        llm=llm,
        converter=FakeConverter(),
        scripter=scripter,
    )

    assert retried is True
    assert store.get(31).script_sent is True
    assert store.get(31).script == "A dramatic nature documentary script."
    assert transport.calls_for("sendMessage")[-1]["text"] == (
        "A dramatic nature documentary script."
    )


def test_start_clears_the_script_and_script_sent_flag() -> None:
    store = _scripted_store(51, script_sent=True)
    transport = FakeTransport()

    sent = handle_update(
        _update(text_update(51, chat_id=51, text="/start")),
        transport,
        session=store,
        gate=FakeGate(),
        llm=FakeLLM(),
        converter=FakeConverter(),
        scripter=FakeScripter(),
    )

    assert sent is True
    state = store.get(51)
    assert state.phase is Phase.awaiting_photo
    assert state.portrait_sent is False
    assert state.script is None
    assert state.script_sent is False


def test_start_clears_the_portrait_sent_flag() -> None:
    store = _completed_store(50, portrait_sent=True)
    transport = FakeTransport()

    sent = handle_update(
        _update(text_update(50, chat_id=50, text="/start")),
        transport,
        session=store,
        gate=FakeGate(),
        llm=FakeLLM(),
        converter=FakeConverter(),
        scripter=FakeScripter(),
        narrator=FakeNarrator(),
    )

    assert sent is True
    state = store.get(50)
    assert state.phase is Phase.awaiting_photo
    assert state.portrait_sent is False


# ------------------------------------------------------------ narrator dispatch
#
# Phase 6 hands the final stage to the Narrator. The same-update chain now ends
# with ``sendAudio``: after the script lands the audio message follows with no
# user input, and a ``done && script_sent && !narrator_sent`` update re-drives
# narration (re-sending stored bytes, never re-synthesising).

NARRATION_AUDIO = b"ID3\x03\x00\x00\x00narration-audio-bytes"
NARRATION_SCRIPT = "A dramatic nature documentary script."


def _narrated_store(
    chat_id: int,
    *,
    script_sent: bool = True,
    narration: bytes | None = None,
    narrator_sent: bool = False,
) -> SessionStore:
    """A store whose chat has a portrait and (by default) a delivered script."""
    store = _completed_store(chat_id, portrait_sent=True)
    store.store_script(chat_id, NARRATION_SCRIPT)
    if script_sent:
        store.mark_script_sent(chat_id)
    if narration is not None:
        store.store_narration(chat_id, narration)
    if narrator_sent:
        store.mark_narrator_sent(chat_id)
    return store


def test_done_with_a_script_but_no_audio_routes_to_the_narrator() -> None:
    store = _narrated_store(200)
    transport = FakeTransport()
    narrator = FakeNarrator(
        [AudioPayload(data=NARRATION_AUDIO, mime_type="audio/mpeg")]
    )

    sent = handle_update(
        _update(text_update(200, chat_id=200, text="hello again")),
        transport,
        session=store,
        gate=FakeGate(),
        llm=FakeLLM(),
        converter=FakeConverter(),
        scripter=FakeScripter(),
        narrator=narrator,
    )

    assert sent is True
    assert narrator.calls == [NARRATION_SCRIPT]
    assert len(transport.audios) == 1
    assert transport.audios[0]["audio"] == NARRATION_AUDIO
    assert transport.audios[0]["media_type"] == "audio/mpeg"
    assert transport.audios[0]["filename"] == "narration.mp3"
    assert store.get(200).narrator_sent is True


def test_done_with_stored_audio_resends_it_without_resynthesising() -> None:
    stored = b"ID3\x03\x00\x00\x00stored-audio-verbatim"
    store = _narrated_store(201, narration=stored)
    transport = FakeTransport()
    narrator = FakeNarrator([NarratorError("must never be called")])

    sent = handle_update(
        _update(text_update(201, chat_id=201, text="hello again")),
        transport,
        session=store,
        gate=FakeGate(),
        llm=FakeLLM(),
        converter=FakeConverter(),
        scripter=FakeScripter(),
        narrator=narrator,
    )

    assert sent is True
    assert narrator.calls == []
    assert len(transport.audios) == 1
    assert transport.audios[0]["audio"] == stored
    assert transport.audios[0]["media_type"] == "audio/mpeg"
    assert store.get(201).narrator_sent is True


def test_same_update_chain_runs_the_narrator_after_the_script() -> None:
    # The whole chain in one pass: a5 completes the interview, then the
    # Converter, Scripter and Narrator all run in that same update so the audio
    # message follows the script with no user input in between.
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True)])
    llm = FakeLLM(
        dossiers=[Dossier(summary="S", suggested_animal="Capybara")]
    )
    converter = FakeConverter()
    scripter = FakeScripter()
    narrator = FakeNarrator()
    transport = _photo_transport()

    handle_update(
        _update(photo_update(1, chat_id=210)),
        transport,
        session=store,
        gate=gate,
        llm=llm,
        converter=converter,
        scripter=scripter,
        narrator=narrator,
    )
    for number in range(1, 5):
        handle_update(
            _update(text_update(10 + number, chat_id=210, text=f"a{number}")),
            transport,
            session=store,
            gate=gate,
            llm=llm,
            converter=converter,
            scripter=scripter,
            narrator=narrator,
        )
    sent = handle_update(
        _update(text_update(20, chat_id=210, text="a5")),
        transport,
        session=store,
        gate=gate,
        llm=llm,
        converter=converter,
        scripter=scripter,
        narrator=narrator,
    )

    assert sent is True
    state = store.get(210)
    assert state.script_sent is True
    assert state.narrator_sent is True
    assert len(scripter.calls) == 1
    assert narrator.calls == [NARRATION_SCRIPT]
    assert len(transport.audios) == 1
    assert transport.audios[0]["media_type"] == "audio/mpeg"


def test_done_and_narrated_keeps_the_light_reply() -> None:
    store = _narrated_store(202, narration=NARRATION_AUDIO, narrator_sent=True)
    transport = FakeTransport()
    narrator = FakeNarrator()

    sent = handle_update(
        _update(text_update(202, chat_id=202, text="hello again")),
        transport,
        session=store,
        gate=FakeGate(),
        llm=FakeLLM(),
        converter=FakeConverter(),
        scripter=FakeScripter(),
        narrator=narrator,
    )

    assert sent is True
    assert [c["text"] for c in transport.calls_for("sendMessage")] == [
        OBSERVATION_COMPLETE_REPLY
    ]
    assert narrator.calls == []
    assert transport.audios == []


def test_handoff_narrator_failure_leaves_the_chat_retryable() -> None:
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True)])
    llm = FakeLLM(
        dossiers=[Dossier(summary="S", suggested_animal="Capybara")]
    )
    converter = FakeConverter()
    scripter = FakeScripter()
    narrator = FakeNarrator([NarratorError("boom")])
    transport = _photo_transport()

    handle_update(
        _update(photo_update(1, chat_id=211)),
        transport,
        session=store,
        gate=gate,
        llm=llm,
        converter=converter,
        scripter=scripter,
        narrator=narrator,
    )
    sent = None
    for number in range(1, 6):
        sent = handle_update(
            _update(text_update(10 + number, chat_id=211, text=f"a{number}")),
            transport,
            session=store,
            gate=gate,
            llm=llm,
            converter=converter,
            scripter=scripter,
            narrator=narrator,
        )

    assert sent is False
    state = store.get(211)
    assert state.script_sent is True
    assert state.narrator_sent is False
    assert state.narration is None
    assert NARRATION_APOLOGY_REPLY in [
        c["text"] for c in transport.calls_for("sendMessage")
    ]

    retried = handle_update(
        _update(text_update(30, chat_id=211, text="go")),
        transport,
        session=store,
        gate=gate,
        llm=llm,
        converter=converter,
        scripter=scripter,
        narrator=narrator,
    )

    assert retried is True
    assert store.get(211).narrator_sent is True
    assert len(transport.audios) == 1


def test_start_clears_the_narration_and_the_narrator_sent_flag() -> None:
    store = _narrated_store(203, narration=NARRATION_AUDIO, narrator_sent=True)
    transport = FakeTransport()

    sent = handle_update(
        _update(text_update(203, chat_id=203, text="/start")),
        transport,
        session=store,
        gate=FakeGate(),
        llm=FakeLLM(),
        converter=FakeConverter(),
        scripter=FakeScripter(),
        narrator=FakeNarrator(),
    )

    assert sent is True
    state = store.get(203)
    assert state.phase is Phase.awaiting_photo
    assert state.narration is None
    assert state.narrator_sent is False


# --- Phase 7: explicit reset routine + command recognition ------------------
#
# The reset is a named routine (``reset_chat``) rather than an inline call so
# the three MISSION guarantees — cancel the session, purge in-memory media and
# re-initialize agent memory — happen in one place and are logged as one
# structured ``session_reset`` event describing exactly what was discarded.


def test_reset_chat_returns_a_report_and_logs_one_session_reset_record() -> None:
    store = SessionStore()
    store.record_pass(chat_id=90, photo=b"PORTRAIT-BYTES")
    store.start_interview(90, question="Q1")
    store.record_interview_answer(90, "Q1", "a1")
    store.store_script(90, script="A dramatic narration.")
    store.mark_script_sent(90)
    store.store_narration(90, audio=b"OggS\x00narration-bytes")
    store.remember_media_group(90, media_group_id="album-9")

    with _capture("telegram_documentaries.bouncer") as (logger, records):
        report = reset_chat(store, 90, update_id=555, log=logger)

    assert isinstance(report, PurgeReport)
    assert report.chat_id == 90
    assert report.previous_phase is Phase.interviewing
    assert report.discarded_photo is True
    assert report.discarded_script is True
    assert report.discarded_narration is True
    assert report.discarded_interview_answers == 1
    assert report.previous_media_group_id == "album-9"

    resets = [r for r in records if getattr(r, "event", "") == "session_reset"]
    assert len(resets) == 1
    record = resets[0]
    assert record.levelno == logging.INFO
    assert getattr(record, "chat_id") == 90
    assert getattr(record, "update_id") == 555
    assert getattr(record, "previous_phase") == "interviewing"
    assert getattr(record, "discarded_photo") is True
    assert getattr(record, "discarded_script") is True
    assert getattr(record, "discarded_narration") is True
    assert getattr(record, "discarded_interview_answers") == 1
    assert getattr(record, "discarded_dossier") is False
    assert getattr(record, "previous_media_group_id") == "album-9"
    # Reset observability must never carry media bytes or user-derived text.
    snapshot = str(record.__dict__)
    assert "PORTRAIT-BYTES" not in snapshot
    assert "OggS" not in snapshot
    assert "A dramatic narration." not in snapshot
    # And the chat is genuinely purged, not just described.
    state = store.get(90)
    assert state.phase is Phase.awaiting_photo
    assert state.photo is None
    assert state.narration is None
    assert state.script is None
    assert state.interview == []
    assert state.last_media_group_id is None


def test_reset_chat_on_a_fresh_chat_reports_an_empty_purge() -> None:
    store = SessionStore()

    with _capture("telegram_documentaries.bouncer") as (logger, records):
        report = reset_chat(store, 91, update_id=1, log=logger)

    assert report.chat_id == 91
    assert report.previous_phase is Phase.awaiting_photo
    assert report.discarded_photo is False
    assert report.discarded_script is False
    assert report.discarded_narration is False
    assert report.discarded_interview_answers == 0
    assert report.discarded_dossier is False
    assert report.previous_media_group_id is None
    assert [getattr(r, "event", "") for r in records].count("session_reset") == 1


def test_command_extraction_reads_text_then_caption_case_insensitively() -> None:
    assert _command(_message(text_update(1, chat_id=1, text="/Stop@SomeBot now"))) == (
        "/stop"
    )
    assert _command(
        _message(photo_update(2, chat_id=1, caption="/ReStart@SomeBot"))
    ) == "/restart"
    assert _command(_message(photo_update(3, chat_id=1, caption="my cat"))) == ""
    assert _command(_message(document_update(4, chat_id=1))) == ""


@pytest.mark.parametrize(
    "text",
    [
        "/start",
        "/restart",
        "/Start",
        "/START",
        "/ReStart",
        "/start@MiargueBot",
        "/restart@somebot",
        "/restart now",
        "/start ",
    ],
)
def test_start_command_variants_reset_and_prompt_for_a_photo(text: str) -> None:
    store = SessionStore()
    store.record_pass(chat_id=92, photo=b"OLD-PORTRAIT")
    gate = FakeGate()
    transport = FakeTransport()

    with _capture("telegram_documentaries.bouncer") as (logger, records):
        sent = handle_update(
            _update(text_update(1, chat_id=92, text=text)),
            transport,
            session=store,
            gate=gate,
            converter=FakeConverter(),
            scripter=FakeScripter(),
            llm=FakeLLM(),
            logger=logger,
        )

    assert sent is True
    assert transport.calls_for("sendMessage") == [
        {"chat_id": 92, "text": PROMPT_PHOTO}
    ]
    assert gate.calls == 0
    state = store.get(92)
    assert state.phase is Phase.awaiting_photo
    assert state.photo is None
    events = _events(records)
    assert "session_reset" in events
    prompted = [r for r in records if getattr(r, "event", "") == "bouncer_prompted"]
    assert getattr(prompted[-1], "reason") == "command"


def test_photo_captioned_start_resets_and_is_not_gated() -> None:
    store = SessionStore()
    store.record_pass(chat_id=93, photo=b"PREVIOUS")
    gate = FakeGate([HumanVerdict(is_human=True, reason="a face")])
    transport = _photo_transport()

    sent = handle_update(
        _update(photo_update(5, chat_id=93, caption="/start@MiargueBot")),
        transport,
        session=store,
        gate=gate,
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
    )

    assert sent is True
    assert transport.calls_for("sendMessage") == [
        {"chat_id": 93, "text": PROMPT_PHOTO}
    ]
    # A command caption is a reset, not a portrait: the photo is never fetched
    # and the gate never sees it.
    assert gate.calls == 0
    assert transport.calls_for("getFile") == []
    state = store.get(93)
    assert state.phase is Phase.awaiting_photo
    assert state.photo is None


def test_photo_with_a_non_command_caption_is_gated_as_normal() -> None:
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True, reason="a face")])
    transport = _photo_transport()

    sent = handle_update(
        _update(photo_update(6, chat_id=94, caption="my cat")),
        transport,
        session=store,
        gate=gate,
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
    )

    assert sent is True
    assert gate.calls == 1
    assert gate.images == [b"PORTRAIT-BYTES"]
    assert transport.calls_for("sendMessage")[0]["text"] == SUCCESS_REPLY
    assert store.get(94).phase is Phase.interviewing


def test_slash_non_command_text_re_prompts_without_crashing() -> None:
    store = SessionStore()
    gate = FakeGate()
    transport = FakeTransport()

    sent = handle_update(
        _update(text_update(7, chat_id=95, text="/help")),
        transport,
        session=store,
        gate=gate,
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
    )

    assert sent is True
    assert transport.calls_for("sendMessage")[0]["text"] == PROMPT_PHOTO
    assert gate.calls == 0
    assert store.get(95).phase is Phase.awaiting_photo


def test_gate_rejection_uses_the_reset_routine_and_logs_session_reset() -> None:
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=False, reason="a dog")])
    transport = _photo_transport()

    with _capture("telegram_documentaries.bouncer") as (logger, records):
        sent = handle_update(
            _update(photo_update(8, chat_id=96)),
            transport,
            session=store,
            gate=gate,
            converter=FakeConverter(),
            scripter=FakeScripter(),
            llm=FakeLLM(),
            logger=logger,
        )

    assert sent is True
    events = _events(records)
    # The reset is logged discretely, and before the rejection it accompanies.
    assert "session_reset" in events
    assert events.index("session_reset") < events.index("bouncer_rejected")
    record = next(r for r in records if getattr(r, "event", "") == "session_reset")
    assert record.levelno == logging.INFO
    assert getattr(record, "chat_id") == 96
    # The rejected photo was never retained (only a *human* verdict stores it),
    # so the routine truthfully reports an empty outgoing state.
    assert getattr(record, "previous_phase") == "awaiting_photo"
    assert getattr(record, "discarded_photo") is False
    assert transport.calls_for("sendMessage")[0]["text"] == REJECTION_REPLY
    assert store.get(96).photo is None


@pytest.mark.parametrize(
    "phase",
    [Phase.awaiting_photo, Phase.gate_passed, Phase.interviewing, Phase.done],
)
@pytest.mark.parametrize(
    "as_caption",
    [False, True],
    ids=["text_command", "command_as_photo_caption"],
)
def test_start_command_resets_at_every_phase(phase: Phase, as_caption: bool) -> None:
    store = SessionStore()
    if phase is not Phase.awaiting_photo:
        store.record_pass(chat_id=97, photo=b"PORTRAIT")
    if phase in (Phase.interviewing, Phase.done):
        store.start_interview(97, question="Q1")
    if phase is Phase.done:
        store.appoint_dossier(
            97, Dossier(summary="S", suggested_animal="Capybara")
        )
        store.mark_portrait_sent(97)
        store.mark_script_sent(97)
    gate = FakeGate()
    transport = FakeTransport()
    # A ``/start`` reset works whether the command arrives as message text or
    # as a photo caption — at every phase, not just the first.
    payload = (
        photo_update(9, chat_id=97, caption="/start")
        if as_caption
        else text_update(9, chat_id=97, text="/restart")
    )

    sent = handle_update(
        _update(payload),
        transport,
        session=store,
        gate=gate,
        converter=FakeConverter(),
        scripter=FakeScripter(),
        llm=FakeLLM(),
        narrator=FakeNarrator(),
    )

    assert sent is True
    assert transport.calls_for("sendMessage") == [
        {"chat_id": 97, "text": PROMPT_PHOTO}
    ]
    assert gate.calls == 0
    state = store.get(97)
    assert state.phase is Phase.awaiting_photo
    assert state.photo is None
    assert state.interview == []
    assert state.dossier is None
    assert state.portrait_sent is False
    assert state.script is None
    assert state.script_sent is False
    assert state.narration is None
    assert state.narrator_sent is False


# --- Phase 7: phase × content guard matrix ----------------------------------
#
# A wrong payload at any stage must never raise into the polling loop: it is
# either answered with the stage's documented reply, a benign skip, or a
# deduplicated album drop — and the session is always left in a coherent state.

_MATRIX_CHAT = 300
_ALBUM_ID = "album-x"


def _matrix_payload(kind: str, update_id: int) -> dict[str, Any]:
    chat_id = _MATRIX_CHAT
    if kind == "text":
        return text_update(update_id, chat_id=chat_id, text="an answer")
    if kind == "empty_text":
        return text_update(update_id, chat_id=chat_id, text="   ")
    if kind == "photo":
        return photo_update(update_id, chat_id=chat_id)
    if kind == "photo_caption":
        return photo_update(update_id, chat_id=chat_id, caption="my cat")
    if kind.startswith("unsupported"):
        return document_update(
            update_id, chat_id=chat_id, kind=kind.partition("_")[2]
        )
    if kind == "messageless":
        return messageless_update(update_id)
    if kind == "album_sibling":
        return photo_update(update_id, chat_id=chat_id, media_group_id=_ALBUM_ID)
    if kind == "command":
        return text_update(update_id, chat_id=chat_id, text="/restart")
    if kind == "command_caption":
        return photo_update(update_id, chat_id=chat_id, caption="/start")
    raise AssertionError(f"unknown content kind: {kind}")


def _matrix_store(phase: Phase, *, variant: str = "") -> SessionStore:
    """Build the session a chat is in for one matrix cell."""
    store = SessionStore()
    if phase is Phase.awaiting_photo:
        return store
    store.record_pass(_MATRIX_CHAT, photo=b"PORTRAIT")
    if phase is Phase.gate_passed:
        return store
    store.start_interview(_MATRIX_CHAT, question="Q1")
    if phase is Phase.interviewing:
        return store
    store.appoint_dossier(
        _MATRIX_CHAT, Dossier(summary="S", suggested_animal="Capybara")
    )
    # phase is Phase.done — the variant selects which terminal flags are set.
    if variant == "incomplete_portrait":
        return store  # neither portrait nor script delivered
    store.mark_portrait_sent(_MATRIX_CHAT)
    if variant == "missing_script":
        return store
    store.store_script(_MATRIX_CHAT, script="A dramatic narration.")
    store.mark_script_sent(_MATRIX_CHAT)
    if variant == "missing_audio":
        return store
    store.store_narration(_MATRIX_CHAT, audio=b"OggS\x00audio")
    store.mark_narrator_sent(_MATRIX_CHAT)
    return store  # variant == "complete"


def _run_matrix(
    kind: str,
    *,
    store: SessionStore,
    update_id: int = 1,
    narrator: FakeNarrator | None = None,
) -> tuple[Any, FakeTransport, FakeGate, FakeLLM, FakeConverter, FakeScripter, list]:
    """Drive one matrix cell; returns the outcome and every collaborator."""
    transport = _photo_transport()
    gate = FakeGate()
    llm = FakeLLM()
    converter = FakeConverter()
    scripter = FakeScripter()
    with _capture("telegram_documentaries.bouncer") as (logger, records):
        sent = handle_update(
            _update(_matrix_payload(kind, update_id)),
            transport,
            session=store,
            gate=gate,
            converter=converter,
            scripter=scripter,
            llm=llm,
            narrator=narrator,
            logger=logger,
        )
    return sent, transport, gate, llm, converter, scripter, records


_UNSUPPORTED = ["unsupported_sticker", "unsupported_voice", "unsupported_document"]

_AWAITING_PHOTO = {
    "text": (PROMPT_PHOTO, False),
    "empty_text": (PROMPT_PHOTO, False),
    "unsupported_sticker": (PROMPT_PHOTO, False),
    "unsupported_voice": (PROMPT_PHOTO, False),
    "unsupported_document": (PROMPT_PHOTO, False),
    "photo": (None, False),  # gated instead of answered
    "photo_caption": (None, False),  # a non-command caption is still a photo
    "messageless": (None, True),
    "album_sibling": (None, True),
    "command": (PROMPT_PHOTO, False),
    "command_caption": (PROMPT_PHOTO, False),
}


@pytest.mark.parametrize("kind", list(_AWAITING_PHOTO))
def test_guard_matrix_awaiting_photo(kind: str) -> None:
    expected_reply, skipped = _AWAITING_PHOTO[kind]
    store = _matrix_store(Phase.awaiting_photo)
    if kind == "album_sibling":
        store.remember_media_group(_MATRIX_CHAT, _ALBUM_ID)

    sent, transport, gate, *_ = _run_matrix(kind, store=store)

    if skipped:
        assert sent is SKIPPED
        assert transport.calls_for("sendMessage") == []
    else:
        assert sent is True
    if expected_reply is not None:
        assert transport.calls_for("sendMessage")[-1]["text"] == expected_reply
    if kind in ("photo", "photo_caption"):
        assert gate.calls == 1
        assert store.get(_MATRIX_CHAT).phase is Phase.interviewing
    else:
        assert gate.calls == 0
        assert store.get(_MATRIX_CHAT).phase is Phase.awaiting_photo


@pytest.mark.parametrize(
    "kind",
    ["text", "empty_text", *_UNSUPPORTED, "photo", "photo_caption"],
)
def test_guard_matrix_gate_passed_retries_the_start(kind: str) -> None:
    # A chat left in ``gate_passed`` (degraded start) is retried by the
    # Interviewer for *any* content: no re-gating, no lost session.
    store = _matrix_store(Phase.gate_passed)

    sent, transport, gate, llm, *_ = _run_matrix(kind, store=store)

    assert sent is True
    assert transport.calls_for("sendMessage")[-1]["text"] == "Q1"
    assert gate.calls == 0
    assert llm.next_calls == [[]]
    assert store.get(_MATRIX_CHAT).phase is Phase.interviewing


@pytest.mark.parametrize("kind", ["messageless", "album_sibling"])
def test_guard_matrix_gate_passed_benign_skips(kind: str) -> None:
    store = _matrix_store(Phase.gate_passed)
    if kind == "album_sibling":
        store.remember_media_group(_MATRIX_CHAT, _ALBUM_ID)

    sent, transport, gate, *_ = _run_matrix(kind, store=store)

    assert sent is SKIPPED
    assert transport.calls_for("sendMessage") == []
    assert gate.calls == 0
    assert store.get(_MATRIX_CHAT).phase is Phase.gate_passed


_INTERVIEWING = {
    "text": "Q2",  # a real answer draws the next question
    "empty_text": "Q1",  # anything else re-asks the stored pending question
    "photo": "Q1",
    "photo_caption": "Q1",
    "unsupported_sticker": "Q1",
    "unsupported_voice": "Q1",
    "unsupported_document": "Q1",
}


@pytest.mark.parametrize("kind", list(_INTERVIEWING))
def test_guard_matrix_interviewing(kind: str) -> None:
    store = _matrix_store(Phase.interviewing)

    sent, transport, gate, llm, *_ = _run_matrix(kind, store=store)

    assert sent is True
    assert transport.calls_for("sendMessage")[-1]["text"] == _INTERVIEWING[kind]
    assert gate.calls == 0
    state = store.get(_MATRIX_CHAT)
    assert state.phase is Phase.interviewing
    if kind == "text":
        assert state.interview == [QaPair(question="Q1", answer="an answer")]
        assert state.pending_question == "Q2"
        assert llm.next_calls == [
            [QaPair(question="Q1", answer="an answer")]
        ]
    else:
        # A non-answer advances nothing and keeps the same outstanding question.
        assert state.interview == []
        assert state.pending_question == "Q1"
        assert llm.next_calls == []


@pytest.mark.parametrize("kind", ["messageless", "album_sibling"])
def test_guard_matrix_interviewing_benign_skips(kind: str) -> None:
    store = _matrix_store(Phase.interviewing)
    if kind == "album_sibling":
        store.remember_media_group(_MATRIX_CHAT, _ALBUM_ID)

    sent, transport, *_ = _run_matrix(kind, store=store)

    assert sent is SKIPPED
    assert transport.calls_for("sendMessage") == []
    assert store.get(_MATRIX_CHAT).interview == []
    assert store.get(_MATRIX_CHAT).pending_question == "Q1"


@pytest.mark.parametrize(
    "kind",
    ["text", "empty_text", *_UNSUPPORTED, "photo", "photo_caption"],
)
def test_guard_matrix_done_incomplete_portrait_retries_conversion(
    kind: str,
) -> None:
    store = _matrix_store(Phase.done, variant="incomplete_portrait")

    sent, transport, gate, _, converter, scripter, _ = _run_matrix(
        kind, store=store
    )

    assert sent is True
    assert gate.calls == 0
    assert len(converter.calls) == 1
    assert len(scripter.calls) == 1
    assert len(transport.photos) == 1
    state = store.get(_MATRIX_CHAT)
    assert state.portrait_sent is True
    assert state.script_sent is True


@pytest.mark.parametrize(
    "kind",
    ["text", "empty_text", *_UNSUPPORTED, "photo", "photo_caption"],
)
def test_guard_matrix_done_missing_script_retries_scripting(kind: str) -> None:
    store = _matrix_store(Phase.done, variant="missing_script")

    sent, transport, gate, _, converter, scripter, _ = _run_matrix(
        kind, store=store
    )

    assert sent is True
    assert gate.calls == 0
    assert converter.calls == []  # the portrait already landed
    assert len(scripter.calls) == 1
    assert transport.photos == []
    assert store.get(_MATRIX_CHAT).script_sent is True


@pytest.mark.parametrize(
    "kind",
    ["text", "empty_text", *_UNSUPPORTED, "photo", "photo_caption"],
)
def test_guard_matrix_done_missing_audio_synthesises(kind: str) -> None:
    store = _matrix_store(Phase.done, variant="missing_audio")
    narrator = FakeNarrator()

    sent, transport, gate, _, converter, scripter, _ = _run_matrix(
        kind, store=store, narrator=narrator
    )

    assert sent is True
    assert gate.calls == 0
    assert converter.calls == []
    assert scripter.calls == []
    assert len(transport.audios) == 1
    assert narrator.calls == ["A dramatic narration."]
    assert store.get(_MATRIX_CHAT).narrator_sent is True


@pytest.mark.parametrize(
    "kind",
    ["text", "empty_text", *_UNSUPPORTED, "photo", "photo_caption"],
)
def test_guard_matrix_done_complete_replies_lightly(kind: str) -> None:
    store = _matrix_store(Phase.done, variant="complete")
    narrator = FakeNarrator()

    sent, transport, gate, _, converter, scripter, _ = _run_matrix(
        kind, store=store, narrator=narrator
    )

    assert sent is True
    assert transport.calls_for("sendMessage")[-1]["text"] == (
        OBSERVATION_COMPLETE_REPLY
    )
    assert gate.calls == 0
    assert converter.calls == []
    assert scripter.calls == []
    assert transport.audios == []


@pytest.mark.parametrize(
    "kind,expected_reply,skipped",
    [
        ("command", PROMPT_PHOTO, False),
        ("command_caption", PROMPT_PHOTO, False),
        ("messageless", None, True),
        ("album_sibling", None, True),
    ],
)
def test_guard_matrix_done_terminal_contents(
    kind: str, expected_reply: str | None, skipped: bool
) -> None:
    store = _matrix_store(Phase.done, variant="complete")
    if kind == "album_sibling":
        store.remember_media_group(_MATRIX_CHAT, _ALBUM_ID)

    sent, transport, *_ = _run_matrix(kind, store=store)

    if skipped:
        assert sent is SKIPPED
        assert transport.calls_for("sendMessage") == []
    else:
        assert sent is True
        assert transport.calls_for("sendMessage")[-1]["text"] == expected_reply
        # A command resets a fully-complete chat, not just re-prompts.
        state = store.get(_MATRIX_CHAT)
        assert state.phase is Phase.awaiting_photo
        assert state.portrait_sent is False


def test_a_reset_logs_session_reset_and_a_normal_update_does_not() -> None:
    # The log record is the observable proof a reset happened: a reset emits
    # exactly one; an ordinary prompt/re-ask emits none.
    reset_store = _matrix_store(Phase.done, variant="complete")
    _, _, _, _, _, _, reset_records = _run_matrix("command", store=reset_store)
    assert _events(reset_records).count("session_reset") == 1

    normal_store = _matrix_store(Phase.awaiting_photo)
    _, _, _, _, _, _, normal_records = _run_matrix("text", store=normal_store)
    assert "session_reset" not in _events(normal_records)


