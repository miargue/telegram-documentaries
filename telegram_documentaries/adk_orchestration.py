"""Google ADK orchestration layer for the live Telegram pipeline (Phase 8).

This module is a **thin, genuine Google ADK layer** that drives the live
gateway. Every stage of the pipeline is expressed as a real
:class:`google.adk.agents.BaseAgent` whose ``_run_async_impl`` delegates to the
*existing*, already-tested stage functions and typed ports — no Gemini REST or
transport logic is re-implemented here. The heavy lifting (validation,
redaction, timeouts, session state) stays exactly where it was:

* :class:`BouncerAgent` wraps the Bouncer's photo-intake and pre-routing
  decision (``_handle_photo`` plus the pure command/album helpers).
* :class:`InterviewerAgent` wraps the Interviewer's turn-taking
  (``handle_update``).
* :class:`ConverterAgent` wraps the hybrid-portrait conversion
  (``handle_conversion``).
* :class:`NarratorAgent` folds the Scripter and Narrator stages together
  (``handle_scripting`` then ``handle_narrating`` as needed).
* :class:`DocumentaryOrchestrator` is the root agent: a live **router** that
  mirrors ``bouncer.handle_update`` and ``bouncer._route_after_gate``, delegating
  each stage to its sub-agent.

State contract
--------------
:class:`SessionStore` remains the single source of truth for all conversation
state. The ADK session state carries only the *ephemeral inputs* needed to run
one update — ``chat_id``, the JSON-safe dumped ``update``, the per-stage
``stage_result`` and the overall ``result`` — never the canonical conversation.
Injected dependencies
(ports, the store, the transport, the logger) are process-side objects passed to
the agents at construction time and are never serialised.

Async model
-----------
The gateway is synchronous and single-threaded, so :meth:`AdkPipeline.handle`
runs the async ADK path with :func:`asyncio.run` once per update. A single ADK
session is created and reused per ``chat_id``. A deliberate skip is encoded as
``"skipped"`` because the ADK state is copied on the way out of the runner and
the :data:`~telegram_documentaries.logging_config.SKIPPED` sentinel identity is
not preserved; the encoding is decoded back to the singleton before returning.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from typing import Any

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import PrivateAttr

from telegram_documentaries.converter import ConverterLLM, handle_conversion
from telegram_documentaries.interviewer import InterviewLLM
from telegram_documentaries.interviewer import handle_update as handle_interview_update
from telegram_documentaries.logging_config import SKIPPED, Skipped
from telegram_documentaries.models import Message, Update
from telegram_documentaries.narrator import NarratorLLM, handle_narrating
from telegram_documentaries.scripter import ScripterLLM, handle_scripting
from telegram_documentaries.session import Phase, SessionStore
from telegram_documentaries.transport import Transport
from telegram_documentaries.vision import VisionGate

APP_NAME = "telegram_documentaries"

# The ADK state key every agent writes its encoded stage outcome to, and the
# key the orchestrator writes the overall outcome to.
_RESULT_KEY = "stage_result"
_OVERALL_RESULT_KEY = "result"

# JSON-safe encodings used inside ADK state. ``Skipped`` is a sentinel whose
# identity is lost when the session is copied out of the runner, so a skip is
# encoded as a string and decoded back to the shared singleton on the way out.
_SKIPPED = "skipped"
# BouncerAgent's signal that the chat is already past the gate and the root
# orchestrator must run the post-gate routing (interview/conversion/narration).
_ROUTE_AFTER_GATE = "route_after_gate"


def _encode_result(result: bool | Skipped) -> bool | str:
    """Encode a stage outcome as a JSON-safe value for ADK state."""
    return _SKIPPED if result is SKIPPED else bool(result)


def _decode_result(value: Any) -> bool | Skipped:
    """Decode an ADK-state stage outcome back to ``bool | Skipped``."""
    if value == _SKIPPED:
        return SKIPPED
    return bool(value)


def _result_event(ctx: InvocationContext, author: str, value: Any) -> Event:
    """Build a state-only ADK event carrying an encoded stage outcome."""
    return Event(
        invocation_id=ctx.invocation_id,
        author=author,
        actions=EventActions(state_delta={_RESULT_KEY: value}),
    )


def _parse_state(ctx: InvocationContext) -> tuple[int, Update] | None:
    """Reconstruct ``(chat_id, update)`` from the ADK session state.

    Returns ``None`` when the ephemeral inputs are missing, which the agents
    treat as a benign skip (the runner is always driven with both set).
    """
    state = ctx.session.state
    raw_update = state.get("update")
    chat_id = state.get("chat_id")
    if raw_update is None or chat_id is None:
        return None
    return int(chat_id), Update.model_validate(raw_update)


class _StageAgent(BaseAgent):
    """Base class for the thin stage agents.

    Subclasses hold their dependencies as private attributes and write their
    encoded outcome to the shared ADK state key via :func:`_result_event`.
    """

    _log: logging.Logger = PrivateAttr()

    def __init__(self, *, name: str, description: str, log: logging.Logger) -> None:
        super().__init__(name=name, description=description)
        self._log = log

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:  # pragma: no cover - abstract
        raise NotImplementedError
        yield  # pragma: no cover - marks this as an async generator


class BouncerAgent(_StageAgent):
    """Wraps the Bouncer's photo intake and pre-routing decision.

    Handles messageless updates, ``/start`` / ``/restart`` resets, album
    follow-ups, the ``awaiting_photo`` prompts and the vision gate. When the
    chat is already past the gate it emits the :data:`_ROUTE_AFTER_GATE` marker
    so :class:`DocumentaryOrchestrator` can run the post-gate routing.
    """

    _session: SessionStore = PrivateAttr()
    _transport: Transport = PrivateAttr()
    _gate: VisionGate = PrivateAttr()
    _llm: InterviewLLM = PrivateAttr()
    _is_start_command: Callable[[Message], bool] = PrivateAttr()
    _is_album_follow_up: Callable[[SessionStore, int, Message], bool] = PrivateAttr()
    _reset_chat: Callable[..., Any] = PrivateAttr()
    _prompt: Callable[..., bool] = PrivateAttr()
    _handle_photo: Callable[..., bool] = PrivateAttr()

    def __init__(
        self,
        *,
        name: str = "bouncer",
        description: str = "Gates the portrait photo and routes pre-gate updates.",
        session: SessionStore,
        transport: Transport,
        gate: VisionGate,
        llm: InterviewLLM,
        log: logging.Logger,
        is_start_command: Callable[[Message], bool],
        is_album_follow_up: Callable[[SessionStore, int, Message], bool],
        reset_chat: Callable[..., Any],
        prompt: Callable[..., bool],
        handle_photo: Callable[..., bool],
    ) -> None:
        super().__init__(name=name, description=description, log=log)
        self._session = session
        self._transport = transport
        self._gate = gate
        self._llm = llm
        self._is_start_command = is_start_command
        self._is_album_follow_up = is_album_follow_up
        self._reset_chat = reset_chat
        self._prompt = prompt
        self._handle_photo = handle_photo

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        parsed = _parse_state(ctx)
        if parsed is None:
            yield _result_event(ctx, self.name, _encode_result(SKIPPED))
            return
        chat_id, update = parsed
        log = self._log
        message = update.message
        if message is None:
            log.info(
                "update without message ignored",
                extra={"event": "update_skipped", "update_id": update.update_id},
            )
            yield _result_event(ctx, self.name, _encode_result(SKIPPED))
            return

        chat_id = message.chat.id
        text = (message.text or "").strip()

        if self._is_start_command(message):
            self._reset_chat(
                self._session, chat_id, update_id=update.update_id, log=log
            )
            sent = self._prompt(
                self._transport, chat_id, update, log, reason="command"
            )
            yield _result_event(ctx, self.name, _encode_result(sent))
            return

        if self._is_album_follow_up(self._session, chat_id, message):
            log.info(
                "album follow-up ignored",
                extra={
                    "event": "bouncer_album_skipped",
                    "update_id": update.update_id,
                    "chat_id": chat_id,
                    "media_group_id": message.media_group_id,
                },
            )
            yield _result_event(ctx, self.name, _encode_result(SKIPPED))
            return

        if self._session.get(chat_id).phase is not Phase.awaiting_photo:
            # The portrait has passed (or a degraded handoff left the chat in
            # ``gate_passed``): the Interviewer owns every remaining update.
            # Remember the album id for *any* grouped photo here too.
            if message.media_group_id is not None and message.photo:
                self._session.remember_media_group(
                    chat_id, message.media_group_id
                )
            yield _result_event(ctx, self.name, _ROUTE_AFTER_GATE)
            return

        if text:
            sent = self._prompt(
                self._transport, chat_id, update, log, reason="text"
            )
        elif message.photo:
            sent = self._handle_photo(
                message,
                self._transport,
                chat_id=chat_id,
                update=update,
                session=self._session,
                gate=self._gate,
                llm=self._llm,
                log=log,
            )
        else:
            sent = self._prompt(
                self._transport, chat_id, update, log,
                reason="unsupported_content",
            )
        yield _result_event(ctx, self.name, _encode_result(sent))


class InterviewerAgent(_StageAgent):
    """Wraps the Interviewer's turn-taking stage (``handle_update``)."""

    _session: SessionStore = PrivateAttr()
    _transport: Transport = PrivateAttr()
    _llm: InterviewLLM = PrivateAttr()
    _handle_interview_update: Callable[..., bool | Skipped] = PrivateAttr()

    def __init__(
        self,
        *,
        name: str = "interviewer",
        description: str = "Owns interview turn-taking and the start handoff.",
        session: SessionStore,
        transport: Transport,
        llm: InterviewLLM,
        log: logging.Logger,
        handle_interview_update: Callable[..., bool | Skipped],
    ) -> None:
        super().__init__(name=name, description=description, log=log)
        self._session = session
        self._transport = transport
        self._llm = llm
        self._handle_interview_update = handle_interview_update

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        parsed = _parse_state(ctx)
        if parsed is None:
            yield _result_event(ctx, self.name, _encode_result(SKIPPED))
            return
        _chat_id, update = parsed
        sent = self._handle_interview_update(
            update,
            self._transport,
            session=self._session,
            llm=self._llm,
            logger=self._log,
        )
        yield _result_event(ctx, self.name, _encode_result(sent))


