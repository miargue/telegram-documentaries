"""Behaviour of the typed vision gate and its Gemini REST adapter.

``urlopen`` is injected, so every case runs without a live API or a socket and
the tests can assert on the outgoing request body.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import logging
import sys
import traceback
import urllib.error
from collections.abc import Callable
from typing import Any, cast

import pytest
from conftest import _RecordingHandler, real_invalid_url

from telegram_documentaries.logging_config import JsonFormatter
from telegram_documentaries.vision import (
    DEFAULT_VISION_MODEL,
    GeminiVisionGate,
    HumanVerdict,
    VisionError,
)

SECRET_KEY = "SECRET-GEMINI-KEY-CANARY"


def _format_exception(exc_info: Any) -> str:
    record = logging.LogRecord(
        name="vision.redaction",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="boom",
        args=(),
        exc_info=exc_info,
    )
    return JsonFormatter().format(record)


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


def make_gate(
    responses: list[Any],
    *,
    api_key: str = SECRET_KEY,
    **kwargs: Any,
) -> tuple[GeminiVisionGate, RecordingUrlopen]:
    opener = RecordingUrlopen(responses)
    gate = GeminiVisionGate(api_key, urlopen=opener, **kwargs)
    return gate, opener


def gemini_body(verdict: dict[str, Any] | str) -> bytes:
    text = verdict if isinstance(verdict, str) else json.dumps(verdict)
    payload = {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text}]},
                "finishReason": "STOP",
            }
        ]
    }
    return json.dumps(payload).encode("utf-8")


def test_classify_returns_a_human_verdict_when_a_person_is_present() -> None:
    gate, _ = make_gate(
        [FakeResponse(gemini_body({"is_human": True, "reason": "a face is visible"}))]
    )

    verdict = gate.classify(b"\xff\xd8portrait")

    assert isinstance(verdict, HumanVerdict)
    assert verdict.is_human is True
    assert verdict.reason == "a face is visible"


def test_classify_returns_a_non_human_verdict_without_a_reason() -> None:
    gate, _ = make_gate([FakeResponse(gemini_body({"is_human": False}))])

    verdict = gate.classify(b"\xff\xd8landscape")

    assert verdict.is_human is False
    assert verdict.reason is None


def test_classify_posts_the_image_as_base64_inline_data() -> None:
    gate, opener = make_gate(
        [FakeResponse(gemini_body({"is_human": True}))]
    )
    image = b"\xff\xd8ORIGINAL-IMAGE-BYTES"

    gate.classify(image, media_type="image/png")

    url, data, timeout = opener.requests[0]
    assert opener.methods[0] == "POST"
    assert url == (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{DEFAULT_VISION_MODEL}:generateContent?key={SECRET_KEY}"
    )
    assert data is not None
    body = json.loads(data)
    parts = body["contents"][0]["parts"]
    inline = next(part for part in parts if "inline_data" in part)["inline_data"]
    assert inline["mime_type"] == "image/png"
    assert base64.b64decode(inline["data"]) == image
    assert timeout == pytest.approx(30.0)


def test_classify_requests_a_json_schema_not_a_regex() -> None:
    gate, opener = make_gate(
        [FakeResponse(gemini_body({"is_human": True}))]
    )

    gate.classify(b"\xff\xd8img")

    body = json.loads(opener.requests[0][1] or b"{}")
    generation = body["generationConfig"]
    assert generation["responseMimeType"] == "application/json"
    schema = generation["responseSchema"]
    assert schema["type"] == "object"
    assert schema["properties"]["is_human"]["type"] == "boolean"
    assert schema["required"] == ["is_human"]


def test_default_media_type_is_jpeg() -> None:
    gate, opener = make_gate(
        [FakeResponse(gemini_body({"is_human": True}))]
    )

    gate.classify(b"\xff\xd8img")

    body = json.loads(opener.requests[0][1] or b"{}")
    parts = body["contents"][0]["parts"]
    inline = next(part for part in parts if "inline_data" in part)["inline_data"]
    assert inline["mime_type"] == "image/jpeg"


def test_custom_model_is_used_in_the_url() -> None:
    gate, opener = make_gate(
        [FakeResponse(gemini_body({"is_human": True}))],
        model="gemini-custom-model",
    )

    gate.classify(b"\xff\xd8img")

    assert (
        "/v1beta/models/gemini-custom-model:generateContent"
        in opener.requests[0][0]
    )


def test_empty_api_key_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        GeminiVisionGate("")


def test_non_2xx_response_becomes_a_vision_error() -> None:
    http_error = urllib.error.HTTPError(
        "https://generativelanguage.googleapis.com/v1beta/models/x:generateContent",
        500,
        "Server Error",
        cast(Any, None),
        io.BytesIO(b'{"error": {"code": 500, "message": "backend exploded"}}'),
    )
    gate, _ = make_gate([http_error])

    with pytest.raises(VisionError) as exc_info:
        gate.classify(b"\xff\xd8img")

    message = str(exc_info.value)
    assert "500" in message
    assert "backend exploded" in message


def test_unreadable_http_error_body_falls_back_and_logs() -> None:
    # Reading the error body can fail the same way the success path does
    # (dropped connection). That fallback must not be silent: it is logged at
    # WARNING so a missing error detail is never mistaken for "no error".
    class UnreadableBody:
        def read(self, *_args: Any, **_kwargs: Any) -> bytes:
            raise http.client.IncompleteRead(b"half a body")

        def close(self) -> None:
            return None

    http_error = urllib.error.HTTPError(
        "https://generativelanguage.googleapis.com/v1beta/models/x:generateContent",
        500,
        "Server Error",
        cast(Any, {}),
        cast(Any, UnreadableBody()),
    )
    gate, _ = make_gate([http_error])

    records: list[logging.LogRecord] = []
    logger = logging.getLogger("telegram_documentaries.vision")
    handler = _RecordingHandler(records)
    handler.setFormatter(JsonFormatter())
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        with pytest.raises(VisionError) as exc_info:
            gate.classify(b"\xff\xd8img")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    assert "500" in str(exc_info.value)
    assert any(
        record.levelno == logging.WARNING
        and getattr(record, "event", None) == "http_error_body_unreadable"
        for record in records
    )


def test_error_payload_in_a_200_body_becomes_a_vision_error() -> None:
    body = json.dumps(
        {
            "error": {
                "code": 400,
                "message": "API key not valid",
                "status": "INVALID_ARGUMENT",
            }
        }
    ).encode()
    gate, _ = make_gate([FakeResponse(body)])

    with pytest.raises(VisionError, match="API key not valid"):
        gate.classify(b"\xff\xd8img")


def test_malformed_json_body_becomes_a_vision_error() -> None:
    gate, _ = make_gate([FakeResponse(b"not json at all")])

    with pytest.raises(VisionError, match="JSON"):
        gate.classify(b"\xff\xd8img")


def test_malformed_candidate_text_becomes_a_vision_error() -> None:
    gate, _ = make_gate([FakeResponse(gemini_body("definitely not json"))])

    with pytest.raises(VisionError):
        gate.classify(b"\xff\xd8img")


def test_schema_invalid_candidate_output_becomes_a_vision_error() -> None:
    gate, _ = make_gate([FakeResponse(gemini_body({"reason": "no verdict"}))])

    with pytest.raises(VisionError):
        gate.classify(b"\xff\xd8img")


def test_response_without_candidates_becomes_a_vision_error() -> None:
    gate, _ = make_gate([FakeResponse(b'{"candidates": []}')])

    with pytest.raises(VisionError):
        gate.classify(b"\xff\xd8img")


def test_non_object_json_body_becomes_a_vision_error() -> None:
    gate, _ = make_gate([FakeResponse(b"[1, 2, 3]")])

    with pytest.raises(VisionError):
        gate.classify(b"\xff\xd8img")


def test_network_failure_becomes_a_vision_error() -> None:
    gate, _ = make_gate([urllib.error.URLError("connection refused")])

    with pytest.raises(VisionError):
        gate.classify(b"\xff\xd8img")


def test_protocol_failure_becomes_a_vision_error() -> None:
    gate, _ = make_gate([http.client.IncompleteRead(b"half")])

    with pytest.raises(VisionError):
        gate.classify(b"\xff\xd8img")


def test_api_key_is_redacted_from_a_raised_error_message() -> None:
    body = json.dumps(
        {"error": {"message": f"invalid key {SECRET_KEY}"}}
    ).encode()
    gate, _ = make_gate([FakeResponse(body)])

    with pytest.raises(VisionError) as exc_info:
        gate.classify(b"\xff\xd8img")

    assert SECRET_KEY not in str(exc_info.value)
    assert "***" in str(exc_info.value)


def test_api_key_is_redacted_from_logs() -> None:
    body = json.dumps(
        {"error": {"message": f"invalid key {SECRET_KEY}"}}
    ).encode()
    gate, _ = make_gate([FakeResponse(body)])

    records: list[logging.LogRecord] = []
    logger = logging.getLogger("telegram_documentaries.vision")
    handler = _RecordingHandler(records)
    handler.setFormatter(JsonFormatter())
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        with pytest.raises(VisionError):
            gate.classify(b"\xff\xd8img")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    assert records, "a failed vision call must be logged"
    lines = [JsonFormatter().format(record) for record in records]
    assert all(SECRET_KEY not in line for line in lines)


@pytest.mark.parametrize(
    "error",
    [
        # A malformed URL raises a plain ValueError that embeds the URL (and
        # therefore the API key) — it must be normalised, never escape raw.
        ValueError(f"unknown url type: https://x?key={SECRET_KEY}"),
        RuntimeError(f"client exploded for key {SECRET_KEY}"),
    ],
)
def test_unexpected_urlopen_error_becomes_a_redacted_vision_error(
    error: BaseException,
) -> None:
    gate, _ = make_gate([error])

    with pytest.raises(VisionError) as exc_info:
        gate.classify(b"\xff\xd8img")

    message = str(exc_info.value)
    traceback_text = "".join(traceback.format_exception(*sys.exc_info()))
    assert error.__class__.__name__ in message
    assert SECRET_KEY not in message
    assert SECRET_KEY not in traceback_text


def test_unexpected_urlopen_error_is_redacted_from_logs() -> None:
    gate, _ = make_gate([ValueError(f"unknown url type: https://x?key={SECRET_KEY}")])

    records: list[logging.LogRecord] = []
    logger = logging.getLogger("telegram_documentaries.vision")
    handler = _RecordingHandler(records)
    handler.setFormatter(JsonFormatter())
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        with pytest.raises(VisionError):
            gate.classify(b"\xff\xd8img")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    assert records, "a normalised failure must be logged"
    lines = [JsonFormatter().format(record) for record in records]
    assert all(SECRET_KEY not in line for line in lines)


def _vision_failures() -> dict[str, Callable[[], BaseException]]:
    """One fresh, key-bearing failure per normalised vision branch.

    The API key travels in ``?key=<key>`` on the request URL, so every error
    whose text embeds the URL/selector — a real path-validation ``InvalidURL``,
    a ``URLError``/``OSError`` reason, or an ``HTTPError`` body — carries it.
    """
    endpoint = f"https://x/v1beta/models/m:generateContent?key={SECRET_KEY}"
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


@pytest.mark.parametrize("label", sorted(_vision_failures()))
def test_every_normalised_vision_failure_redacts_the_api_key(label: str) -> None:
    gate, _ = make_gate([_vision_failures()[label]()])

    with pytest.raises(VisionError) as exc_info:
        gate.classify(b"\xff\xd8img")

    error = exc_info.value
    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    json_line = _format_exception((type(error), error, error.__traceback__))

    assert SECRET_KEY not in str(error), f"{label} leaked in str(exc)"
    assert SECRET_KEY not in formatted, f"{label} leaked via traceback"
    assert SECRET_KEY not in json_line, f"{label} leaked via JsonFormatter"
    # The raw cause is suppressed so the key-bearing original never appears in
    # a formatted traceback.
    assert error.__cause__ is None, f"{label} leaked via __cause__"
    assert error.__suppress_context__ is True, f"{label} leaked via __context__"


def test_api_key_with_a_control_character_is_still_redacted() -> None:
    # A control character *inside* the key makes urlopen's InvalidURL
    # repr-escape the selector, so the raw key no longer appears verbatim in
    # the message. Redaction must still remove its escaped form.
    key = "SECRET-GEMINI-KEY-\nCANARY"
    gate, _ = make_gate(
        [real_invalid_url(f"/v1beta/models/m:generateContent?key={key}")],
        api_key=key,
    )

    with pytest.raises(VisionError) as exc_info:
        gate.classify(b"\xff\xd8img")

    error = exc_info.value
    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    json_line = _format_exception((type(error), error, error.__traceback__))

    assert "CANARY" not in str(error)
    assert "CANARY" not in formatted
    assert "CANARY" not in json_line
    assert error.__cause__ is None


def test_normalised_vision_failure_never_leaks_the_key_into_logs() -> None:
    gate, _ = make_gate([_vision_failures()["invalid_url"]()])
    records: list[logging.LogRecord] = []
    logger = logging.getLogger("telegram_documentaries.vision")
    handler = _RecordingHandler(records)
    handler.setFormatter(JsonFormatter())
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        try:
            gate.classify(b"\xff\xd8img")
        except VisionError:
            # Mirrors production: a caller logs the failure with its traceback.
            logger.error("caller logged the failure", exc_info=True)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    assert records, "a failed vision call must be logged"
    lines = [JsonFormatter().format(record) for record in records]
    assert all(SECRET_KEY not in line for line in lines)


def test_human_verdict_requires_is_human() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        HumanVerdict.model_validate({"reason": "human please"})
