"""Component: the whole interview conversation over the real ``poll_once`` loop.

Honest label: no real I/O. The real polling loop, update parsing, Bouncer
routing/handoff, Interviewer turn-taking and session driver all run; only
Telegram (``FakeTransport``), Gemini (``FakeGate``) and the interview model
(``FakeLLM``) are doubles, so the whole pass → 5-answer → dossier conversation
is exercised without a network.
"""

from __future__ import annotations

from conftest import FakeGate, FakeLLM, FakeTransport, photo_update, text_update

from telegram_documentaries.bouncer import PROMPT_PHOTO, SUCCESS_REPLY
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
    *,
    offset: int,
) -> int:
    return poll_once(
        transport, offset=offset, session=store, gate=gate, llm=llm
    )


def test_full_interview_delivers_and_stores_the_dossier() -> None:
    dossier = Dossier(
        summary="Snores on the sofa and claims every sunbeam.",
        suggested_animal="House cat",
        animal_reason="Professional procrastinator with a sunbeam habit.",
    )
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True, reason="a face")])
    llm = FakeLLM(
        questions=["Q1", "Q2", "Q3", "Q4", "Q5"],
        dossiers=[dossier],
    )
    transport = _transport(
        [
            [photo_update(1, chat_id=1)],
            [text_update(2, chat_id=1, text="a1")],
            [text_update(3, chat_id=1, text="a2")],
            [text_update(4, chat_id=1, text="a3")],
            [text_update(5, chat_id=1, text="a4")],
            [text_update(6, chat_id=1, text="a5")],
        ]
    )

    offset = 0
    sent_per_turn: list[int] = []
    for _ in range(6):
        before = len(transport.calls_for("sendMessage"))
        offset = _poll(transport, store, gate, llm, offset=offset)
        sent_per_turn.append(len(transport.calls_for("sendMessage")) - before)

    replies = _texts(transport)
    # The success line is followed by exactly one question; every later answer
    # turn produces exactly one message (one question, or the final report).
    assert replies[0] == SUCCESS_REPLY
    assert sent_per_turn == [2, 1, 1, 1, 1, 1]
    assert replies[1:6] == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    assert "House cat" in replies[6]
    assert "Snores on the sofa" in replies[6]

    state = store.get(1)
    assert state.phase is Phase.done
    assert state.dossier == dossier
    assert state.interview == [
        QaPair(question=f"Q{number}", answer=f"a{number}")
        for number in range(1, 6)
    ]
    assert llm.summarize_calls == [
        [
            QaPair(question=f"Q{number}", answer=f"a{number}")
            for number in range(1, 6)
        ]
    ]


def test_photo_mid_interview_reasks_the_current_question_without_advancing() -> None:
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True, reason="a face")])
    llm = FakeLLM(questions=["Q1", "Q2", "Q3", "Q4", "Q5"])
    transport = _transport(
        [
            [photo_update(1, chat_id=2)],  # pass -> Q1
            [text_update(2, chat_id=2, text="a1")],  # -> Q2
            [photo_update(3, chat_id=2)],  # mid-interview -> re-ask Q2
        ]
    )

    offset = 0
    for _ in range(3):
        offset = _poll(transport, store, gate, llm, offset=offset)

    assert _texts(transport) == [SUCCESS_REPLY, "Q1", "Q2", "Q2"]
    state = store.get(2)
    assert state.phase is Phase.interviewing
    assert state.interview == [QaPair(question="Q1", answer="a1")]


def test_start_mid_interview_resets_photo_and_interview_log() -> None:
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True, reason="a face")])
    llm = FakeLLM(questions=["Q1", "Q2", "Q3", "Q4", "Q5"])
    transport = _transport(
        [
            [photo_update(1, chat_id=3)],  # pass -> interviewing
            [text_update(2, chat_id=3, text="a1")],
            [text_update(3, chat_id=3, text="/start")],
        ]
    )

    offset = 0
    for _ in range(3):
        offset = _poll(transport, store, gate, llm, offset=offset)

    assert _texts(transport)[-1] == PROMPT_PHOTO
    state = store.get(3)
    assert state.phase is Phase.awaiting_photo
    assert state.photo is None
    assert state.interview == []
    assert state.dossier is None


def test_text_after_completion_gets_a_light_reply_without_state_change() -> None:
    dossier = Dossier(summary="S", suggested_animal="Capybara")
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True, reason="a face")])
    llm = FakeLLM(
        questions=["Q1", "Q2", "Q3", "Q4", "Q5"],
        dossiers=[dossier],
    )
    transport = _transport(
        [
            [photo_update(1, chat_id=4)],
            [text_update(2, chat_id=4, text="a1")],
            [text_update(3, chat_id=4, text="a2")],
            [text_update(4, chat_id=4, text="a3")],
            [text_update(5, chat_id=4, text="a4")],
            [text_update(6, chat_id=4, text="a5")],
            [text_update(7, chat_id=4, text="hello again")],
        ]
    )

    offset = 0
    for _ in range(7):
        offset = _poll(transport, store, gate, llm, offset=offset)

    assert _texts(transport)[-1] == OBSERVATION_COMPLETE_REPLY
    state = store.get(4)
    assert state.phase is Phase.done
    assert state.dossier == dossier
    assert len(state.interview) == 5
