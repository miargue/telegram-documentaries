"""Typed boundary models for Telegram Bot API updates.

Every update coming in from Telegram is parsed here first (see
``SPECS/TECH.md`` — contracts at boundaries): nothing downstream of this
module may touch a raw dict. All external input is treated as untrusted, so
only the fields the gateway actually needs are declared, and unknown/extra
fields (photos, stickers, future Telegram additions) are ignored rather than
blowing up the parse.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _TelegramModel(BaseModel):
    """Base config: tolerate unknown fields from an evolving external API."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class Chat(_TelegramModel):
    """A chat the bot can talk to. Only ``id`` is needed to reply."""

    id: int
    type: str = "private"
    title: str | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None


class User(_TelegramModel):
    """The sender of a message."""

    id: int
    is_bot: bool = False
    first_name: str
    last_name: str | None = None
    username: str | None = None


class PhotoSize(_TelegramModel):
    """One resolution of a compressed photo.

    Telegram sends an array of these for a single image (smallest first);
    ``file_size`` is optional metadata. Missing dimensions are a parse error so
    the boundary never hands a malformed size downstream.
    """

    file_id: str
    file_unique_id: str
    width: int
    height: int
    file_size: int | None = None


class FileRef(_TelegramModel):
    """The ``getFile`` result: where the actual bytes live on Telegram's CDN.

    ``file_path`` is optional in the API but required before a download URL can
    be built; the media layer treats a missing or unsafe path as a hard error.
    """

    file_id: str
    file_unique_id: str | None = None
    file_size: int | None = None
    file_path: str | None = None


class Message(_TelegramModel):
    """An incoming message of any content type.

    ``text`` is ``None`` for non-text messages (photos, stickers, ...);
    ``photo`` carries the compressed-photo resolutions when present.
    ``media_group_id`` is set when Telegram groups several photos sent together
    as one album (it is a string in the Bot API). ``date`` and chat metadata
    default so a slightly-minimal payload can still be addressed and acked
    instead of crashing the loop.
    """

    message_id: int
    chat: Chat
    date: int = 0
    from_: User | None = Field(default=None, alias="from")
    text: str | None = None
    caption: str | None = None
    photo: list[PhotoSize] | None = None
    media_group_id: str | None = None


class Update(_TelegramModel):
    """A single Telegram update.

    ``message`` is optional because Telegram also delivers update kinds the
    gateway does not care about yet (edited messages, callback queries, ...);
    those must parse and be acked without crashing.
    """

    update_id: int
    message: Message | None = None
