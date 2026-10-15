"""Unit tests for the Google ADK orchestration layer (Phase 8).

No test contacts Telegram or Gemini: every stage runs against the conftest
fakes (``FakeTransport``, ``FakeGate``, ``FakeLLM``, ``FakeConverter``,
``FakeScripter``, ``FakeNarrator``). The ADK agents are driven through a real
``Runner`` + ``InMemorySessionService`` so the state-passing contract is
exercised, not mocked.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from conftest import (
    FakeConverter,
    FakeGate,
    FakeLLM,
    FakeNarrator,
    FakeScripter,
    FakeTransport,
    document_update,
    messageless_update,
    photo_update,
    text_update,
)
from google.adk.agents import BaseAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from telegram_documentaries import bouncer
from telegram_documentaries.adk_orchestration import (
    _ROUTE_AFTER_GATE,
    BouncerAgent,
    ConverterAgent,
    DocumentaryOrchestrator,
    InterviewerAgent,
    NarratorAgent,
)
from telegram_documentaries.bouncer import PROMPT_PHOTO
from telegram_documentaries.models import Update
from telegram_documentaries.narrator import handle_narrating
from telegram_documentaries.scripter import handle_scripting
from telegram_documentaries.session import Dossier, Phase, SessionStore
from telegram_documentaries.vision import HumanVerdict

_LOG = logging.getLogger("test.adk")


def _update(payload: dict[str, Any]) -> Update:
    return Update.model_validate(payload)


def _state(update: Update, chat_id: int) -> dict[str, Any]:
    return {"chat_id": chat_id, "update": update.model_dump(mode="json")}


def _run(agent: BaseAgent, state: dict[str, Any]) -> dict[str, Any]:
    """Drive ``agent`` once through a real Runner; return the final session state."""

    async def _go() -> dict[str, Any]:
        service = InMemorySessionService()
        await service.create_session(app_name="t", user_id="1", session_id="1")
        runner = Runner(app_name="t", agent=agent, session_service=service)
        async for _event in runner.run_async(
            user_id="1",
            session_id="1",
            new_message=types.Content(
                role="user", parts=[types.Part(text="run")]
            ),
            state_delta=state,
        ):
            pass
        session = await service.get_session(
            app_name="t", user_id="1", session_id="1"
        )
        assert session is not None
        return dict(session.state)

    return asyncio.run(_go())


def _photo_transport() -> FakeTransport:
    return FakeTransport(files={"abc123": ("photos/a.jpg", b"PORTRAIT-BYTES")})


def _bouncer_kwargs() -> dict[str, Any]:
    return {
        "is_start_command": bouncer._is_start_command,
        "is_album_follow_up": bouncer._is_album_follow_up,
        "reset_chat": bouncer.reset_chat,
        "prompt": bouncer._prompt,
        "handle_photo": bouncer._handle_photo,
    }


def test_stage_agents_subclass_base_agent() -> None:
    for agent_cls in (
        BouncerAgent,
        InterviewerAgent,
        ConverterAgent,
        NarratorAgent,
        DocumentaryOrchestrator,
    ):
        assert issubclass(agent_cls, BaseAgent)


def test_bouncer_agent_delegates_photo_to_injected_handler() -> None:
    session = SessionStore()
    seen: list[int] = []

    def spy_handle_photo(
        message: Any,
        transport: Any,
        *,
        chat_id: int,
        update: Any,
        session: SessionStore,
        gate: Any,
        llm: Any,
        log: Any,
    ) -> bool:
        seen.append(chat_id)
        return True

    agent = BouncerAgent(
        session=session,
        transport=FakeTransport(),
        gate=FakeGate(),
        llm=FakeLLM(),
        log=_LOG,
        is_start_command=bouncer._is_start_command,
        is_album_follow_up=bouncer._is_album_follow_up,
        reset_chat=bouncer.reset_chat,
        prompt=bouncer._prompt,
        handle_photo=spy_handle_photo,
    )

    state = _run(agent, _state(_update(photo_update(1, chat_id=7)), 7))

    assert seen == [7]
    assert state["stage_result"] is True


def test_bouncer_agent_skips_messageless_update() -> None:
    agent = BouncerAgent(
        session=SessionStore(),
        transport=FakeTransport(),
        gate=FakeGate(),
        llm=FakeLLM(),
        log=_LOG,
        **_bouncer_kwargs(),
    )

    state = _run(agent, _state(_update(messageless_update(2)), 0))

    assert state["stage_result"] == "skipped"


def test_bouncer_agent_signals_route_when_past_the_gate() -> None:
    session = SessionStore()
    session.record_pass(1, b"PORTRAIT")
    agent = BouncerAgent(
        session=session,
        transport=FakeTransport(),
        gate=FakeGate(),
        llm=FakeLLM(),
        log=_LOG,
        **_bouncer_kwargs(),
    )

    state = _run(agent, _state(_update(text_update(3, chat_id=1)), 1))

    assert state["stage_result"] == _ROUTE_AFTER_GATE


def test_bouncer_agent_gates_a_real_photo_without_network() -> None:
    session = SessionStore()
    transport = _photo_transport()
    gate = FakeGate([HumanVerdict(is_human=True)])
    agent = BouncerAgent(
        session=session,
        transport=transport,
        gate=gate,
        llm=FakeLLM(),
        log=_LOG,
        **_bouncer_kwargs(),
    )

    state = _run(agent, _state(_update(photo_update(4, chat_id=1)), 1))

    assert state["stage_result"] is True
    assert gate.calls == 1
    assert session.get(1).phase is Phase.interviewing


def test_interviewer_agent_delegates_to_injected_callable() -> None:
    session = SessionStore()
    seen: list[Update] = []

    def spy(update: Update, transport: Any, **kwargs: Any) -> bool:
        seen.append(update)
        return True

    agent = InterviewerAgent(
        session=session,
        transport=FakeTransport(),
        llm=FakeLLM(),
        log=_LOG,
        handle_interview_update=spy,
    )
    update = _update(text_update(5, chat_id=3, text="answer"))

    state = _run(agent, _state(update, 3))

    assert len(seen) == 1
    assert seen[0].update_id == 5
    assert state["stage_result"] is True


def test_converter_agent_delegates_to_injected_callable() -> None:
    session = SessionStore()
    seen: list[int] = []

    def spy(transport: Any, chat_id: int, **kwargs: Any) -> bool:
        seen.append(chat_id)
        return False

    agent = ConverterAgent(
        session=session,
        transport=FakeTransport(),
        converter=FakeConverter(),
        log=_LOG,
        handle_conversion=spy,
    )

    state = _run(agent, _state(_update(text_update(6, chat_id=9)), 9))

    assert seen == [9]
    assert state["stage_result"] is False


def test_narrator_agent_folds_scripting_then_narration() -> None:
    session = SessionStore()
    session.record_pass(2, b"PORTRAIT")
    session.start_interview(2, "Q1")
    session.record_interview_answer(2, "Q1", "A1")
    session.appoint_dossier(2, Dossier(summary="S", suggested_animal="Capybara"))
    session.mark_portrait_sent(2)

    scripter = FakeScripter(default="The script.")
    narrator = FakeNarrator()
    agent = NarratorAgent(
        session=session,
        transport=FakeTransport(),
        scripter=scripter,
        narrator=narrator,
        log=_LOG,
        handle_scripting=handle_scripting,
        handle_narrating=handle_narrating,
    )

    state = _run(agent, _state(_update(text_update(7, chat_id=2)), 2))

    assert state["stage_result"] is True
    assert scripter.calls  # scripting ran
    assert narrator.calls == ["The script."]  # narration ran on the stored script
    assert session.get(2).script_sent is True
    assert session.get(2).narrator_sent is True


def test_narrator_agent_stops_when_scripting_fails() -> None:
    session = SessionStore()
    session.record_pass(2, b"PORTRAIT")
    session.start_interview(2, "Q1")
    session.record_interview_answer(2, "Q1", "A1")
    session.appoint_dossier(2, Dossier(summary="S", suggested_animal="Capybara"))
    session.mark_portrait_sent(2)

    scripter = FakeScripter(scripts=[RuntimeError("boom")])
    narrator = FakeNarrator()
    agent = NarratorAgent(
        session=session,
        transport=FakeTransport(),
        scripter=scripter,
        narrator=narrator,
        log=_LOG,
        handle_scripting=handle_scripting,
        handle_narrating=handle_narrating,
    )

    state = _run(agent, _state(_update(text_update(8, chat_id=2)), 2))

    assert state["stage_result"] is False
    assert narrator.calls == []  # narration never reached


def test_orchestrator_is_a_base_agent_with_four_sub_agents() -> None:
    session = SessionStore()
    bouncer_agent = BouncerAgent(
        session=session,
        transport=FakeTransport(),
        gate=FakeGate(),
        llm=FakeLLM(),
        log=_LOG,
        **_bouncer_kwargs(),
    )
    orchestrator = DocumentaryOrchestrator(
        session=session,
        narrator=None,
        bouncer=bouncer_agent,
        interviewer=InterviewerAgent(
            session=session,
            transport=FakeTransport(),
            llm=FakeLLM(),
            log=_LOG,
            handle_interview_update=lambda *a, **k: True,
        ),
        converter=ConverterAgent(
            session=session,
            transport=FakeTransport(),
            converter=FakeConverter(),
            log=_LOG,
            handle_conversion=lambda *a, **k: True,
        ),
        narrator_agent=NarratorAgent(
            session=session,
            transport=FakeTransport(),
            scripter=FakeScripter(),
            narrator=None,
            log=_LOG,
            handle_scripting=lambda *a, **k: True,
            handle_narrating=lambda *a, **k: True,
        ),
    )

    assert isinstance(orchestrator, BaseAgent)
    assert [agent.name for agent in orchestrator.sub_agents] == [
        "bouncer",
        "interviewer",
        "converter",
        "narrator",
    ]


def test_orchestrator_writes_result_to_state_for_awaiting_photo_prompt() -> None:
    session = SessionStore()
    transport = FakeTransport()
    bouncer_agent = BouncerAgent(
        session=session,
        transport=transport,
        gate=FakeGate(),
        llm=FakeLLM(),
        log=_LOG,
        **_bouncer_kwargs(),
    )
    orchestrator = DocumentaryOrchestrator(
        session=session,
        narrator=None,
        bouncer=bouncer_agent,
        interviewer=InterviewerAgent(
            session=session,
            transport=transport,
            llm=FakeLLM(),
            log=_LOG,
            handle_interview_update=lambda *a, **k: True,
        ),
        converter=ConverterAgent(
            session=session,
            transport=transport,
            converter=FakeConverter(),
            log=_LOG,
            handle_conversion=lambda *a, **k: True,
        ),
        narrator_agent=NarratorAgent(
            session=session,
            transport=transport,
            scripter=FakeScripter(),
            narrator=None,
            log=_LOG,
            handle_scripting=lambda *a, **k: True,
            handle_narrating=lambda *a, **k: True,
        ),
    )

    state = _run(
        orchestrator, _state(_update(text_update(9, chat_id=4)), 4)
    )

    assert state["result"] is True
    assert transport.calls_for("sendMessage")[0]["text"] == PROMPT_PHOTO


def test_orchestrator_skips_a_non_photo_document() -> None:
    session = SessionStore()
    transport = FakeTransport()
    orchestrator = DocumentaryOrchestrator(
        session=session,
        narrator=None,
        bouncer=BouncerAgent(
            session=session,
            transport=transport,
            gate=FakeGate(),
            llm=FakeLLM(),
            log=_LOG,
            **_bouncer_kwargs(),
        ),
        interviewer=InterviewerAgent(
            session=session,
            transport=transport,
            llm=FakeLLM(),
            log=_LOG,
            handle_interview_update=lambda *a, **k: True,
        ),
        converter=ConverterAgent(
            session=session,
            transport=transport,
            converter=FakeConverter(),
            log=_LOG,
            handle_conversion=lambda *a, **k: True,
        ),
        narrator_agent=NarratorAgent(
            session=session,
            transport=transport,
            scripter=FakeScripter(),
            narrator=None,
            log=_LOG,
            handle_scripting=lambda *a, **k: True,
            handle_narrating=lambda *a, **k: True,
        ),
    )

    state = _run(
        orchestrator, _state(_update(document_update(10, chat_id=4)), 4)
    )

    assert state["result"] is True  # re-prompted for a portrait
    assert transport.calls_for("sendMessage")[0]["text"] == PROMPT_PHOTO