class ConverterAgent(_StageAgent):
    """Wraps the hybrid-portrait conversion stage (``handle_conversion``)."""

    _session: SessionStore = PrivateAttr()
    _transport: Transport = PrivateAttr()
    _converter: ConverterLLM = PrivateAttr()
    _handle_conversion: Callable[..., bool] = PrivateAttr()

    def __init__(
        self,
        *,
        name: str = "converter",
        description: str = "Generates and delivers the hybrid portrait.",
        session: SessionStore,
        transport: Transport,
        converter: ConverterLLM,
        log: logging.Logger,
        handle_conversion: Callable[..., bool],
    ) -> None:
        super().__init__(name=name, description=description, log=log)
        self._session = session
        self._transport = transport
        self._converter = converter
        self._handle_conversion = handle_conversion

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        parsed = _parse_state(ctx)
        if parsed is None:
            yield _result_event(ctx, self.name, _encode_result(False))
            return
        chat_id, _update = parsed
        sent = self._handle_conversion(
            self._transport,
            chat_id,
            session=self._session,
            converter=self._converter,
            logger=self._log,
        )
        yield _result_event(ctx, self.name, _encode_result(sent))


class NarratorAgent(_StageAgent):
    """Folds the Scripter and Narrator stages into one agent.

    Performs scripting (``handle_scripting``) when the script has not been
    delivered yet, then narration (``handle_narrating``) when a narrator is
    injected and the audio message is still outstanding — exactly the chain the
    Bouncer performed inline before Phase 8.
    """

    _session: SessionStore = PrivateAttr()
    _transport: Transport = PrivateAttr()
    _scripter: ScripterLLM = PrivateAttr()
    _narrator: NarratorLLM | None = PrivateAttr()
    _handle_scripting: Callable[..., bool] = PrivateAttr()
    _handle_narrating: Callable[..., bool] = PrivateAttr()

    def __init__(
        self,
        *,
        name: str = "narrator",
        description: str = "Writes the narration script and synthesises its audio.",
        session: SessionStore,
        transport: Transport,
        scripter: ScripterLLM,
        narrator: NarratorLLM | None,
        log: logging.Logger,
        handle_scripting: Callable[..., bool],
        handle_narrating: Callable[..., bool],
    ) -> None:
        super().__init__(name=name, description=description, log=log)
        self._session = session
        self._transport = transport
        self._scripter = scripter
        self._narrator = narrator
        self._handle_scripting = handle_scripting
        self._handle_narrating = handle_narrating

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        parsed = _parse_state(ctx)
        if parsed is None:
            yield _result_event(ctx, self.name, _encode_result(False))
            return
        chat_id, _update = parsed
        state = self._session.get(chat_id)

        if not state.script_sent:
            scripted = self._handle_scripting(
                self._transport,
                chat_id,
                session=self._session,
                scripter=self._scripter,
                logger=self._log,
            )
            if scripted is not True:
                yield _result_event(ctx, self.name, _encode_result(scripted))
                return
            state = self._session.get(chat_id)
            if self._narrator is None:
                yield _result_event(ctx, self.name, _encode_result(scripted))
                return

        if self._narrator is not None and not state.narrator_sent:
            narrated = self._handle_narrating(
                self._transport,
                chat_id,
                session=self._session,
                narrator=self._narrator,
                logger=self._log,
            )
            yield _result_event(ctx, self.name, _encode_result(narrated))
            return

        yield _result_event(ctx, self.name, _encode_result(True))


