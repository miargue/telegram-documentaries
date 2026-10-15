"""Component: the Narrator over the real ``poll_once`` loop.

Honest label: no real I/O. The real polling loop, update parsing, Bouncer
routing/handoff, Interviewer turn-taking, Converter routing, Scripter routing,
Narrator routing and the session driver all run; only Telegram
(``FakeTransport``), the vision gate (``FakeGate``), the interview model
(``FakeLLM``), the image model (``FakeConverter``), the narration-text model
(``FakeScripter``) and the TTS model (``FakeNarrator``) are doubles, so the
whole pass → 5-answer → dossier → hybrid-portrait → narration-script →
audio-message conversation is exercised without a network.

Phase 6 behaviour proven here: the audio message arrives in the very same update
as the script — no user input in between — the raw audio is captured in the
session, it is delivered exactly once, a degraded synthesis apologises in band
and becomes retryable, and a failed *send* re-sends the stored audio without ever
re-synthesising.
"""

from __future__ import annotations

from typing import Any

from conftest import (
    FakeConverter,
    FakeGate,
    FakeLLM,
    FakeNarrator,
    FakeScripter,
    FakeTransport,
    photo_update,
    text_update,
)

from telegram_documentaries.bouncer import PROMPT_PHOTO, SUCCESS_REPLY
from telegram_documentaries.converter import TEASER_REPLY
from telegram_documentaries.interviewer import OBSERVATION_COMPLETE_REPLY
from telegram_documentaries.narrator import NARRATION_APOLOGY_REPLY, NarratorError
from telegram_documentaries.polling import poll_once
from telegram_documentaries.session import Dossier, Phase, QaPair, SessionStore
from telegram_documentaries.transport import TelegramApiError
from telegram_documentaries.vision import HumanVerdict

PORTRAIT_BYTES = b"PORTRAIT-BYTES"
SCRIPT = "A dramatic nature documentary script."


class _FlakyAudioTransport(FakeTransport):
    """A transport whose ``sendAudio`` fails exactly once.

    The first audio upload raises (a transient Telegram failure); every later
    call — including the retry — succeeds. Used to prove a failed *send*
    re-sends the stored audio without re-synthesising.
    """

    def __init__(self, *, fail_first_audio: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._fail_first_audio = fail_first_audio
        self._audio_failed = False

    def send_audio(
        self,
        chat_id: int,
        audio: bytes,
        *,
        media_type: str,
        filename: str = "narration.mp3",
    ) -> dict[str, Any]:
        self.audios.append(
            {
                "chat_id": chat_id,
                "audio": audio,
                "media_type": media_type,
                "filename": filename,
            }
        )
        if self._fail_first_audio and not self._audio_failed:
            self._audio_failed = True
            raise TelegramApiError("sendAudio", "transient")
        return {"ok": True, "result": {"message_id": 1, "chat": {"id": chat_id}}}


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
    narrator: FakeNarrator,
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
        narrator=narrator,
    )


def _dossier() -> Dossier:
    return Dossier(
        summary="Snores on the sofa and claims every sunbeam.",
        suggested_animal="House cat",
        animal_reason="Professional procrastinator with a sunbeam habit.",
    )


def test_full_pass_delivers_the_audio_message_right_after_the_script() -> None:
    dossier = _dossier()
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True, reason="a face")])
    llm = FakeLLM(
        questions=["Q1", "Q2", "Q3", "Q4", "Q5"],
        dossiers=[dossier],
    )
    converter = FakeConverter()
    scripter = FakeScripter()
    narrator = FakeNarrator()
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
            transport, store, gate, llm, converter, scripter, narrator, offset=offset
        )

    state = store.get(1)
    assert state.phase is Phase.done
    assert state.interview == [
        QaPair(question=f"Q{number}", answer=f"a{number}")
        for number in range(1, 6)
    ]

    # The narration text is delivered, then the audio message follows in the very
    # same pass — before the follow-up turn is even polled. No transcript change
    # and no extra user input in between.
    texts = _texts(transport)
    assert texts[0] == SUCCESS_REPLY
    assert texts[1:6] == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    assert "House cat" in texts[6]
    assert texts[7] == TEASER_REPLY
    assert len(transport.photos) == 1
    assert texts[8] == SCRIPT
    assert texts[9] == OBSERVATION_COMPLETE_REPLY

    # Exactly one audio message, carrying the synthesised MP3, and the raw audio
    # is captured in the session with the delivery flag set.
    assert len(transport.audios) == 1
    assert transport.audios[0]["audio"] == narrator.default.data
    assert transport.audios[0]["media_type"] == "audio/mpeg"
    assert transport.audios[0]["filename"] == "narration.mp3"
    assert state.narration == narrator.default.data
    assert state.narrator_sent is True
    assert state.script_sent is True
    assert len(scripter.calls) == 1
    assert narrator.calls == [SCRIPT]


