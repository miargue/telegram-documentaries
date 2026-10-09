"""Behaviour of the Pydantic boundary models for Telegram updates."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from telegram_documentaries.models import Chat, Message, Update, User


def test_parse_text_message_update_exposes_all_echo_relevant_fields() -> None:
    update = Update.model_validate(
        {
            "update_id": 42,
            "message": {
                "message_id": 7,
                "date": 1_700_000_000,
                "chat": {"id": -100, "type": "group", "title": "Docs"},
                "from": {
                    "id": 5,
                    "is_bot": False,
                    "first_name": "Ada",
                    "username": "ada",
                },
                "text": "hey!",
            },
        }
    )

    assert update.update_id == 42
    assert update.message is not None
    assert update.message.text == "hey!"
    assert update.message.chat.id == -100
    assert update.message.chat.type == "group"
    assert update.message.from_ is not None
    assert update.message.from_.username == "ada"
    assert isinstance(update.message.chat, Chat)
    assert isinstance(update.message.from_, User)
    assert isinstance(update.message, Message)


def test_parse_update_without_message_is_valid() -> None:
    update = Update.model_validate({"update_id": 3})

    assert update.update_id == 3
    assert update.message is None


def test_parse_message_without_text_is_valid() -> None:
    update = Update.model_validate(
        {
            "update_id": 4,
            "message": {
                "message_id": 1,
                "date": 1,
                "chat": {"id": 1, "type": "private"},
                "photo": [{"file_id": "x", "file_unique_id": "y"}],
            },
        }
    )

    assert update.message is not None
    assert update.message.text is None


def test_parse_minimal_payload_with_optional_fields_missing() -> None:
    # Telegram-adjacent payloads may omit optional metadata; the gateway must
    # still be able to address the chat and ack the update.
    update = Update.model_validate(
        {
            "update_id": 9,
            "message": {
                "message_id": 1,
                "chat": {"id": 42},
                "text": "hi",
            },
        }
    )

    assert update.message is not None
    assert update.message.chat.id == 42
    assert update.message.date == 0


def test_parse_ignores_unknown_and_future_fields() -> None:
    update = Update.model_validate(
        {
            "update_id": 11,
            "poll": {"question": "?"},
            "message": {
                "message_id": 1,
                "chat": {"id": 1, "type": "private", "pinned_message": {}},
                "text": "hi",
                "entities": [{"type": "bold", "offset": 0, "length": 1}],
                "new_chat_members": [{"id": 2, "first_name": "Bot"}],
            },
        }
    )

    assert update.update_id == 11
    assert update.message is not None
    assert update.message.text == "hi"


def test_update_without_update_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Update.model_validate({"message": {"message_id": 1, "chat": {"id": 1}}})


def test_message_without_chat_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Update.model_validate(
            {"update_id": 1, "message": {"message_id": 1, "text": "hi"}}
        )


def test_chat_without_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Update.model_validate(
            {
                "update_id": 1,
                "message": {
                    "message_id": 1,
                    "chat": {"type": "private"},
                    "text": "hi",
                },
            }
        )


def test_non_object_payload_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Update.model_validate("not an update")


def test_wrong_types_at_the_boundary_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Update.model_validate({"update_id": "not-an-int"})
