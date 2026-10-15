"""Unit tests for :class:`AdkPipeline` — the live ADK driver (Phase 8).

No test contacts Telegram or Gemini. The pipeline is built from the conftest
fakes and driven through the real ``Runner`` / ``InMemorySessionService`` so the
session-per-``chat_id`` contract and the ``bool | Skipped`` return type are
verified end to end.
"""

from __future__ import annotations

import logging
from typing import Any

from conftest import (
    FakeConverter,
    FakeGate,
    FakeLLM,
    FakeNarrator,
    FakeScripter,
    FakeTransport,
    messageless_update,
    photo_update,
    text_update,
)
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from telegram_documentaries import bouncer
from telegram_documentaries.adk_orchestration import AdkPipeline
from telegram_documentaries.bouncer import PROMPT_PHOTO
from telegram_documentaries.logging_config import SKIPPED
from telegram_documentaries.models import Update
from telegram_documentaries.session import Dossier, Phase, SessionStore
from telegram_documentaries.vision import HumanVerdict

_LOG = logging.getLogger("test.adk.pipeline")


class _CountingSessionService(InMemorySessionService):
    """``InMemorySessionService`` that records each session creation."""

    def __init__(self) -> None:
        super().__init__()
        self.created: list[str] = []

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Any:
        self.created.append(session_id or "")
        return await super().create_session(
            app_name=app_name, user_id=user_id, state=state, session_id=session_id
        )


def _pipeline(
    *,
    session: SessionStore,
    transport: FakeTransport,
    gate: FakeGate | None = None,
    llm: FakeLLM | None = None,
    converter: FakeConverter | None = None,
    scripter: FakeScripter | None = None,
    narrator: FakeNarrator | None = None,
    session_service: InMemorySessionService | None = None,
) -> AdkPipeline:
    return AdkPipeline(
        session=session,
        transport=transport,
        gate=FakeGate() if gate is None else gate,
        llm=FakeLLM() if llm is None else llm,
        converter=FakeConverter() if converter is None else converter,
        scripter=FakeScripter() if scripter is None else scripter,
        narrator=narrator,
        log=_LOG,
        is_start_command=bouncer._is_start_command,
        is_album_follow_up=bouncer._is_album_follow_up,
        reset_chat=bouncer.reset_chat,
        prompt=bouncer._prompt,
        handle_photo=bouncer._handle_photo,
        session_service=session_service,
    )


def _update(payload: dict[str, Any]) -> Update:
    return Update.model_validate(payload)


def test_pipeline_builds_one_runner_and_spawns_sessions_per_chat() -> None:
    service = _CountingSessionService()
    session = SessionStore()
    pipeline = _pipeline(
        session=session,
        transport=FakeTransport(),
        session_service=service,
    )

    assert isinstance(pipeline._runner, Runner)
    runner_before = pipeline._runner

    pipeline.handle(_update(text_update(1, chat_id=1)), chat_id=1)
    pipeline.handle(_update(text_update(2, chat_id=1)), chat_id=1)
    pipeline.handle(_update(text_update(3, chat_id=2)), chat_id=2)

    # The pipeline is built once; the session is created once per chat_id.
    assert pipeline._runner is runner_before
    assert sorted(service.created) == ["1", "2"]


def test_pipeline_photo_gate_passes_and_hands_off() -> None:
    session = SessionStore()
    transport = FakeTransport(
        files={"abc123": ("photos/a.jpg", b"PORTRAIT-BYTES")}
    )
    pipeline = _pipeline(
        session=session,
        transport=transport,
        gate=FakeGate([HumanVerdict(is_human=True)]),
        llm=FakeLLM(),
    )

    result = pipeline.handle(_update(photo_update(1, chat_id=1)), chat_id=1)

    assert result is True
    assert session.get(1).phase is Phase.interviewing
    assert session.get(1).photo == b"PORTRAIT-BYTES"


def test_pipeline_start_command_resets_mid_interview() -> None:
    session = SessionStore()
    session.record_pass(1, b"PORTRAIT")
    transport = FakeTransport()
    pipeline = _pipeline(session=session, transport=transport)

    result = pipeline.handle(
        _update(text_update(1, chat_id=1, text="/restart")), chat_id=1
    )

    assert result is True
    assert session.get(1).phase is Phase.awaiting_photo
    assert session.get(1).photo is None
    assert transport.calls_for("sendMessage")[0]["text"] == PROMPT_PHOTO


def test_pipeline_messageless_update_returns_the_skipped_singleton() -> None:
    pipeline = _pipeline(session=SessionStore(), transport=FakeTransport())

    result = pipeline.handle(_update(messageless_update(1)), chat_id=0)

    assert result is SKIPPED


def test_pipeline_send_failure_returns_false() -> None:
    session = SessionStore()
    transport = FakeTransport(fail_on={"sendMessage": RuntimeError("blocked")})
    pipeline = _pipeline(session=session, transport=transport)

    result = pipeline.handle(
        _update(text_update(1, chat_id=1, text="hello")), chat_id=1
    )

    assert result is False


def test_pipeline_full_chain_completion_delivers_every_artifact() -> None:
    session = SessionStore()
    transport = FakeTransport(
        files={"abc123": ("photos/a.jpg", b"PORTRAIT-BYTES")}
    )
    pipeline = _pipeline(
        session=session,
        transport=transport,
        gate=FakeGate([HumanVerdict(is_human=True)]),
        llm=FakeLLM(dossiers=[Dossier(summary="S", suggested_animal="Capybara")]),
        converter=FakeConverter(),
        scripter=FakeScripter(),
        narrator=FakeNarrator(),
    )

    assert pipeline.handle(_update(photo_update(1, chat_id=1)), chat_id=1) is True
    for index in range(5):
        result = pipeline.handle(
            _update(text_update(2 + index, chat_id=1, text=f"a{index + 1}")),
            chat_id=1,
        )
        assert result is True

    state = session.get(1)
    assert state.phase is Phase.done
    assert state.portrait_sent is True
    assert state.script_sent is True
    assert state.narrator_sent is True
    assert len(transport.photos) == 1
    assert len(transport.audios) == 1


def test_pipeline_returns_false_when_the_overall_result_is_missing() -> None:
    # Defensive: a session missing the ``result`` key decodes to the default
    # ``False`` rather than raising.
    from telegram_documentaries.adk_orchestration import _decode_result

    assert _decode_result(None) is False
