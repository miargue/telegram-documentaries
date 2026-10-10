"""Behaviour of the per-``chat_id`` session state driver.

The driver is the single owner of every read/write/reset of conversation
state. It must isolate chats (one user's photo must never leak to another) and
start every unknown chat at a clean ``awaiting_photo`` phase.
"""

from __future__ import annotations

from telegram_documentaries.session import Phase, SessionState, SessionStore


def test_unknown_chat_yields_a_fresh_awaiting_photo_default() -> None:
    store = SessionStore()

    state = store.get(chat_id=42)

    assert state.version == 1
    assert state.phase is Phase.awaiting_photo
    assert state.photo is None


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
    assert state.version == 1
    assert state.phase is Phase.awaiting_photo


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
