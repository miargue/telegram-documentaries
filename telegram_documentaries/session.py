"""Minimal versioned, per-``chat_id`` session state driver.

Phase 2 needs the Bouncer phases — waiting for a portrait and having passed
the gate — and Phase 3 adds the Interviewer's ``interviewing``/``done``
phases, the question-then-answer log (``list[QaPair]``) and the behavioural
``Dossier``. Phase 4 adds the ``portrait_sent`` flag: a ``done`` chat that has
not yet received its hybrid portrait stays retryable, and the flag (never
inferred) marks the true terminal state. Phase 5 adds the raw narration
``script`` and the ``script_sent`` flag (set only once the script actually
reached the chat), so a failed send re-sends the exact stored text while the
raw script stays ready for Phase 6 audio synthesis. The schema is versioned
and explicit so later phases extend it instead of inferring state (see
``SPECS/TECH.md`` §Session state).

This module is the **single owner** of conversation read/write/reset. State is
in-memory only (per ``SPECS/MISSION.md`` — no long-term storage); every chat's
state is keyed by an ``int`` ``chat_id`` so one user's data can never leak into
another's.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

# Bump when the persisted shape changes in a way later phases must migrate.
# v2 (Phase 3): interview log + pending question + dossier.
# v3 (Phase 4): the ``portrait_sent`` flag the Converter sets once the hybrid
# portrait has actually reached the chat.
# v4 (Phase 5): the raw narration ``script`` (ready for Phase 6 audio
# synthesis) and the ``script_sent`` flag marking that it reached the chat.
# v5 (Phase 6): the raw narration audio (``narration``) and the
# ``narrator_sent`` flag marking that the voice note reached the chat.
SESSION_VERSION = 5


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
    # Phase 4: True once the generated hybrid portrait has actually been sent
    # to this chat. Stored (never inferred) so a failed conversion simply
    # leaves it False and the next update retries.
    portrait_sent: bool = False
    # Phase 5: the raw narration script (plain one-paragraph prose, validated
    # and bounded before storage), kept for the Phase 6 audio-synthesis stage.
    script: str | None = None
    # Phase 5: True once the script actually reached this chat. Stored (never
    # inferred) so a failed *send* re-sends the stored script LLM-free while a
    # failed *generation* (nothing stored yet) retries on the next update.
    script_sent: bool = False
    # Phase 6: the raw narration audio payload (OGG), kept like the photo bytes.
    narration: bytes | None = None
    # Phase 6: True once the narration voice note reached this chat. Stored
    # (never inferred) so a failed *send* re-sends the stored audio with no
    # re-synthesis.
    narrator_sent: bool = False


class SessionStore:
    """In-memory, per-``chat_id`` session state.

    ``get`` never fails: an unknown chat starts from a clean default. ``reset``
    always produces a fresh default (purging any retained photo, interview log,
    dossier, portrait flag, script and script flag, narration and narrator flag);
    ``record_pass`` retains the accepted portrait for later pipeline stages; the
    interview methods are the only writers of the question/answer log, the pending
    question and the final ``Dossier``; ``mark_portrait_sent`` is the only writer of
    the Phase 4 portrait flag; ``store_script``/``mark_script_sent`` are the only
    writers of the Phase 5 script and its delivered flag;
    ``store_narration``/``mark_narrator_sent`` are the only writers of the Phase 6
    narration and its delivered flag.
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
        """Replace this chat's state with a fresh default and purge its data.

        A fresh default clears everything the previous subject left behind —
        photo, interview log, dossier, media-group dedupe, the
        ``portrait_sent`` flag, the Phase 5 ``script``/``script_sent`` and the
        Phase 6 ``narration``/``narrator_sent``.
        """
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

    def mark_portrait_sent(self, chat_id: int) -> SessionState:
        """Record that the hybrid portrait reached this chat.

        Called only *after* a successful ``sendPhoto`` (the flag is evidence,
        not intent), which makes ``done && portrait_sent`` the true terminal
        state while a ``done`` chat without it stays retryable. Only the flag
        moves: the photo, interview log and dossier survive for later phases.
        """
        state = self.get(chat_id)
        state.portrait_sent = True
        return state

    def store_script(self, chat_id: int, script: str) -> SessionState:
        """Store the raw narration script for this chat (Phase 5).

        The script is captured *before* the send attempt so a failed delivery
        re-sends the exact stored text on the next update (the Interviewer's
        re-ask pattern — no re-generation). ``script_sent`` is left untouched:
        that flag only moves after the script actually reached the chat.
        """
        state = self.get(chat_id)
        state.script = script
        return state

    def mark_script_sent(self, chat_id: int) -> SessionState:
        """Record that the narration script reached this chat.

        Called only *after* a successful ``sendMessage`` (the flag is evidence,
        not intent): ``script_sent`` makes Phase 6 read a script the user truly
        heard, while ``done && portrait_sent && !script_sent`` stays retryable.
        Only the flag moves — the stored script survives for the Narrator.
        """
        state = self.get(chat_id)
        state.script_sent = True
        return state

    def store_narration(self, chat_id: int, audio: bytes) -> SessionState:
        """Store the raw narration audio for this chat (Phase 6).

        The bytes are captured *before* the send attempt so a failed delivery
        re-sends the exact stored audio on the next update (the re-ask pattern —
        no re-synthesis). ``narrator_sent`` is left untouched: that flag only
        moves after the voice note actually reached the chat.
        """
        state = self.get(chat_id)
        state.narration = audio
        return state

    def mark_narrator_sent(self, chat_id: int) -> SessionState:
        """Record that the narration voice note reached this chat.

        Called only *after* a successful ``sendVoice`` (the flag is evidence,
        not intent): ``done && script_sent && !narrator_sent`` stays retryable
        while ``narrator_sent`` makes the narration terminal. Only the flag
        moves — the stored audio survives if a later stage ever needs it.
        """
        state = self.get(chat_id)
        state.narrator_sent = True
        return state
