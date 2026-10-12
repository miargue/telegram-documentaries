"""Component: the Scripter over the real ``poll_once`` loop.

Honest label: no real I/O. The real polling loop, update parsing, Bouncer
routing/handoff, Interviewer turn-taking, Converter routing, Scripter routing
and session driver all run; only Telegram (``FakeTransport``), the vision gate
(``FakeGate``), the interview model (``FakeLLM``), the image model
(``FakeConverter``) and the narration model (``FakeScripter``) are doubles, so
the whole pass → 5-answer → dossier → hybrid-portrait → narration-script
conversation is exercised without a network.

Phase 5 behaviour proven here: the narration arrives in the very same update as
the portrait — no user input in between — it is captured in the session for
Phase 6, it is delivered exactly once, and a degraded generation apologises in
band and becomes retryable on the next update.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

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

from telegram_documentaries.bouncer import PROMPT_PHOTO, SUCCESS_REPLY
from telegram_documentaries.converter import TEASER_REPLY
from telegram_documentaries.interviewer import OBSERVATION_COMPLETE_REPLY
from telegram_documentaries.polling import poll_once
from telegram_documentaries.scripter import SCRIPT_APOLOGY_REPLY, ScriptError
from telegram_documentaries.session import Dossier, Phase, QaPair, SessionStore
from telegram_documentaries.transport import TelegramApiError
from telegram_documentaries.vision import HumanVerdict

PORTRAIT_BYTES = b"PORTRAIT-BYTES"


class _FlakyNarrationTransport(FakeTransport):
    """A transport whose narration ``sendMessage`` fails exactly once.

    The first attempt to deliver the narration text raises (a transient
    Telegram failure); every later call — including the retry — succeeds. Used
    to prove a failed *send* re-sends the stored script without re-generating.
    """

    def __init__(self, *, narration: str, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._narration = narration
        self._failed = False

    def call(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        if (
            method == "sendMessage"
            and not self._failed
            and (params or {}).get("text") == self._narration
        ):
            self._failed = True
            # Record the attempt before failing, like FakeTransport.fail_on.
            self.calls.append((method, dict(params or {})))
            raise TelegramApiError("sendMessage", "transient")
        return super().call(method, params)


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
    scripter: FakeScripter,
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
        scripter=scripter,
    )


def _dossier() -> Dossier:
    return Dossier(
        summary="Snores on the sofa and claims every sunbeam.",
        suggested_animal="House cat",
        animal_reason="Professional procrastinator with a sunbeam habit.",
    )


def test_full_pass_delivers_the_narration_right_after_the_portrait() -> None:
    dossier = _dossier()
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True, reason="a face")])
    llm = FakeLLM(
        questions=["Q1", "Q2", "Q3", "Q4", "Q5"],
        dossiers=[dossier],
    )
    converter = FakeConverter()
    scripter = FakeScripter()
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
        offset = _poll(
            transport, store, gate, llm, converter, scripter, offset=offset
        )

    state = store.get(1)
    assert state.phase is Phase.done
    assert state.interview == [
        QaPair(question=f"Q{number}", answer=f"a{number}")
        for number in range(1, 6)
    ]

    # The exact send sequence of the one pass: success line, five questions,
    # dossier report, teaser, then the hybrid portrait upload, and finally the
    # narration script — all before the follow-up turn is even polled.
    texts = _texts(transport)
    assert texts[0] == SUCCESS_REPLY
    assert texts[1:6] == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    assert "House cat" in texts[6]
    assert texts[7] == TEASER_REPLY
    assert len(transport.photos) == 1
    assert texts[8] == "A dramatic nature documentary script."
    assert texts[9] == OBSERVATION_COMPLETE_REPLY

    # The raw narration is captured in the session, ready for Phase 6 audio
    # synthesis, and the delivery flag is set — exactly one script generated.
    assert state.script == "A dramatic nature documentary script."
    assert state.script_sent is True
    assert len(scripter.calls) == 1
    assert scripter.calls[0] == dossier


def test_degraded_generation_apologises_and_retries_on_the_next_update() -> None:
    dossier = _dossier()
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True, reason="a face")])
    llm = FakeLLM(
        questions=["Q1", "Q2", "Q3", "Q4", "Q5"],
        dossiers=[dossier],
    )
    # The first narration call fails; the fake then returns its default script.
    scripter = FakeScripter(scripts=[ScriptError("model exploded")])
    converter = FakeConverter()
    transport = _transport(
        [
            [photo_update(1, chat_id=2)],
            [text_update(2, chat_id=2, text="a1")],
            [text_update(3, chat_id=2, text="a2")],
            [text_update(4, chat_id=2, text="a3")],
            [text_update(5, chat_id=2, text="a4")],
            [text_update(6, chat_id=2, text="a5")],
            [text_update(7, chat_id=2, text="again")],
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
            offset = _poll(
                transport, store, gate, llm, converter, scripter, offset=offset
            )
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)

    # The generation failed loudly in the logs, the apology arrived in-band,
    # and nothing was recorded as delivered — the scripting stays retryable.
    assert any(
        getattr(record, "event", None) == "script_failed"
        for record in records
    )
    state = store.get(2)
    assert state.phase is Phase.done
    assert state.portrait_sent is True
    assert state.script is None
    assert state.script_sent is False
    texts = _texts(transport)
    assert texts[-1] == SCRIPT_APOLOGY_REPLY
    assert "A dramatic nature documentary script." not in texts

    # The next update completes the narration: generated, delivered, captured.
    offset = _poll(
        transport, store, gate, llm, converter, scripter, offset=offset
    )

    state = store.get(2)
    assert state.script == "A dramatic nature documentary script."
    assert state.script_sent is True
    assert len(scripter.calls) == 2
    assert _texts(transport)[-1] == "A dramatic nature documentary script."


def test_failed_send_reuses_the_stored_script_without_regenerating() -> None:
    # The narration send fails once right after the portrait handoff. The
    # script was stored before the send, so the next update re-sends that exact
    # text through the real poll loop — and the Scripter is never called again.
    dossier = _dossier()
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True, reason="a face")])
    llm = FakeLLM(
        questions=["Q1", "Q2", "Q3", "Q4", "Q5"],
        dossiers=[dossier],
    )
    converter = FakeConverter()
    scripter = FakeScripter()
    transport = _FlakyNarrationTransport(
        narration="A dramatic nature documentary script.",
        batches=[
            [photo_update(1, chat_id=3)],
            [text_update(2, chat_id=3, text="a1")],
            [text_update(3, chat_id=3, text="a2")],
            [text_update(4, chat_id=3, text="a3")],
            [text_update(5, chat_id=3, text="a4")],
            [text_update(6, chat_id=3, text="a5")],
            [text_update(7, chat_id=3, text="again")],
        ],
        files={"abc123": ("photos/a.jpg", PORTRAIT_BYTES)},
    )

    offset = 0
    for _ in range(6):
        offset = _poll(
            transport, store, gate, llm, converter, scripter, offset=offset
        )

    # The generation succeeded and the script was stored, but the delivery
    # failed: apologised in-band, no flag, the transcript is ready to re-send.
    state = store.get(3)
    assert state.script == "A dramatic nature documentary script."
    assert state.script_sent is False
    assert SCRIPT_APOLOGY_REPLY in _texts(transport)

    offset = _poll(
        transport, store, gate, llm, converter, scripter, offset=offset
    )

    # The retry re-sent the stored script verbatim and the scripter was never
    # called a second time (no re-generation, no drift).
    assert store.get(3).script_sent is True
    assert _texts(transport)[-1] == "A dramatic nature documentary script."
    assert len(scripter.calls) == 1


def test_start_after_the_narration_resets_the_script_state() -> None:
    # A ``/start`` arriving after the whole pipeline has run clears the raw
    # script and the delivered flag along with every other artifact, so the
    # next portrait starts a brand new documentary rather than replaying the
    # old narration.
    dossier = _dossier()
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True, reason="a face")])
    llm = FakeLLM(
        questions=["Q1", "Q2", "Q3", "Q4", "Q5"],
        dossiers=[dossier],
    )
    converter = FakeConverter()
    scripter = FakeScripter()
    transport = _transport(
        [
            [photo_update(1, chat_id=4)],
            [text_update(2, chat_id=4, text="a1")],
            [text_update(3, chat_id=4, text="a2")],
            [text_update(4, chat_id=4, text="a3")],
            [text_update(5, chat_id=4, text="a4")],
            [text_update(6, chat_id=4, text="a5")],
            [text_update(7, chat_id=4, text="/start")],
        ]
    )

    offset = 0
    for _ in range(7):
        offset = _poll(
            transport, store, gate, llm, converter, scripter, offset=offset
        )

    state = store.get(4)
    assert state.phase is Phase.awaiting_photo
    assert state.script is None
    assert state.script_sent is False
    assert state.portrait_sent is False
    assert _texts(transport)[-1] == PROMPT_PHOTO
