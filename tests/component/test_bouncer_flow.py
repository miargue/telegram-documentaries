"""Component: the Bouncer conversation over the real ``poll_once`` loop.

Honest label: no real I/O. The real polling loop, update parsing, Bouncer
routing, media intake and session driver all run; only Telegram
(``FakeTransport``) and Gemini (``FakeGate``) are double, so the whole
conversation is exercised without a network.
"""

from __future__ import annotations

from conftest import FakeGate, FakeTransport, photo_update, text_update

from telegram_documentaries.bouncer import (
    APOLOGY_REPLY,
    PROMPT_PHOTO,
    REJECTION_REPLY,
    SUCCESS_REPLY,
)
from telegram_documentaries.polling import poll_once
from telegram_documentaries.session import Phase, SessionStore
from telegram_documentaries.vision import HumanVerdict, VisionError

PORTRAIT_BYTES = b"PORTRAIT-BYTES"


def _transport(
    batch: list[dict[str, object]],
) -> FakeTransport:
    return FakeTransport(
        batches=[batch],
        files={"abc123": ("photos/a.jpg", PORTRAIT_BYTES)},
    )


def _texts(transport: FakeTransport) -> list[str]:
    return [message["text"] for message in transport.calls_for("sendMessage")]


def test_start_then_non_human_photo_rejects_and_resets_the_session() -> None:
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=False, reason="a cat")])
    transport = _transport(
        [
            text_update(1, chat_id=10, text="/start"),
            photo_update(2, chat_id=10),
        ]
    )

    poll_once(transport, offset=0, session=store, gate=gate)

    assert _texts(transport) == [PROMPT_PHOTO, REJECTION_REPLY]
    state = store.get(10)
    assert state.phase is Phase.awaiting_photo
    assert state.photo is None


def test_human_photo_passes_and_retains_the_photo() -> None:
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True, reason="a face")])
    transport = _transport([photo_update(1, chat_id=20)])

    poll_once(transport, offset=0, session=store, gate=gate)

    assert _texts(transport) == [SUCCESS_REPLY]
    state = store.get(20)
    assert state.phase is Phase.gate_passed
    assert state.photo == PORTRAIT_BYTES


def test_text_after_pass_re_prompts_without_losing_the_session() -> None:
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True)])
    transport = _transport(
        [
            photo_update(1, chat_id=30),
            text_update(2, chat_id=30, text="what happens now?"),
        ]
    )

    poll_once(transport, offset=0, session=store, gate=gate)

    assert _texts(transport) == [SUCCESS_REPLY, PROMPT_PHOTO]
    # The re-prompt must not purge the accepted portrait.
    state = store.get(30)
    assert state.phase is Phase.gate_passed
    assert state.photo == PORTRAIT_BYTES


def test_full_conversation_sequence_over_one_batch() -> None:
    store = SessionStore()
    gate = FakeGate(
        [
            HumanVerdict(is_human=False, reason="a landscape"),
            HumanVerdict(is_human=True, reason="a face"),
        ]
    )
    transport = _transport(
        [
            text_update(1, chat_id=40, text="/start"),
            photo_update(2, chat_id=40),  # landscape -> rejected + reset
            photo_update(3, chat_id=40),  # portrait -> passes
            text_update(4, chat_id=40, text="hello"),  # re-prompt
        ]
    )

    poll_once(transport, offset=0, session=store, gate=gate)

    assert _texts(transport) == [
        PROMPT_PHOTO,
        REJECTION_REPLY,
        SUCCESS_REPLY,
        PROMPT_PHOTO,
    ]
    state = store.get(40)
    assert state.phase is Phase.gate_passed
    assert state.photo == PORTRAIT_BYTES


def test_album_batch_uses_only_the_first_photo() -> None:
    # Telegram delivers an album as one update per photo sharing a
    # media_group_id; the first is gated and the rest must be dropped silently
    # so the user sees a single verdict.
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True, reason="a face")])
    transport = _transport(
        [
            photo_update(1, chat_id=60, media_group_id="album-1"),
            photo_update(2, chat_id=60, media_group_id="album-1"),
            photo_update(3, chat_id=60, media_group_id="album-1"),
        ]
    )

    poll_once(transport, offset=0, session=store, gate=gate)

    assert _texts(transport) == [SUCCESS_REPLY]
    assert gate.calls == 1
    state = store.get(60)
    assert state.phase is Phase.gate_passed
    assert state.photo == PORTRAIT_BYTES


def test_gate_failure_degrades_then_a_later_photo_still_passes() -> None:
    store = SessionStore()
    gate = FakeGate(
        [
            VisionError("gemini exploded"),
            HumanVerdict(is_human=True),
        ]
    )
    transport = _transport(
        [
            photo_update(1, chat_id=50),
            photo_update(2, chat_id=50),
        ]
    )

    poll_once(transport, offset=0, session=store, gate=gate)

    assert _texts(transport) == [APOLOGY_REPLY, SUCCESS_REPLY]
    assert store.get(50).phase is Phase.gate_passed