def test_degraded_synthesis_apologises_and_retries_on_the_next_update() -> None:
    dossier = _dossier()
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True, reason="a face")])
    llm = FakeLLM(
        questions=["Q1", "Q2", "Q3", "Q4", "Q5"],
        dossiers=[dossier],
    )
    converter = FakeConverter()
    scripter = FakeScripter()
    # The first synthesis attempt fails; the fake then returns its default audio.
    narrator = FakeNarrator([NarratorError("model exploded")])
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
    for _ in range(6):
        offset = _poll(
            transport, store, gate, llm, converter, scripter, narrator, offset=offset
        )

    # The failure apologised in-band and recorded nothing as delivered: the
    # narration stays retryable, and no audio message was uploaded.
    state = store.get(2)
    assert state.script_sent is True
    assert state.narration is None
    assert state.narrator_sent is False
    assert NARRATION_APOLOGY_REPLY in _texts(transport)
    assert transport.audios == []

    # The next update completes it: synthesised, delivered exactly once.
    offset = _poll(
        transport, store, gate, llm, converter, scripter, narrator, offset=offset
    )

    state = store.get(2)
    assert state.narration == narrator.default.data
    assert state.narrator_sent is True
    assert len(transport.audios) == 1
    assert len(narrator.calls) == 2


def test_failed_send_reuses_the_stored_audio_without_resynthesising() -> None:
    # The audio upload fails once right after synthesis. The audio was stored
    # before the send, so the next update re-sends those exact bytes through the
    # real poll loop — and the Narrator is never called again.
    dossier = _dossier()
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True, reason="a face")])
    llm = FakeLLM(
        questions=["Q1", "Q2", "Q3", "Q4", "Q5"],
        dossiers=[dossier],
    )
    converter = FakeConverter()
    scripter = FakeScripter()
    narrator = FakeNarrator()
    transport = _FlakyAudioTransport(
        fail_first_audio=True,
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
            transport, store, gate, llm, converter, scripter, narrator, offset=offset
        )

    # Synthesis succeeded and the audio was stored, but the send failed:
    # apologised in-band, no flag, the audio is ready to re-send.
    state = store.get(3)
    assert state.narration == narrator.default.data
    assert state.narrator_sent is False
    assert NARRATION_APOLOGY_REPLY in _texts(transport)
    assert len(transport.audios) == 1

    offset = _poll(
        transport, store, gate, llm, converter, scripter, narrator, offset=offset
    )

    # The retry re-sent the stored bytes verbatim and the Narrator was never
    # consulted a second time (no re-synthesis, no drift).
    assert store.get(3).narrator_sent is True
    assert len(transport.audios) == 2
    assert transport.audios[1]["audio"] == narrator.default.data
    assert transport.audios[1]["media_type"] == "audio/mpeg"
    assert len(narrator.calls) == 1


def test_start_after_the_audio_message_resets_the_narration_state() -> None:
    # A ``/start`` after the whole pipeline ran clears the raw audio and the
    # delivered flag along with every other artifact, so the next portrait
    # starts a brand new documentary rather than replaying the old audio.
    dossier = _dossier()
    store = SessionStore()
    gate = FakeGate([HumanVerdict(is_human=True, reason="a face")])
    llm = FakeLLM(
        questions=["Q1", "Q2", "Q3", "Q4", "Q5"],
        dossiers=[dossier],
    )
    converter = FakeConverter()
    scripter = FakeScripter()
    narrator = FakeNarrator()
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
            transport, store, gate, llm, converter, scripter, narrator, offset=offset
        )

    state = store.get(4)
    assert state.phase is Phase.awaiting_photo
    assert state.narration is None
    assert state.narrator_sent is False
    assert state.script is None
    assert state.script_sent is False
    assert state.portrait_sent is False
    assert _texts(transport)[-1] == PROMPT_PHOTO
