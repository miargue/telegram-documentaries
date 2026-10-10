"""Behaviour of the per-``chat_id`` session state driver.

The driver is the single owner of every read/write/reset of conversation
state. It must isolate chats (one user's photo must never leak to another) and
start every unknown chat at a clean ``awaiting_photo`` phase.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from telegram_documentaries.session import (
    Dossier,
    Phase,
    QaPair,
    SessionState,
    SessionStore,
)


def test_unknown_chat_yields_a_fresh_awaiting_photo_default() -> None:
    store = SessionStore()

    state = store.get(chat_id=42)

    assert state.version == 2
    assert state.phase is Phase.awaiting_photo
    assert state.photo is None
    assert state.interview == []
    assert state.dossier is None
    assert state.pending_question is None


def test_get_returns_the_same_state_for_the_same_chat() -> None:
    store = SessionStore()
    store.get(chat_id=7)

    assert store.get(chat_id=7) is store.get(chat_id=7)


def test_record_pass_retains_the_photo_and_advances_the_phase() -> None:
    store = SessionStore()
    image = b"\xff\xd8portrait-bytes"

    state = store.record_pass(chat_id=7, photo=image)

    assert state.phase is Phase.gate_passed
    assert state.photo == image
    assert store.get(chat_id=7).photo == image


def test_reset_purges_the_photo_and_returns_to_awaiting_photo() -> None:
    store = SessionStore()
    store.record_pass(chat_id=7, photo=b"image")

    state = store.reset(chat_id=7)

    assert state.phase is Phase.awaiting_photo
    assert state.photo is None
    assert store.get(chat_id=7).phase is Phase.awaiting_photo
    assert store.get(chat_id=7).photo is None


def test_sessions_are_isolated_per_chat_id() -> None:
    store = SessionStore()

    store.record_pass(chat_id=1, photo=b"one")

    other = store.get(chat_id=2)
    assert other.phase is Phase.awaiting_photo
    assert other.photo is None
    # The passing chat still holds its own bytes.
    assert store.get(chat_id=1).photo == b"one"


def test_resetting_one_chat_leaves_another_untouched() -> None:
    store = SessionStore()
    store.record_pass(chat_id=1, photo=b"one")
    store.record_pass(chat_id=2, photo=b"two")

    store.reset(chat_id=1)

    assert store.get(chat_id=1).phase is Phase.awaiting_photo
    assert store.get(chat_id=2).photo == b"two"


def test_reset_on_an_unknown_chat_still_yields_a_clean_default() -> None:
    store = SessionStore()

    state = store.reset(chat_id=99)

    assert state.phase is Phase.awaiting_photo
    assert state.photo is None


def test_session_state_is_a_pydantic_model_with_a_stable_version() -> None:
    state = SessionState()

    assert isinstance(state, SessionState)
    assert state.version == 2
    assert state.phase is Phase.awaiting_photo
    assert state.interview == []
    assert state.dossier is None


def test_unknown_chat_has_no_remembered_media_group() -> None:
    store = SessionStore()

    assert store.get(chat_id=42).last_media_group_id is None


def test_remember_media_group_stores_the_id_on_the_state() -> None:
    store = SessionStore()

    state = store.remember_media_group(chat_id=7, media_group_id="album-1")

    assert state.last_media_group_id == "album-1"
    assert store.get(chat_id=7).last_media_group_id == "album-1"


def test_remembered_media_group_survives_a_pass_and_a_reset() -> None:
    # The Bouncer remembers the album id *after* recording a verdict, so the
    # dedupe must survive whichever state transition just happened.
    passed = SessionStore()
    passed.record_pass(chat_id=1, photo=b"portrait")
    passed.remember_media_group(chat_id=1, media_group_id="album-1")
    assert passed.get(chat_id=1).last_media_group_id == "album-1"

    rejected = SessionStore()
    rejected.reset(chat_id=1)
    rejected.remember_media_group(chat_id=1, media_group_id="album-1")
    assert rejected.get(chat_id=1).last_media_group_id == "album-1"


def test_reset_clears_a_remembered_media_group() -> None:
    # `/start` resets the session, which must also let a reused album id be
    # classified again rather than being silently deduplicated.
    store = SessionStore()
    store.remember_media_group(chat_id=7, media_group_id="album-1")

    state = store.reset(chat_id=7)

    assert state.last_media_group_id is None
    assert store.get(chat_id=7).last_media_group_id is None


def test_interviewing_and_done_phases_exist() -> None:
    assert Phase.interviewing == "interviewing"
    assert Phase.done == "done"


def test_start_interview_enters_interviewing_with_an_empty_log() -> None:
    store = SessionStore()
    store.record_pass(chat_id=7, photo=b"portrait")

    state = store.start_interview(chat_id=7)

    assert state.phase is Phase.interviewing
    assert state.interview == []
    assert state.photo == b"portrait"
    assert store.get(chat_id=7).phase is Phase.interviewing


def test_start_interview_twice_keeps_a_single_conversation() -> None:
    # A degraded start retried on the next update must not duplicate state —
    # the interview log is derived from what the user actually answered.
    store = SessionStore()
    store.start_interview(chat_id=7)
    store.record_interview_answer(
        chat_id=7, question="Where does it sleep?", answer="On a hammock"
    )

    state = store.start_interview(chat_id=7)

    assert state.phase is Phase.interviewing
    assert state.interview == [
        QaPair(question="Where does it sleep?", answer="On a hammock")
    ]


def test_start_interview_stores_the_pending_question() -> None:
    store = SessionStore()
    store.record_pass(chat_id=7, photo=b"portrait")

    state = store.start_interview(chat_id=7, question="Where does it sleep?")

    assert state.pending_question == "Where does it sleep?"


def test_ask_question_advances_the_pending_question() -> None:
    store = SessionStore()
    store.start_interview(chat_id=7, question="Q1")

    state = store.ask_question(chat_id=7, question="Q2")

    assert state.pending_question == "Q2"
    assert store.get(chat_id=7).pending_question == "Q2"


def test_record_interview_answer_consumes_the_pending_question() -> None:
    store = SessionStore()
    store.start_interview(chat_id=7, question="Q1")

    state = store.record_interview_answer(
        chat_id=7, question="Q1", answer="a1"
    )

    # The committed answer consumes the outstanding question; the next ask
    # installs its successor.
    assert state.pending_question is None
    store.ask_question(chat_id=7, question="Q2")
    assert store.get(chat_id=7).pending_question == "Q2"


def test_appoint_dossier_clears_the_pending_question() -> None:
    store = SessionStore()
    store.start_interview(chat_id=7, question="Q5")
    dossier = Dossier(summary="S", suggested_animal="Cat")

    state = store.appoint_dossier(chat_id=7, dossier=dossier)

    assert state.phase is Phase.done
    assert state.pending_question is None


def test_reset_purges_the_pending_question() -> None:
    store = SessionStore()
    store.start_interview(chat_id=7, question="Q1")

    state = store.reset(chat_id=7)

    assert state.pending_question is None
    assert store.get(chat_id=7).pending_question is None


def test_pending_questions_are_isolated_per_chat_id() -> None:
    store = SessionStore()
    store.start_interview(chat_id=1, question="chat one Q1")
    store.start_interview(chat_id=2, question="chat two Q1")

    store.ask_question(chat_id=1, question="chat one Q2")

    assert store.get(chat_id=1).pending_question == "chat one Q2"
    assert store.get(chat_id=2).pending_question == "chat two Q1"


def test_record_interview_answer_appends_question_then_answer_in_order() -> None:
    store = SessionStore()
    store.start_interview(chat_id=7)

    store.record_interview_answer(
        chat_id=7, question="Where does it sleep?", answer="In a hammock"
    )
    state = store.record_interview_answer(
        chat_id=7, question="What does it eat?", answer="Salted crackers"
    )

    assert state.interview == [
        QaPair(question="Where does it sleep?", answer="In a hammock"),
        QaPair(question="What does it eat?", answer="Salted crackers"),
    ]
    assert store.get(chat_id=7).interview == state.interview


def test_appoint_dossier_sets_dossier_and_phase_done() -> None:
    store = SessionStore()
    store.start_interview(chat_id=7)
    dossier = Dossier(
        summary="A very dramatic sleeper.",
        suggested_animal="House cat",
        animal_reason="Owns the hammock.",
    )

    state = store.appoint_dossier(chat_id=7, dossier=dossier)

    assert state.phase is Phase.done
    assert state.dossier == dossier
    assert store.get(chat_id=7).dossier == dossier
    assert store.get(chat_id=7).phase is Phase.done


def test_reset_purges_photo_interview_and_dossier() -> None:
    store = SessionStore()
    store.record_pass(chat_id=7, photo=b"portrait")
    store.start_interview(chat_id=7)
    store.record_interview_answer(
        chat_id=7, question="Where does it sleep?", answer="In a hammock"
    )
    store.appoint_dossier(
        chat_id=7,
        dossier=Dossier(
            summary="A very dramatic sleeper.",
            suggested_animal="House cat",
        ),
    )

    state = store.reset(chat_id=7)

    assert state.phase is Phase.awaiting_photo
    assert state.photo is None
    assert state.interview == []
    assert state.dossier is None
    assert store.get(chat_id=7).interview == []
    assert store.get(chat_id=7).dossier is None


def test_qa_pair_parses_and_validates() -> None:
    pair = QaPair(question="Where does it sleep?", answer="In a hammock")

    assert pair.question == "Where does it sleep?"
    assert pair.answer == "In a hammock"

    with pytest.raises(ValidationError):
        QaPair(question="Only a question", answer=42)  # type: ignore[arg-type]


def test_dossier_parses_and_validates() -> None:
    dossier = Dossier(
        summary="A very dramatic sleeper.",
        suggested_animal="House cat",
    )

    assert dossier.summary == "A very dramatic sleeper."
    assert dossier.suggested_animal == "House cat"
    assert dossier.animal_reason is None

    with pytest.raises(ValidationError):
        Dossier(summary="No animal!", suggested_animal=42)  # type: ignore[arg-type]


def test_two_chats_never_share_an_interview() -> None:
    store = SessionStore()
    store.start_interview(chat_id=1)
    store.record_interview_answer(
        chat_id=1, question="Where does it sleep?", answer="In a hammock"
    )

    other = store.get(chat_id=2)

    assert other.interview == []
    assert store.get(chat_id=1).interview == [
        QaPair(question="Where does it sleep?", answer="In a hammock")
    ]
