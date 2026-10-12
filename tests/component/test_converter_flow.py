"""Component: the whole pipeline over the real ``poll_once`` loop.

Honest label: no real I/O. The real polling loop, update parsing, Bouncer
routing/handoff, Interviewer turn-taking, Converter routing and session driver
all run; only Telegram (``FakeTransport``), the vision gate (``FakeGate``), the
interview model (``FakeLLM``) and the image model (``FakeConverter``) are
doubles, so the whole pass → 5-answer → dossier → hybrid-portrait conversation
is exercised without a network.
"""

from __future__ import annotations

import logging

from conftest import (
    FakeConverter,
    FakeGate,
    FakeLLM,
    FakeScripter,
    FakeTransport,
    _RecordingHandler,
    photo_update,
    text_update,
)

from telegram_documentaries.bouncer import SUCCESS_REPLY
from telegram_documentaries.converter import TEASER_REPLY, ConverterError
from telegram_documentaries.interviewer import OBSERVATION_COMPLETE_REPLY
from telegram_documentaries.polling import poll_once
from telegram_documentaries.session import Dossier, Phase, QaPair, SessionStore
from telegram_documentaries.vision import HumanVerdict

PORTRAIT_BYTES = b"PORTRAIT-BYTES"


def _transport(batches: list[list[dict[str, object]]]) -> FakeTransport:
    return FakeTransport(
        batches=batches,
        files={"abc123": ("photos/a.jpg", PORTRAIT_BYTES)},
    )


def _texts(transport: FakeTransport) -> list[str]:
    return [message["text"] for message in transport.calls_for("sendMessage")]


def _poll(
    transport: FakeTransport,
    store: SessionStore,
    gate: FakeGate,
    llm: FakeLLM,
    converter: FakeConverter,
    *,
    offset: int,
) -> int:
    return poll_once(
        transport,
        offset=offset,
        session=store,
        gate=gate,
        llm=llm,
        converter=converter,
        scripter=FakeScripter(),
    )


def _dossier() -> Dossier:
    return Dossier(
        summary="Snores on the sofa and claims every sunbeam.",
        suggested_animal="House cat",
        animal_reason="Professional procrastinator with a sunbeam habit.",
    )


def test_full_pass_delivers_the_hybrid_portrait_exactly_once() -> None:
    dossier = _dossier()
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True, reason="a face")])
    llm = FakeLLM(
        questions=["Q1", "Q2", "Q3", "Q4", "Q5"],
        dossiers=[dossier],
    )
    converter = FakeConverter()
    transport = _transport(
        [
            [photo_update(1, chat_id=1)],
            [text_update(2, chat_id=1, text="a1")],
            [text_update(3, chat_id=1, text="a2")],
            [text_update(4, chat_id=1, text="a3")],
            [text_update(5, chat_id=1, text="a4")],
            [text_update(6, chat_id=1, text="a5")],
            [text_update(7, chat_id=1, text="hello again")],
        ]
    )

    offset = 0
    for _ in range(7):
        offset = _poll(transport, store, gate, llm, converter, offset=offset)

    state = store.get(1)
    assert state.phase is Phase.done
    assert state.portrait_sent is True
    assert state.dossier == dossier
    assert state.interview == [
        QaPair(question=f"Q{number}", answer=f"a{number}")
        for number in range(1, 6)
    ]

    # Exactly one hybrid portrait, fused from the retained photo and dossier,
    # captioned with the model-suggested animal (never any dossier text).
    assert len(transport.photos) == 1
    portrait = transport.photos[0]
    assert portrait["chat_id"] == 1
    assert portrait["image"] == converter.default.data
    assert "House cat" in portrait["caption"]
    assert converter.calls == [(PORTRAIT_BYTES, dossier, "image/jpeg")]

    # The dossier report and the teaser precede the image; the narration script
    # follows in the same update (Phase 5 handoff), and the follow-up turn gets
    # the Interviewer's light completion reply — never a second photo.
    texts = _texts(transport)
    assert texts[0] == SUCCESS_REPLY
    assert texts[1:6] == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    assert "House cat" in texts[6]
    assert texts[7] == TEASER_REPLY
    assert texts[8] == "A dramatic nature documentary script."
    assert texts[9] == OBSERVATION_COMPLETE_REPLY
    assert state.script == "A dramatic nature documentary script."
    assert state.script_sent is True


def test_failed_handoff_conversion_retries_on_the_next_update() -> None:
    dossier = _dossier()
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True, reason="a face")])
    llm = FakeLLM(
        questions=["Q1", "Q2", "Q3", "Q4", "Q5"],
        dossiers=[dossier],
    )
    # The first image call fails; the fake then returns its default image.
    converter = FakeConverter(results=[ConverterError("model exploded")])
    transport = _transport(
        [
            [photo_update(1, chat_id=2)],
            [text_update(2, chat_id=2, text="a1")],
            [text_update(3, chat_id=2, text="a2")],
            [text_update(4, chat_id=2, text="a3")],
            [text_update(5, chat_id=2, text="a4")],
            [text_update(6, chat_id=2, text="a5")],
            [text_update(7, chat_id=2, text="try again")],
        ]
    )

    offset = 0
    records: list[logging.LogRecord] = []
    handler = _RecordingHandler(records)
    root = logging.getLogger()
    root.addHandler(handler)
    old_level = root.level
    root.setLevel(logging.INFO)
    try:
        for _ in range(6):
            offset = _poll(transport, store, gate, llm, converter, offset=offset)
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)

    # The generation failed loudly: the dossier is stored, but no portrait and
    # no ``portrait_sent`` flag — the conversion stays retryable.
    assert any(
        getattr(record, "event", None) == "conversion_failed"
        for record in records
    )
    state = store.get(2)
    assert state.phase is Phase.done
    assert state.portrait_sent is False
    assert transport.photos == []

    offset = _poll(transport, store, gate, llm, converter, offset=offset)

    state = store.get(2)
    assert state.phase is Phase.done
    assert state.portrait_sent is True
    assert len(transport.photos) == 1
    assert len(converter.calls) == 2
