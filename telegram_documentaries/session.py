"""Minimal versioned, per-``chat_id`` session state driver.

Phase 2 needs the Bouncer phases — waiting for a portrait and having passed
the gate — and Phase 3 adds the Interviewer's ``interviewing``/``done``
phases, the question-then-answer log (``list[QaPair]``) and the behavioural
``Dossier``. The schema is versioned and explicit so later phases extend it
instead of inferring state (see ``SPECS/TECH.md`` §Session state).

This module is the **single owner** of conversation read/write/reset. State is
in-memory only (per ``SPECS/MISSION.md`` — no long-term storage); every chat's
state is keyed by an ``int`` ``chat_id`` so one user's data can never leak into
another's.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

# Bump when the persisted shape changes in a way later phases must migrate.
SESSION_VERSION = 2


class Phase(str, Enum):
    """The pipeline stage a chat is currently in."""

    awaiting_photo = "awaiting_photo"
    gate_passed = "gate_passed"
    interviewing = "interviewing"
    done = "done"


class QaPair(BaseModel):
    """One recorded question and its answer, in the order the user saw them."""

    question: str
    answer: str


class Dossier(BaseModel):
    """The behavioural profile synthesised from the interview."""

    summary: str
    suggested_animal: str
    animal_reason: str | None = None


class SessionState(BaseModel):
    """Everything the gateway remembers about one chat."""

    version: int = SESSION_VERSION
    phase: Phase = Phase.awaiting_photo
    photo: bytes | None = None
    last_media_group_id: str | None = None
    interview: list[QaPair] = []
    # The exact question text this chat is currently looking at, or None when
    # no question is outstanding. Stored so a re-ask reproduces what the user
    # saw without asking the model again.
    pending_question: str | None = None
    dossier: Dossier | None = None


class SessionStore:
    """In-memory, per-``chat_id`` session state.

    ``get`` never fails: an unknown chat starts from a clean default. ``reset``
    always produces a fresh default (purging any retained photo, interview log
    and dossier); ``record_pass`` retains the accepted portrait for later
    pipeline stages; the interview methods are the only writers of the
    question/answer log, the pending question and the final ``Dossier``.
    """

    def __init__(self) -> None:
        self._sessions: dict[int, SessionState] = {}

    def get(self, chat_id: int) -> SessionState:
        """Return this chat's state, creating a fresh default if unseen."""
        state = self._sessions.get(chat_id)
        if state is None:
            state = SessionState()
            self._sessions[chat_id] = state
        return state

    def reset(self, chat_id: int) -> SessionState:
        """Replace this chat's state with a fresh default and purge its data."""
        state = SessionState()
        self._sessions[chat_id] = state
        return state

    def record_pass(self, chat_id: int, photo: bytes) -> SessionState:
        """Retain ``photo`` and advance this chat to ``gate_passed``."""
        state = SessionState(phase=Phase.gate_passed, photo=photo)
        self._sessions[chat_id] = state
        return state

    def remember_media_group(
        self, chat_id: int, media_group_id: str
    ) -> SessionState:
        """Remember the album this chat is currently being gated for.

        Called *after* a verdict is recorded: mutating the current state (rather
        than replacing it) means the dedupe survives the ``reset``/``record_pass``
        that just happened, so the rest of an album is ignored. ``reset`` builds
        a fresh default, which is how ``/start`` clears the dedupe.
        """
        state = self.get(chat_id)
        state.last_media_group_id = media_group_id
        return state

    def start_interview(
        self, chat_id: int, question: str | None = None
    ) -> SessionState:
        """Advance this chat into the ``interviewing`` phase.

        Mutates the existing state (retaining the portrait and any recorded
        answers) so a degraded start retried on the next update never
        duplicates or wipes the conversation. ``question`` is the text just
        sent to the user; storing it makes later re-asks exact and
        LLM-free.
        """
        state = self.get(chat_id)
        state.phase = Phase.interviewing
        if question is not None:
            state.pending_question = question
        return state

    def ask_question(self, chat_id: int, question: str) -> SessionState:
        """Store ``question`` as this chat's outstanding (pending) question."""
        state = self.get(chat_id)
        state.pending_question = question
        return state

    def record_interview_answer(
        self, chat_id: int, question: str, answer: str
    ) -> SessionState:
        """Append one question-then-answer pair and consume the pending one.

        Committing an answer advances the conversation: the outstanding
        question is spent, so ``pending_question`` clears until the next
        :meth:`ask_question` installs its successor.
        """
        state = self.get(chat_id)
        state.interview.append(QaPair(question=question, answer=answer))
        state.pending_question = None
        return state

    def appoint_dossier(self, chat_id: int, dossier: Dossier) -> SessionState:
        """Store the behavioural dossier and mark this chat's interview done."""
        state = self.get(chat_id)
        state.dossier = dossier
        state.pending_question = None
        state.phase = Phase.done
        return state