class DocumentaryOrchestrator(BaseAgent):
    """Root ADK agent: the live router over the four stage agents.

    Its ``_run_async_impl`` mirrors the Bouncer's routing exactly — the
    messageless / command / album / ``awaiting_photo`` decisions are delegated
    to :class:`BouncerAgent`, and the post-gate handoff (interview → conversion
    → scripting → narration) mirrors ``bouncer._route_after_gate``, delegating
    each stage to its sub-agent. The overall ``bool | Skipped`` is written to
    the ADK state under ``result``.
    """

    _session: SessionStore = PrivateAttr()
    _narrator_llm: NarratorLLM | None = PrivateAttr()
    _bouncer: BouncerAgent = PrivateAttr()
    _interviewer: InterviewerAgent = PrivateAttr()
    _converter: ConverterAgent = PrivateAttr()
    _narrator: NarratorAgent = PrivateAttr()

    def __init__(
        self,
        *,
        name: str = "documentary_orchestrator",
        description: str = (
            "Routes each Telegram update through the Bouncer, Interviewer, "
            "Converter and Narrator ADK agents."
        ),
        session: SessionStore,
        narrator: NarratorLLM | None,
        bouncer: BouncerAgent,
        interviewer: InterviewerAgent,
        converter: ConverterAgent,
        narrator_agent: NarratorAgent,
    ) -> None:
        super().__init__(
            name=name,
            description=description,
            sub_agents=[bouncer, interviewer, converter, narrator_agent],
        )
        self._session = session
        self._narrator_llm = narrator
        self._bouncer = bouncer
        self._interviewer = interviewer
        self._converter = converter
        self._narrator = narrator_agent

    async def _call_agent(
        self, agent: BaseAgent, ctx: InvocationContext
    ) -> tuple[list[Event], Any]:
        """Run one sub-agent, returning its events and raw stage outcome.

        The events are collected (in order) so the orchestrator can yield them
        to the runner; the outcome is read from the agent's own event state
        delta, so it does not depend on when the runner applies that delta. The
        raw value is returned undecoded so the ``route_after_gate`` marker is
        distinguishable from a boolean stage result.
        """
        events: list[Event] = []
        raw: Any = None
        async for event in agent.run_async(ctx):
            events.append(event)
            actions = event.actions
            if actions is not None and _RESULT_KEY in actions.state_delta:
                raw = actions.state_delta[_RESULT_KEY]
        return events, raw

    def _narrator_due(self, chat_id: int) -> bool:
        """Whether the Scripter/Narrator chain still has work for this chat.

        Mirrors the ``done``/``portrait_sent`` guard in
        ``bouncer._route_after_gate``: the chain runs when the script is still
        outstanding, or when a narrator is injected and its audio is.
        """
        state = self._session.get(chat_id)
        if state.phase is not Phase.done or not state.portrait_sent:
            return False
        return not state.script_sent or (
            self._narrator_llm is not None and not state.narrator_sent
        )

    async def _route_after_gate(
        self, ctx: InvocationContext, chat_id: int
    ) -> tuple[list[Event], bool | Skipped]:
        """Mirror ``bouncer._route_after_gate`` using the stage agents."""
        session = self._session
        events: list[Event] = []

        state = session.get(chat_id)
        if state.phase is Phase.done and not state.portrait_sent:
            converted_events, raw = await self._call_agent(self._converter, ctx)
            events += converted_events
            converted = _decode_result(raw)
            if converted is not True:
                return events, converted

        if self._narrator_due(chat_id):
            narrate_events, raw = await self._call_agent(self._narrator, ctx)
            events += narrate_events
            return events, _decode_result(raw)

        interview_events, raw = await self._call_agent(self._interviewer, ctx)
        events += interview_events
        sent = _decode_result(raw)
        if sent is not True:
            return events, sent

        after = session.get(chat_id)
        if after.phase is Phase.done and not after.portrait_sent:
            converted_events, raw = await self._call_agent(self._converter, ctx)
            events += converted_events
            converted = _decode_result(raw)
            if converted is not True:
                return events, converted

        if self._narrator_due(chat_id):
            narrate_events, raw = await self._call_agent(self._narrator, ctx)
            events += narrate_events
            return events, _decode_result(raw)
        return events, sent

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        parsed = _parse_state(ctx)
        if parsed is None:
            yield Event(
                invocation_id=ctx.invocation_id,
                author=self.name,
                actions=EventActions(
                    state_delta={_OVERALL_RESULT_KEY: _encode_result(SKIPPED)}
                ),
            )
            return
        chat_id, _update = parsed

        bouncer_events, raw = await self._call_agent(self._bouncer, ctx)
        for event in bouncer_events:
            yield event

        if raw == _ROUTE_AFTER_GATE:
            route_events, result = await self._route_after_gate(ctx, chat_id)
            for event in route_events:
                yield event
        else:
            result = _decode_result(raw)

        yield Event(
            invocation_id=ctx.invocation_id,
            author=self.name,
            actions=EventActions(
                state_delta={_OVERALL_RESULT_KEY: _encode_result(result)}
            ),
        )


