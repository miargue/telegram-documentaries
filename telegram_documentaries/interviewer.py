"""The Phase 3 interview: a typed LLM port and its Gemini REST adapter.

The port (:class:`InterviewLLM`) is deliberately narrow and typed so the rest
of the pipeline never depends on a vendor SDK: one call to ask the next
question, one to synthesise the behavioural dossier. The Gemini adapter is a
``generateContent`` client over stdlib ``urllib`` with an injectable
``urlopen`` seam, exactly like the Bouncer's vision adapter — no third-party
HTTP dependencies.

Both calls constrain the model with a JSON ``responseSchema`` (never a regex
scrape) and normalise every failure — network, HTTP, error payload, malformed
or schema-invalid output — into a redacted :class:`InterviewError` raised with
``from None``, so the raw key-bearing cause never surfaces in a formatted
traceback.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from pydantic import BaseModel

from telegram_documentaries.defaults import (
    DEFAULT_INTERVIEW_MODEL,
    DEFAULT_QUESTION_COUNT,
    MAX_ANSWER_LENGTH,
)
from telegram_documentaries.gemini import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT,
    GeminiError,
    GeminiJsonClient,
    UrlopenFn,
)
from telegram_documentaries.logging_config import SKIPPED, Skipped, log_lifecycle
from telegram_documentaries.models import Update
from telegram_documentaries.session import Dossier, Phase, QaPair, SessionStore
from telegram_documentaries.transport import (
    MAX_MESSAGE_LENGTH,
    Transport,
    message_length,
    truncate_message,
)


class InterviewError(Exception):
    """A failed LLM call (network, HTTP, or unusable model output).

    Guarantee: the message never contains the API key — the shared Gemini
    client redacts before this class wraps the error, and it is always raised
    with ``from None``.
    """


class InterviewLLM(Protocol):
    """Port: a conversation model that runs the behaviour interview."""

    def next_question(self, transcript: list[QaPair]) -> str:
        """Return the next question given the conversation so far."""
        ...

    def summarize(self, transcript: list[QaPair]) -> Dossier:
        """Synthesise the behavioural dossier from the full transcript."""
        ...


# The interview persona opens every conversation. Both prompts demand the
# structured JSON reply so the responseSchema can take over.
ASK_PROMPT = (
    "You are an investigative, playful, slightly eccentric documentary "
    "researcher interviewing a wildlife subject (in reality the human backer "
    "of a pet). Interview the subject about their sleep cycles, territory "
    "marking, and favourite forage. Keep each question short, whimsical, and "
    "grounded in what the subject has already said."
)

SYNTHESIZE_PROMPT = (
    "You are an investigative, playful, slightly eccentric documentary "
    "researcher reviewing a completed interview. Synthesise the subject's "
    "behavioural dossier: their sleep cycles, territory-marking habits and "
    "favourite forage, then suggest the animal this subject most resembles, "
    "with a short justification. Reply only with the requested JSON."
)

# Shown in place of a transcript when the interview has not started yet: one
# labelled ``user`` content still leads with the ask/dossier instruction alone,
# but Gemini is told explicitly there is nothing to build on.
EMPTY_TRANSCRIPT_NOTE = (
    "No interview has taken place yet - the subject has answered nothing."
)

QUESTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "The next interview question.",
        },
    },
    "required": ["question"],
}

DOSSIER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Behavioural summary synthesised from the interview.",
            "maxLength": 2000,
        },
        "suggested_animal": {
            "type": "string",
            "description": "The animal the subject most resembles.",
            "maxLength": 100,
        },
        "animal_reason": {
            "type": "string",
            "description": "Short justification for the suggested animal.",
            "maxLength": 500,
        },
    },
    "required": ["summary", "suggested_animal"],
}


class _Question(BaseModel):
    question: str


class GeminiInterviewer:
    """A Gemini ``generateContent`` adapter for :class:`InterviewLLM`.

    ``urlopen`` is injectable so tests never contact Gemini. All failure
    normalisation and API-key redaction is delegated to the shared
    :class:`GeminiJsonClient`; every Gemini failure is wrapped into a redacted
    :class:`InterviewError` raised with ``from None``.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_INTERVIEW_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        urlopen: UrlopenFn | None = None,
    ) -> None:
        self._gemini = GeminiJsonClient(
            api_key,
            model=model,
            base_url=base_url,
            timeout=timeout,
            urlopen=urlopen,
            logger=LOGGER,
        )

    def next_question(self, transcript: list[QaPair]) -> str:
        """Ask for the next question, given the conversation so far.

        The returned text must be deliverable by Telegram: a non-empty string
        (after stripping) of at most :data:`MAX_MESSAGE_LENGTH` UTF-16 code
        units. Anything else is an unusable model output and raises
        :class:`InterviewError`, so the caller's existing degrade/retry path
        takes a fresh draw instead of looping on an unsendable question.
        """
        try:
            payload = self._gemini.generate_json(
                self._ask_body(transcript), _Question
            )
        except GeminiError as exc:
            raise InterviewError(str(exc)) from None
        question = payload["question"]
        if not question.strip() or message_length(question) > MAX_MESSAGE_LENGTH:
            raise InterviewError(
                "model returned an unusable question"
            ) from None
        return question

    def summarize(self, transcript: list[QaPair]) -> Dossier:
        """Synthesise and validate the behavioural dossier."""
        try:
            payload = self._gemini.generate_json(
                self._synthesize_body(transcript), Dossier
            )
        except GeminiError as exc:
            raise InterviewError(str(exc)) from None
        return Dossier.model_validate(payload)

    # ------------------------------------------------------------- request body

    def _request_body(
        self,
        system_prompt: str,
        transcript: list[QaPair],
        instruction: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Build a ``generateContent`` body Google will accept.

        The whole transcript rides as a *single* labelled ``user`` content —
        ``Q1: <question>`` / ``A1: <answer>`` lines in order, or
        :data:`EMPTY_TRANSCRIPT_NOTE` when the interview has not started —
        followed by the instruction. The persona lives only in the top-level
        ``systemInstruction`` field, never in a turn, so the model's context is
        one ``user`` content per call and Gemini's consecutive-user-turn rule
        can never be tripped.
        """
        if transcript:
            lines = [
                line
                for index, pair in enumerate(transcript, start=1)
                for line in (
                    f"Q{index}: {pair.question}",
                    f"A{index}: {pair.answer}",
                )
            ]
            transcript_block = "\n".join(lines)
        else:
            transcript_block = EMPTY_TRANSCRIPT_NOTE
        prompt = f"{transcript_block}\n\n{instruction}"
        return {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": schema,
            },
        }

    def _ask_body(self, transcript: list[QaPair]) -> dict[str, Any]:
        instruction = (
            f"Ask question {len(transcript) + 1} of "
            f"{DEFAULT_QUESTION_COUNT} - exactly one question."
        )
        return self._request_body(
            ASK_PROMPT, transcript, instruction, QUESTION_SCHEMA
        )

    def _synthesize_body(self, transcript: list[QaPair]) -> dict[str, Any]:
        return self._request_body(
            SYNTHESIZE_PROMPT,
            transcript,
            "Now build the behavioural dossier as the requested JSON.",
            DOSSIER_SCHEMA,
        )


# --------------------------------------------------------------------------- routing
#
# The Interviewer is the orchestrator of its own turn-taking (see
# ``SPECS/TECH.md``): it owns the phase machine, the 5-question cap and the
# single-question-per-message rule, while the ``InterviewLLM`` merely supplies
# one validated question (or dossier) at a time.
#
# State invariants:
# - The session stores the **pending question** — the exact text the user is
#   currently looking at. A re-ask re-sends that stored text with *no* LLM
#   call, so the user can never be shown two different phrasings of the same
#   question and a non-answer costs no model round-trip.
# - Answers are committed with a **delayed commit**: a pair enters the
#   transcript only after the *next artifact* (question or dossier report) was
#   successfully sent. The interview therefore never advances past what the
#   user actually saw, and a failed dispatch leaves the transcript untouched
#   (the "rollback" the spec demands) — no store-level undo is needed.
# - Nothing raises into the polling loop: every ask/synthesize/send failure
#   degrades to an apology (or, when the transport itself is broken, to a
#   logged ``False``) with the session left retryable.

LOGGER = logging.getLogger(__name__)

APOLOGY_REPLY = (
    "Sorry, my notes got scrambled for a moment. Nothing is lost - please "
    "carry on and I'll pick the interview back up."
)

OBSERVATION_COMPLETE_REPLY = (
    "Observation complete - the subject is classified. Send /restart for a "
    "new subject."
)


def _send(
    transport: Transport, chat_id: int, text: str, log: logging.Logger
) -> bool:
    """Send one message; degrade a failure to ``False`` with a loud log.

    Every outbound message is defensively bounded to Telegram's limit at this
    chokepoint (mechanism-level guard for the over-long outbound-text bug
    class): whatever text the caller produces — a re-ask, a question or a
    dossier report — is truncated before it can be rejected by ``sendMessage``.
    """
    try:
        transport.call(
            "sendMessage",
            {"chat_id": chat_id, "text": truncate_message(text)},
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        log.error(
            "reply failed",
            extra={
                "event": "reply_failed",
                "chat_id": chat_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
            exc_info=True,
        )
        return False
    return True


def _fail_loud(
    chat_id: int,
    *,
    update_id: int | None,
    reason: str,
    log: logging.Logger,
) -> None:
    """Log an interview failure whose message never carries user text."""
    log.error(
        "interview failed",
        extra={
            "event": "interview_failed",
            "chat_id": chat_id,
            "update_id": update_id,
            "error": reason,
        },
    )


def _reask_pending(
    transport: Transport,
    chat_id: int,
    *,
    session: SessionStore,
    update_id: int | None,
    log: logging.Logger,
) -> bool:
    """Re-send the stored pending question for anything that isn't an answer.

    The text comes from the session, never from a fresh model call: a re-ask
    must reproduce exactly what the user originally saw, and a non-answer must
    not cost a model round-trip. A missing pending question is an invariant
    break (``interviewing`` implies one was stored) — fail loud with a log
    and an apology rather than silently asking the model.
    """
    state = session.get(chat_id)
    pending = state.pending_question
    if pending is None:
        _fail_loud(
            chat_id,
            update_id=update_id,
            reason="ValueError: no pending question while interviewing",
            log=log,
        )
        return _send(transport, chat_id, APOLOGY_REPLY, log)
    sent = _send(transport, chat_id, pending, log)
    if sent:
        log.info(
            "question asked",
            extra={
                "event": "question_asked",
                "chat_id": chat_id,
                "update_id": update_id,
                "question_number": len(state.interview) + 1,
            },
        )
    return sent


@log_lifecycle(LOGGER, "start_interview")
def start_interview(
    transport: Transport,
    chat_id: int,
    *,
    session: SessionStore,
    llm: InterviewLLM,
    logger: logging.Logger | None = None,
) -> bool:
    """Send question 1 and advance this chat into the ``interviewing`` phase.

    The phase advances only once both the ask and the send have succeeded, so
    a degraded start leaves the chat in ``gate_passed`` (retryable on the next
    update) and never claims a question the user never saw. The sent text is
    stored as the chat's pending question so re-asks are exact and free.
    """
    log = LOGGER if logger is None else logger
    try:
        question = llm.next_question([])
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        log.error(
            "interview failed",
            extra={
                "event": "interview_failed",
                "chat_id": chat_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
            exc_info=True,
        )
        _send(transport, chat_id, APOLOGY_REPLY, log)
        return False

    if not _send(transport, chat_id, question, log):
        return False

    session.start_interview(chat_id, question)
    log.info(
        "interview started",
        extra={"event": "interview_started", "chat_id": chat_id},
    )
    log.info(
        "question asked",
        extra={
            "event": "question_asked",
            "chat_id": chat_id,
            "question_number": 1,
        },
    )
    return True


def _synthesize_and_report(
    transport: Transport,
    chat_id: int,
    session: SessionStore,
    *,
    transcript: list[QaPair],
    update_id: int | None,
    llm: InterviewLLM,
    log: logging.Logger,
) -> bool:
    """Summarise the completed interview, send the report, then commit.

    The dossier message is sent *before* anything is stored: only once the
    user has actually seen the report is the final answer committed and the
    phase advanced to ``done``. A failed report leaves an ``interviewing``
    chat with a full (4-pair) log whose next message retries synthesis — and
    the user is never left in silence, because the failure is followed by a
    best-effort apology.
    """
    try:
        dossier = llm.summarize(transcript)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        log.error(
            "interview failed",
            extra={
                "event": "interview_failed",
                "chat_id": chat_id,
                "update_id": update_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
            exc_info=True,
        )
        return _send(transport, chat_id, APOLOGY_REPLY, log)

    if not _send(transport, chat_id, _dossier_report(dossier), log):
        # The report never landed; apologise on a best-effort basis (a failure
        # here must not mask the already-degraded outcome) and stay retryable.
        _send(transport, chat_id, APOLOGY_REPLY, log)
        return False

    final_pair = transcript[-1]
    session.record_interview_answer(
        chat_id, final_pair.question, final_pair.answer
    )
    session.appoint_dossier(chat_id, dossier)
    log.info(
        "answer recorded",
        extra={
            "event": "answer_recorded",
            "chat_id": chat_id,
            "update_id": update_id,
            "question_number": len(transcript),
        },
    )
    log.info(
        "dossier created",
        extra={
            "event": "dossier_created",
            "chat_id": chat_id,
            "update_id": update_id,
            # The summary is user-derived text that may echo secrets; only
            # the suggested animal is ever logged (see requirements.md).
            "suggested_animal": dossier.suggested_animal,
        },
    )
    return True


def _dossier_report(dossier: Dossier) -> str:
    """Render the dossier the user sees in the chat."""
    text = (
        "Survey complete - the behavioural dossier is ready!\n"
        f"Suggested animal: {dossier.suggested_animal}\n\n"
        f"{dossier.summary}"
    )
    if dossier.animal_reason:
        text += f"\n\nWhy: {dossier.animal_reason}"
    return text


def _handle_update(
    update: Update,
    transport: Transport,
    *,
    chat_id: int,
    session: SessionStore,
    llm: InterviewLLM,
    log: logging.Logger,
) -> bool | Skipped:
    state = session.get(chat_id)
    if state.phase is Phase.gate_passed:
        # Only a degraded start leaves a chat here; retry it.
        return start_interview(
            transport, chat_id, session=session, llm=llm, logger=log
        )
    if state.phase is Phase.done:
        return _send(transport, chat_id, OBSERVATION_COMPLETE_REPLY, log)
    if state.phase is not Phase.interviewing:
        log.info(
            "interview skipped: chat is not being interviewed",
            extra={
                "event": "interview_skipped",
                "chat_id": chat_id,
                "update_id": update.update_id,
                "phase": state.phase.value,
            },
        )
        return SKIPPED

    message = update.message
    if message is None:
        # Defensive: the router filters messageless updates, but a direct
        # call must skip — never ``assert`` (asserts vanish under ``-O``).
        log.info(
            "interview skipped: update without a message",
            extra={
                "event": "interview_skipped",
                "chat_id": chat_id,
                "update_id": update.update_id,
            },
        )
        return SKIPPED
    text = (message.text or "").strip()
    if not text:
        # Anything that isn't an answer (photo/sticker/voice/document/empty
        # text) re-sends the stored pending question and advances nothing.
        return _reask_pending(
            transport,
            chat_id,
            session=session,
            update_id=update.update_id,
            log=log,
        )

    answer = text
    if len(answer) > MAX_ANSWER_LENGTH:
        answer = answer[:MAX_ANSWER_LENGTH]
        log.info(
            "answer truncated",
            extra={
                "event": "answer_truncated",
                "chat_id": chat_id,
                "update_id": update.update_id,
                "original_length": len(text),
            },
        )

    transcript = list(state.interview)
    pending = state.pending_question
    if pending is None:
        # Invariant break: ``interviewing`` implies an outstanding question.
        # Fail loud (logged) and apologise; never fall back to the model.
        _fail_loud(
            chat_id,
            update_id=update.update_id,
            reason="ValueError: no pending question while interviewing",
            log=log,
        )
        return _send(transport, chat_id, APOLOGY_REPLY, log)

    pair = QaPair(question=pending, answer=answer)
    if len(transcript) + 1 >= DEFAULT_QUESTION_COUNT:
        # Exactly five answers: the next artifact is the dossier report.
        return _synthesize_and_report(
            transport,
            chat_id,
            session,
            transcript=[*transcript, pair],
            update_id=update.update_id,
            llm=llm,
            log=log,
        )

    try:
        next_question = llm.next_question([*transcript, pair])
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        log.error(
            "interview failed",
            extra={
                "event": "interview_failed",
                "chat_id": chat_id,
                "update_id": update.update_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
            exc_info=True,
        )
        return _send(transport, chat_id, APOLOGY_REPLY, log)

    if not _send(transport, chat_id, next_question, log):
        # Delayed commit: the answer never enters the transcript because the
        # user never saw the next question. Apologise and re-send the stored
        # pending question instead — no model call, exact same text.
        _send(transport, chat_id, APOLOGY_REPLY, log)
        _send(transport, chat_id, pending, log)
        return False

    session.record_interview_answer(chat_id, pending, answer)
    session.ask_question(chat_id, next_question)
    log.info(
        "answer recorded",
        extra={
            "event": "answer_recorded",
            "chat_id": chat_id,
            "update_id": update.update_id,
            "question_number": len(transcript) + 1,
        },
    )
    log.info(
        "question asked",
        extra={
            "event": "question_asked",
            "chat_id": chat_id,
            "update_id": update.update_id,
            "question_number": len(transcript) + 2,
        },
    )
    return True


@log_lifecycle(LOGGER, "handle_update")
def handle_update(
    update: Update,
    transport: Transport,
    *,
    session: SessionStore,
    llm: InterviewLLM,
    logger: logging.Logger | None = None,
) -> bool | Skipped:
    """Route one update through the interview state machine.

    ``gate_passed`` retries the (possibly degraded) start; ``interviewing``
    records text answers one at a time and re-sends the stored pending
    question for anything that isn't an answer; ``done`` replies lightly; any
    other phase is a logged no-op. No exception — except process-control ones
    — escapes into the polling loop.
    """
    log = LOGGER if logger is None else logger
    message = update.message
    if message is None:
        log.info(
            "interview skipped: update without a message",
            extra={"event": "interview_skipped", "update_id": update.update_id},
        )
        return SKIPPED
    chat_id = message.chat.id
    try:
        return _handle_update(
            update,
            transport,
            chat_id=chat_id,
            session=session,
            llm=llm,
            log=log,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        log.error(
            "interview failed",
            extra={
                "event": "interview_failed",
                "chat_id": chat_id,
                "update_id": update.update_id,
                "error": f"{type(exc).__name__}: {exc}",
            },
            exc_info=True,
        )
        return _send(transport, chat_id, APOLOGY_REPLY, log)
