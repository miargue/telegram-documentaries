"""Behaviour of the Phase 4 Converter: typed port + Gemini image adapter.

``urlopen`` is injected, so every case runs without a live API or a socket and
the tests assert on the outgoing request body. The adapter fuses the original
portrait with the behavioural dossier in a *single* multimodal call and
normalises every failure into a redacted ``ConverterError``.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import logging
import urllib.error
from collections.abc import Callable
from typing import Any, cast

import pytest
from conftest import (
    FakeConverter,
    FakeTransport,
    _RecordingHandler,
    real_invalid_url,
)

from telegram_documentaries import converter as converter_module
from telegram_documentaries.converter import (
    ConverterError,
    GeminiConverter,
    GeneratedImage,
    handle_conversion,
)
from telegram_documentaries.gemini import DEFAULT_BASE_URL
from telegram_documentaries.session import Dossier, Phase, SessionStore
from telegram_documentaries.transport import TelegramApiError

SECRET_KEY = "SECRET-CONVERTER-KEY-CANARY"

PHOTO = b"\xff\xd8\xff\xe0original-portrait-bytes"
GENERATED = b"\x89PNG\r\n\x1a\nhybrid-animal-portrait"


# --------------------------------------------------------------- fakes

class FakeResponse:
    """Stand-in for the object returned by ``urllib.request.urlopen``."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class RecordingUrlopen:
    """Records requests and replays scripted responses (in order)."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, bytes | None, float]] = []
        self.methods: list[str] = []

    def __call__(self, request: Any, timeout: float) -> FakeResponse:
        self.requests.append((request.full_url, request.data, timeout))
        self.methods.append(request.get_method())
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def make_converter(
    responses: list[Any],
    *,
    api_key: str = SECRET_KEY,
    model: str = "gemini-image-model",
    **kwargs: Any,
) -> tuple[GeminiConverter, RecordingUrlopen]:
    opener = RecordingUrlopen(responses)
    client = GeminiConverter(
        api_key, model=model, urlopen=opener, **kwargs
    )
    return client, opener


def image_body(data: bytes = GENERATED, mime_type: str = "image/png") -> bytes:
    """A ``generateContent`` response carrying one inline image."""
    payload = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": mime_type,
                                "data": base64.b64encode(data).decode("ascii"),
                            }
                        }
                    ],
                },
                "finishReason": "STOP",
            }
        ]
    }
    return json.dumps(payload).encode("utf-8")


def dossier() -> Dossier:
    return Dossier(
        summary="Sleeps eighteen hours a day and hoards shiny cutlery.",
        suggested_animal="caffeinated badger",
        animal_reason="Nocturnal, territorial and permanently over-caffeinated.",
    )


def sent_body(opener: RecordingUrlopen) -> dict[str, Any]:
    """The JSON request the converter actually posted."""
    _url, data, _timeout = opener.requests[0]
    assert data is not None
    return json.loads(data)


# --------------------------------------------------------- request body

def test_hybridize_posts_a_single_user_turn_with_the_photo_inline() -> None:
    client, opener = make_converter([FakeResponse(image_body())])

    client.hybridize(PHOTO, dossier())

    contents = sent_body(opener)["contents"]
    assert len(contents) == 1
    assert contents[0]["role"] == "user"
    parts = contents[0]["parts"]
    assert len(parts) == 2
    assert "text" in parts[0]
    inline = parts[1]["inline_data"]
    assert inline["mime_type"] == "image/jpeg"
    assert base64.b64decode(inline["data"]) == PHOTO


def test_hybridize_keeps_the_persona_out_of_the_turns() -> None:
    client, opener = make_converter([FakeResponse(image_body())])

    client.hybridize(PHOTO, dossier())

    request_body = sent_body(opener)
    system = request_body["systemInstruction"]["parts"][0]["text"]
    user_text = request_body["contents"][0]["parts"][0]["text"]

    assert system.strip()
    assert system not in user_text
    assert "You are" in system


def test_hybridize_directive_fuses_the_likeness_with_the_dossier() -> None:
    client, opener = make_converter([FakeResponse(image_body())])
    subject = dossier()

    client.hybridize(PHOTO, subject)

    directive = sent_body(opener)["contents"][0]["parts"][0]["text"]

    assert subject.suggested_animal in directive
    assert subject.summary in directive
    assert "portrait" in directive.lower()
    assert "image" in directive.lower()


def test_hybridize_asks_for_image_modalities_without_a_response_schema() -> None:
    client, opener = make_converter([FakeResponse(image_body())])

    client.hybridize(PHOTO, dossier())

    config = sent_body(opener)["generationConfig"]

    assert config["responseModalities"] == ["IMAGE"]
    assert "responseSchema" not in config
    assert "responseMimeType" not in config


def test_hybridize_posts_to_the_generate_content_endpoint() -> None:
    client, opener = make_converter([FakeResponse(image_body())])

    client.hybridize(PHOTO, dossier())

    url, data, timeout = opener.requests[0]
    assert opener.methods[0] == "POST"
    assert url == (
        f"{DEFAULT_BASE_URL}/v1beta/models/gemini-image-model:"
        f"generateContent?key={SECRET_KEY}"
    )
    assert data is not None
    assert json.loads(data)["contents"][0]["parts"][1]["inline_data"][
        "mime_type"
    ] == "image/jpeg"
    assert timeout == pytest.approx(120.0)


def test_hybridize_returns_the_generated_image() -> None:
    client, _ = make_converter(
        [FakeResponse(image_body(b"WEBP-DATA", "image/webp"))]
    )

    generated = client.hybridize(PHOTO, dossier())

    assert isinstance(generated, GeneratedImage)
    assert generated.data == b"WEBP-DATA"
    assert generated.mime_type == "image/webp"


def test_hybridize_passes_the_media_type_through() -> None:
    client, opener = make_converter([FakeResponse(image_body())])

    client.hybridize(PHOTO, dossier(), media_type="image/webp")

    _url, data, _timeout = opener.requests[0]
    assert data is not None
    inline = json.loads(data)["contents"][0]["parts"][1]["inline_data"]
    assert inline["mime_type"] == "image/webp"


# ---------------------------------------------------- unusable model output

def test_hybridize_rejects_a_response_without_an_image_part() -> None:
    body = json.dumps(
        {"candidates": [{"content": {"parts": [{"text": "no image"}]}}]}
    ).encode()
    client, _ = make_converter([FakeResponse(body)])

    with pytest.raises(ConverterError):
        client.hybridize(PHOTO, dossier())


def test_hybridize_rejects_a_non_image_media_type() -> None:
    client, _ = make_converter([FakeResponse(image_body(GENERATED, "text/plain"))])

    with pytest.raises(ConverterError):
        client.hybridize(PHOTO, dossier())


def test_hybridize_rejects_a_blank_media_type() -> None:
    client, _ = make_converter([FakeResponse(image_body(GENERATED, ""))])

    with pytest.raises(ConverterError):
        client.hybridize(PHOTO, dossier())


def test_hybridize_rejects_an_empty_image() -> None:
    client, _ = make_converter([FakeResponse(image_body(b""))])

    with pytest.raises(ConverterError):
        client.hybridize(PHOTO, dossier())


def test_hybridize_rejects_an_oversized_image() -> None:
    oversized = b"x" * (converter_module.MAX_PHOTO_BYTES + 1)
    client, _ = make_converter([FakeResponse(image_body(oversized))])

    with pytest.raises(ConverterError):
        client.hybridize(PHOTO, dossier())


def test_hybridize_accepts_an_image_exactly_at_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(converter_module, "MAX_PHOTO_BYTES", 16)
    client, _ = make_converter([FakeResponse(image_body(b"x" * 16))])

    generated = client.hybridize(PHOTO, dossier())

    assert generated.data == b"x" * 16


# ------------------------------------------------------------- failures

@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError("connection refused"),
        urllib.error.HTTPError(
            "https://generativelanguage.googleapis.com/v1beta/models/x:"
            "generateContent",
            500,
            "Server Error",
            cast(Any, None),
            io.BytesIO(b'{"error": {"message": "backend exploded"}}'),
        ),
        http.client.IncompleteRead(b"half"),
        TimeoutError("timed out"),
        b"not json at all",
        json.dumps({"candidates": []}).encode(),
        json.dumps({"error": {"message": "API key not valid"}}).encode(),
    ],
)
def test_hybridize_normalises_every_failure(failure: Any) -> None:
    client, _ = make_converter(
        [failure if isinstance(failure, BaseException) else FakeResponse(failure)]
    )

    with pytest.raises(ConverterError) as exc_info:
        client.hybridize(PHOTO, dossier())

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True


def _converter_failures() -> dict[str, Callable[[], BaseException]]:
    """One fresh, key-bearing failure per normalised converter branch."""
    endpoint = (
        f"https://x/v1beta/models/m:generateContent?key={SECRET_KEY}"
    )
    return {
        "invalid_url": lambda: real_invalid_url(
            f"/v1beta/models/m:generateContent?key={SECRET_KEY}\ncontrol"
        ),
        "url_error": lambda: urllib.error.URLError(
            f"connection refused: {endpoint}"
        ),
        "os_error": lambda: OSError(f"timed out talking to {endpoint}"),
        "http_error": lambda: urllib.error.HTTPError(
            endpoint,
            500,
            f"Server Error {SECRET_KEY}",
            cast(Any, None),
            io.BytesIO(
                json.dumps({"error": {"message": f"backend {SECRET_KEY}"}}).encode()
            ),
        ),
        "non_urllib": lambda: ValueError(f"unknown url type: {endpoint}"),
    }


def _format_exception(exc_info: Any) -> str:
    record = logging.LogRecord(
        name="converter.redaction",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="boom",
        args=(),
        exc_info=exc_info,
    )
    from telegram_documentaries.logging_config import JsonFormatter

    return JsonFormatter().format(record)


@pytest.mark.parametrize("label", sorted(_converter_failures()))
def test_every_converter_failure_redacts_the_api_key(label: str) -> None:
    client, _ = make_converter([_converter_failures()[label]()])

    with pytest.raises(ConverterError) as exc_info:
        client.hybridize(PHOTO, dossier())

    error = exc_info.value
    record = _format_exception((type(error), error, error.__traceback__))

    assert SECRET_KEY not in str(error), f"{label} leaked in str(exc)"
    assert SECRET_KEY not in record, f"{label} leaked via JsonFormatter"
    assert error.__cause__ is None, f"{label} leaked via __cause__"
    assert error.__suppress_context__ is True, f"{label} leaked via __context__"


def test_converter_redacts_a_control_character_key() -> None:
    key = "SECRET-CONVERTER-KEY-\nCANARY"
    client, _ = make_converter(
        [real_invalid_url(f"/v1beta/models/m:generateContent?key={key}")],
        api_key=key,
    )

    with pytest.raises(ConverterError) as exc_info:
        client.hybridize(PHOTO, dossier())

    error = exc_info.value
    assert "CANARY" not in str(error)
    assert "CANARY" not in (
        str(error).encode("unicode_escape").decode("ascii")
    )
    assert error.__cause__ is None


# ------------------------------------------------------ routing / delivery
#
# ``handle_conversion`` is the Converter's own turn: teaser, one multimodal
# generation, one ``sendPhoto``, then the terminal flag. Every failure degrades
# to an apology (or a reset-and-reprompt for a broken session) and never raises
# into the polling loop.

PORTRAIT = GeneratedImage(data=b"\x89PNG\r\n\x1a\nhybrid", mime_type="image/png")


def done_store(
    chat_id: int = 1,
    *,
    photo: bytes | None = PHOTO,
    subject: Dossier | None = None,
) -> SessionStore:
    """A ``done`` chat holding the photo and dossier the Converter consumes."""
    store = SessionStore()
    if photo is not None:
        store.record_pass(chat_id, photo)
    store.appoint_dossier(chat_id, dossier() if subject is None else subject)
    return store


def events(records: list[logging.LogRecord]) -> list[str]:
    return [str(getattr(record, "event", "")) for record in records]


def messages(transport: FakeTransport) -> list[str]:
    return [params["text"] for params in transport.calls_for("sendMessage")]


def test_handle_conversion_delivers_the_portrait_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = done_store()
    transport = FakeTransport()
    converter = FakeConverter([PORTRAIT])
    records: list[logging.LogRecord] = []
    logger = logging.getLogger("converter.flow")
    handler = _RecordingHandler(records)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    flag_at_send: list[bool] = []
    original_send_photo = transport.send_photo

    def spy_send_photo(*args: Any, **kwargs: Any) -> dict[str, Any]:
        flag_at_send.append(session.get(1).portrait_sent)
        return original_send_photo(*args, **kwargs)

    monkeypatch.setattr(transport, "send_photo", spy_send_photo)
    try:
        delivered = handle_conversion(
            transport, 1, session=session, converter=converter, logger=logger
        )
    finally:
        logger.removeHandler(handler)

    assert delivered is True
    # Teaser landed before generation, generation before upload, flag last.
    assert messages(transport)[0] == converter_module.TEASER_REPLY
    assert flag_at_send == [False]
    assert session.get(1).portrait_sent is True
    # The upload carries the *generated* image, not the original photo.
    assert len(transport.photos) == 1
    photo = transport.photos[0]
    assert photo["chat_id"] == 1
    assert photo["image"] == PORTRAIT.data
    assert photo["image"] != PHOTO
    assert photo["media_type"] == "image/png"
    assert "caffeinated badger" in photo["caption"]
    # Exactly one teaser, one portrait.
    assert messages(transport).count(converter_module.TEASER_REPLY) == 1
    assert len(transport.photos) == 1
    assert converter.calls == [(PHOTO, session.get(1).dossier, "image/jpeg")]
    assert events(records) == [
        "conversion_started",
        "conversion_succeeded",
        "portrait_sent",
    ]


def test_handle_conversion_apologises_when_generation_fails() -> None:
    session = done_store()
    transport = FakeTransport()
    converter = FakeConverter([ConverterError("boom")])
    records: list[logging.LogRecord] = []
    logger = logging.getLogger("converter.genfail")
    handler = _RecordingHandler(records)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        delivered = handle_conversion(
            transport, 1, session=session, converter=converter, logger=logger
        )
    finally:
        logger.removeHandler(handler)

    assert delivered is False
    assert session.get(1).portrait_sent is False
    assert transport.photos == []
    assert messages(transport)[-1] == converter_module.APOLOGY_REPLY
    assert "conversion_failed" in events(records)
    assert "conversion_succeeded" not in events(records)


def test_handle_conversion_never_raises_on_an_unexpected_generation_error() -> None:
    session = done_store()
    transport = FakeTransport()
    converter = FakeConverter([RuntimeError("kaput")])

    delivered = handle_conversion(
        transport, 1, session=session, converter=converter
    )

    assert delivered is False
    assert session.get(1).portrait_sent is False
    assert transport.photos == []


def test_handle_conversion_apologises_and_stays_retryable_when_upload_fails() -> None:
    session = done_store()
    transport = FakeTransport(
        fail_on={"sendPhoto": TelegramApiError("sendPhoto", "blocked")}
    )
    converter = FakeConverter([PORTRAIT])
    records: list[logging.LogRecord] = []
    logger = logging.getLogger("converter.sendfail")
    handler = _RecordingHandler(records)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        delivered = handle_conversion(
            transport, 1, session=session, converter=converter, logger=logger
        )
    finally:
        logger.removeHandler(handler)

    assert delivered is False
    assert session.get(1).portrait_sent is False
    assert messages(transport)[-1] == converter_module.APOLOGY_REPLY
    assert "conversion_failed" in events(records)


def test_handle_conversion_resets_and_reprompts_when_the_photo_is_missing() -> None:
    store = SessionStore()
    store.appoint_dossier(1, dossier())  # done, but no photo was ever retained
    transport = FakeTransport()
    records: list[logging.LogRecord] = []
    logger = logging.getLogger("converter.nophoto")
    handler = _RecordingHandler(records)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        delivered = handle_conversion(
            transport, 1, session=store, converter=FakeConverter(), logger=logger
        )
    finally:
        logger.removeHandler(handler)

    assert delivered is False
    assert store.get(1).phase is Phase.awaiting_photo
    assert store.get(1).dossier is None
    assert transport.photos == []
    assert messages(transport) == [converter_module.RESET_REPLY]
    assert "conversion_failed" in events(records)
    failures = [r for r in records if r.levelno >= logging.ERROR]
    assert failures, "a broken session must be logged loudly"


def test_handle_conversion_resets_and_reprompts_when_the_dossier_is_missing() -> None:
    store = SessionStore()
    state = store.record_pass(1, PHOTO)
    state.phase = Phase.done  # invariant break: done without a dossier
    transport = FakeTransport()

    delivered = handle_conversion(
        transport, 1, session=store, converter=FakeConverter()
    )

    assert delivered is False
    assert store.get(1).phase is Phase.awaiting_photo
    assert messages(transport) == [converter_module.RESET_REPLY]


def test_handle_conversion_skips_a_chat_that_is_not_done() -> None:
    store = SessionStore()
    transport = FakeTransport()
    records: list[logging.LogRecord] = []
    logger = logging.getLogger("converter.skip")
    handler = _RecordingHandler(records)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        delivered = handle_conversion(
            transport, 1, session=store, converter=FakeConverter(), logger=logger
        )
    finally:
        logger.removeHandler(handler)

    assert delivered is False
    assert transport.calls == []
    assert "conversion_skipped" in events(records)


def test_handle_conversion_skips_a_chat_whose_portrait_was_already_sent() -> None:
    store = done_store()
    store.mark_portrait_sent(1)
    transport = FakeTransport()
    records: list[logging.LogRecord] = []
    logger = logging.getLogger("converter.alreadysent")
    handler = _RecordingHandler(records)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        delivered = handle_conversion(
            transport, 1, session=store, converter=FakeConverter(), logger=logger
        )
    finally:
        logger.removeHandler(handler)

    assert delivered is False
    assert transport.calls == []
    assert "conversion_skipped" in events(records)


def test_portrait_caption_is_short_and_names_the_animal() -> None:
    caption = converter_module.PORTRAIT_CAPTION.format(animal="meticulous beaver")

    assert "meticulous beaver" in caption
    assert len(caption) < 200