class AdkPipeline:
    """The live ADK driver: a ``Runner`` over the orchestrator agent tree.

    Built from the same injected dependencies the Bouncer receives. It owns an
    :class:`~google.adk.sessions.InMemorySessionService` and reuses one session
    per ``chat_id``; :meth:`handle` runs the async ADK path with
    :func:`asyncio.run` and returns the decoded ``bool | Skipped``.
    """

    def __init__(
        self,
        *,
        session: SessionStore,
        transport: Transport,
        gate: VisionGate,
        llm: InterviewLLM,
        converter: ConverterLLM,
        scripter: ScripterLLM,
        narrator: NarratorLLM | None = None,
        log: logging.Logger,
        is_start_command: Callable[[Message], bool],
        is_album_follow_up: Callable[[SessionStore, int, Message], bool],
        reset_chat: Callable[..., Any],
        prompt: Callable[..., bool],
        handle_photo: Callable[..., bool],
        app_name: str = APP_NAME,
        session_service: InMemorySessionService | None = None,
    ) -> None:
        self.app_name = app_name
        self._session_service = session_service or InMemorySessionService()

        bouncer = BouncerAgent(
            session=session,
            transport=transport,
            gate=gate,
            llm=llm,
            log=log,
            is_start_command=is_start_command,
            is_album_follow_up=is_album_follow_up,
            reset_chat=reset_chat,
            prompt=prompt,
            handle_photo=handle_photo,
        )
        interviewer = InterviewerAgent(
            session=session,
            transport=transport,
            llm=llm,
            log=log,
            handle_interview_update=handle_interview_update,
        )
        converter_agent = ConverterAgent(
            session=session,
            transport=transport,
            converter=converter,
            log=log,
            handle_conversion=handle_conversion,
        )
        narrator_agent = NarratorAgent(
            session=session,
            transport=transport,
            scripter=scripter,
            narrator=narrator,
            log=log,
            handle_scripting=handle_scripting,
            handle_narrating=handle_narrating,
        )
        self.orchestrator = DocumentaryOrchestrator(
            session=session,
            narrator=narrator,
            bouncer=bouncer,
            interviewer=interviewer,
            converter=converter_agent,
            narrator_agent=narrator_agent,
        )
        self._runner = Runner(
            app_name=app_name,
            agent=self.orchestrator,
            session_service=self._session_service,
        )

    def handle(self, update: Update, *, chat_id: int) -> bool | Skipped:
        """Run one update through the ADK pipeline; return ``bool | Skipped``."""
        return asyncio.run(self._handle_async(update, chat_id))

    async def _handle_async(self, update: Update, chat_id: int) -> bool | Skipped:
        user_id = str(chat_id)
        session_id = str(chat_id)
        session = await self._session_service.get_session(
            app_name=self.app_name, user_id=user_id, session_id=session_id
        )
        if session is None:
            await self._session_service.create_session(
                app_name=self.app_name, user_id=user_id, session_id=session_id
            )

        async for _event in self._runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=types.Content(
                role="user",
                parts=[types.Part(text=f"update {update.update_id}")],
            ),
            state_delta={
                "chat_id": chat_id,
                "update": update.model_dump(mode="json"),
            },
        ):
            pass

        final = await self._session_service.get_session(
            app_name=self.app_name, user_id=user_id, session_id=session_id
        )
        raw = final.state.get(_OVERALL_RESULT_KEY) if final is not None else None
        return _decode_result(raw)
