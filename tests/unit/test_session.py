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

    assert state.version == 5
    assert state.phase is Phase.awaiting_photo
    assert state.photo is None
    assert state.interview == []
    assert state.dossier is None
    assert state.pending_question is None
    assert state.portrait_sent is False
    assert state.script is None
    assert state.script_sent is False
    assert state.narration is None
    assert state.narrator_sent is False


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
    assert state.version == 5
    assert state.phase is Phase.awaiting_photo
    assert state.interview == []
    assert state.dossier is None
    assert state.portrait_sent is False
    assert state.script is None
    assert state.script_sent is False
    assert state.narration is None
    assert state.narrator_sent is False


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


def _dossier_store(chat_id: int) -> SessionStore:
    """A store whose chat has completed the interview: photo, log, dossier."""
    store = SessionStore()
    store.record_pass(chat_id=chat_id, photo=b"portrait")
    store.start_interview(chat_id=chat_id, question="Q1")
    for number in range(1, 6):
        store.record_interview_answer(chat_id, f"Q{number}", f"a{number}")
    store.appoint_dossier(
        chat_id,
        Dossier(summary="A very dramatic sleeper.", suggested_animal="House cat"),
    )
    return store


def test_mark_portrait_sent_sets_the_flag_without_touching_the_artifacts() -> (
    None
):
    store = _dossier_store(chat_id=7)

    state = store.mark_portrait_sent(chat_id=7)

    assert state.portrait_sent is True
    # Only the flag moves: the photo, interview log and dossier stay intact so
    # later phases (Scripter/Narrator) can still consume them.
    assert state.phase is Phase.done
    assert state.photo == b"portrait"
    assert len(state.interview) == 5
    assert state.dossier is not None
    assert store.get(7).portrait_sent is True


def test_reset_clears_the_portrait_sent_flag() -> None:
    store = _dossier_store(chat_id=7)
    store.mark_portrait_sent(chat_id=7)

    state = store.reset(chat_id=7)

    assert state.portrait_sent is False
    assert store.get(7).portrait_sent is False
    # The reset still purges everything else too.
    assert state.photo is None
    assert state.interview == []
    assert state.dossier is None


def test_portrait_sent_rejects_a_non_boolean_value() -> None:
    with pytest.raises(ValidationError):
        SessionState(portrait_sent="banana")  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        SessionState.model_validate({"portrait_sent": 42})


def test_portrait_flag_is_isolated_per_chat_id() -> None:
    store = _dossier_store(chat_id=1)
    store.get(chat_id=2)  # a second chat, never marked

    store.mark_portrait_sent(chat_id=1)

    assert store.get(1).portrait_sent is True
    assert store.get(2).portrait_sent is False


def test_store_script_sets_the_script_and_leaves_the_flag_alone() -> None:
    store = _dossier_store(chat_id=7)

    state = store.store_script(chat_id=7, script="A dramatic narration.")

    assert state.script == "A dramatic narration."
    assert state.script_sent is False
    # Only the script moves: the artifacts later phases consume survive.
    assert state.phase is Phase.done
    assert state.photo == b"portrait"
    assert state.dossier is not None
    assert store.get(7).script == "A dramatic narration."


def test_mark_script_sent_sets_the_flag_without_touching_the_script() -> None:
    store = _dossier_store(chat_id=7)
    store.store_script(chat_id=7, script="A dramatic narration.")

    state = store.mark_script_sent(chat_id=7)

    assert state.script_sent is True
    assert state.script == "A dramatic narration."
    assert state.phase is Phase.done
    assert state.photo == b"portrait"
    assert len(state.interview) == 5
    assert state.dossier is not None
    assert store.get(7).script_sent is True


