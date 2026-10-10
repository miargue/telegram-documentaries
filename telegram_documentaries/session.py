"""Minimal versioned, per-``chat_id`` session state driver.

Phase 2 only needs two phases — waiting for a portrait and having passed the
Bouncer gate — but the schema is versioned and explicit so later phases extend
it instead of inferring state (see ``SPECS/TECH.md`` §Session state).

This module is the **single owner** of conversation read/write/reset. State is
in-memory only (per ``SPECS/MISSION.md`` — no long-term storage); every chat's
state is keyed by an ``int`` ``chat_id`` so one user's data can never leak into
another's.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

# Bump when the persisted shape changes in a way later phases must migrate.
SESSION_VERSION = 1


class Phase(str, Enum):
    """The pipeline stage a chat is currently in."""

    awaiting_photo = "awaiting_photo"
    gate_passed = "gate_passed"


class SessionState(BaseModel):
    """Everything the gateway remembers about one chat."""

    version: int = SESSION_VERSION
    phase: Phase = Phase.awaiting_photo
    photo: bytes | None = None
    last_media_group_id: str | None = None


class SessionStore:
    """In-memory, per-``chat_id`` session state.

    ``get`` never fails: an unknown chat starts from a clean default. ``reset``
    always produces a fresh default (purging any retained photo) and
    ``record_pass`` retains the accepted portrait for later pipeline stages.
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
        """Replace this chat's state with a fresh default and purge its photo."""
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