def test_reset_clears_the_script_and_the_script_sent_flag() -> None:
    store = _dossier_store(chat_id=7)
    store.store_script(chat_id=7, script="A dramatic narration.")
    store.mark_script_sent(chat_id=7)

    state = store.reset(chat_id=7)

    assert state.script is None
    assert state.script_sent is False
    assert store.get(7).script is None
    assert store.get(7).script_sent is False
    # The reset still purges everything else too.
    assert state.photo is None
    assert state.interview == []
    assert state.dossier is None


def test_script_rejects_a_non_string_value() -> None:
    with pytest.raises(ValidationError):
        SessionState(script=42)  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        SessionState.model_validate({"script": ["not", "a", "string"]})


def test_script_sent_rejects_a_non_boolean_value() -> None:
    with pytest.raises(ValidationError):
        SessionState(script_sent="banana")  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        SessionState.model_validate({"script_sent": 42})


def test_scripts_are_isolated_per_chat_id() -> None:
    store = _dossier_store(chat_id=1)
    store.get(chat_id=2)  # a second chat, never scripted

    store.store_script(chat_id=1, script="chat one narration")

    assert store.get(1).script == "chat one narration"
    assert store.get(2).script is None
    assert store.get(2).script_sent is False


def test_store_narration_sets_the_audio_and_leaves_the_flag_alone() -> None:
    store = _dossier_store(chat_id=7)
    audio = b"OggS\x00narration-audio-bytes"

    state = store.store_narration(chat_id=7, audio=audio)

    assert state.narration == audio
    assert state.narrator_sent is False
    # Only the audio moves: the artifacts later stages consume survive.
    assert state.phase is Phase.done
    assert state.photo == b"portrait"
    assert state.dossier is not None
    assert store.get(7).narration == audio


def test_mark_narrator_sent_sets_the_flag_without_touching_the_audio() -> None:
    store = _dossier_store(chat_id=7)
    store.store_script(chat_id=7, script="A dramatic narration.")
    store.mark_script_sent(chat_id=7)
    audio = b"OggS\x00narration-audio-bytes"
    store.store_narration(chat_id=7, audio=audio)

    state = store.mark_narrator_sent(chat_id=7)

    assert state.narrator_sent is True
    assert state.narration == audio
    assert state.script == "A dramatic narration."
    assert state.script_sent is True
    assert state.portrait_sent is False
    assert state.phase is Phase.done
    assert state.photo == b"portrait"
    assert len(state.interview) == 5
    assert state.dossier is not None
    assert store.get(7).narrator_sent is True


def test_reset_clears_the_narration_and_the_narrator_sent_flag() -> None:
    store = _dossier_store(chat_id=7)
    store.store_script(chat_id=7, script="A dramatic narration.")
    store.mark_script_sent(chat_id=7)
    store.store_narration(chat_id=7, audio=b"OggS\x00audio")
    store.mark_narrator_sent(chat_id=7)

    state = store.reset(chat_id=7)

    assert state.narration is None
    assert state.narrator_sent is False
    assert store.get(7).narration is None
    assert store.get(7).narrator_sent is False
    # The reset still purges everything else too.
    assert state.script is None
    assert state.script_sent is False
    assert state.portrait_sent is False
    assert state.photo is None
    assert state.interview == []
    assert state.dossier is None


def test_narration_rejects_a_non_bytes_value() -> None:
    with pytest.raises(ValidationError):
        SessionState(narration=42)  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        SessionState.model_validate({"narration": ["not", "bytes"]})


def test_narrator_sent_rejects_a_non_boolean_value() -> None:
    with pytest.raises(ValidationError):
        SessionState(narrator_sent="banana")  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        SessionState.model_validate({"narrator_sent": 42})


def test_narrations_are_isolated_per_chat_id() -> None:
    store = _dossier_store(chat_id=1)
    store.get(chat_id=2)  # a second chat, never narrated

    store.store_narration(chat_id=1, audio=b"chat one audio")

    assert store.get(1).narration == b"chat one audio"
    assert store.get(2).narration is None
    assert store.get(2).narrator_sent is False


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
