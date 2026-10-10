"""Behaviour of the shared Gemini JSON client.

``urlopen`` is injected, so every case runs without a live API or a socket
and the tests can assert on the outgoing request. The client is the single
owner of the REST plumbing the vision and interview adapters share: endpoint
build, POST, failure normalisation, candidate-text extraction, schema
validation — and the one API-key redaction implementation every adapter relies
on (``test_token_redaction.py`` and ``test_vision.py`` lock the same guarantee
at the adapter level).
"""

from __future__ import annotations

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
from pydantic import BaseModel

from telegram_documentaries.gemini import (
    DEFAULT_TIMEOUT,
    GeminiError,
    GeminiJsonClient,
)

SECRET_KEY = "SECRET-GEMINI-KEY-CANARY"


class SampleResponse(BaseModel):
    """A schema the tests can validate candidate output against."""

    value: str


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


def make_client(
    responses: list[Any],
    *,
    api_key: str = SECRET_KEY,
    model: str = "gemini-test-model",
    **kwargs: Any,
) -> tuple[GeminiJsonClient, RecordingUrlopen]:
    opener = RecordingUrlopen(responses)
    client = GeminiJsonClient(
        api_key, model=model, urlopen=opener, **kwargs
    )
    return client, opener


def gemini_body(text: str) -> bytes:
    """A ``generateContent`` response whose only candidate is ``text``."""
    payload = {
        "candidates": [
            {
                "content": {"role": "model", "parts": [{"text": text}]},
                "finishReason": "STOP",
            }
        ]
    }
    return json.dumps(payload).encode("utf-8")


def sample_body(value: str = "hello") -> bytes:
    return gemini_body(json.dumps({"value": value}))


def sample_request() -> dict[str, Any]:
    return {
        "contents": [{"role": "user", "parts": [{"text": "speak"}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        },
    }


def test_generate_json_posts_to_the_generate_content_endpoint() -> None:
    client, opener = make_client([FakeResponse(sample_body())])

    client.generate_json(sample_request(), SampleResponse)

    url, data, timeout = opener.requests[0]
    assert opener.methods[0] == "POST"
    assert url == (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-test-model:generateContent?key={SECRET_KEY}"
    )
    assert data is not None
    assert json.loads(data) == sample_request()
    assert timeout == pytest.approx(DEFAULT_TIMEOUT)


def test_generate_json_uses_a_custom_base_url_and_model() -> None:
    client, opener = make_client(
        [FakeResponse(sample_body())],
        base_url="https://gemini.example.test/",
        model="gemini-custom-model",
    )

    client.generate_json(sample_request(), SampleResponse)

    assert (
        "https://gemini.example.test/v1beta/models/gemini-custom-model:generateContent"
        in opener.requests[0][0]
    )


def test_generate_json_returns_the_schema_validated_candidate_payload() -> None:
    client, _ = make_client([FakeResponse(sample_body("documented value"))])

    payload = client.generate_json(sample_request(), SampleResponse)

    assert payload == {"value": "documented value"}


def test_generate_json_validates_candidate_output_against_the_schema() -> None:
    client, _ = make_client([FakeResponse(gemini_body('{"other": "field"}'))])

    with pytest.raises(GeminiError):
        client.generate_json(sample_request(), SampleResponse)


def test_generate_json_rejects_an_empty_api_key_at_construction() -> None:
    with pytest.raises(ValueError):
        GeminiJsonClient("", model="m")


def test_non_2xx_response_becomes_a_gemini_error() -> None:
    http_error = urllib.error.HTTPError(
        "https://generativelanguage.googleapis.com/v1beta/models/x:generateContent",
        500,
        "Server Error",
        cast(Any, None),
        io.BytesIO(b'{"error": {"code": 500, "message": "backend exploded"}}'),
    )
    client, _ = make_client([http_error])

    with pytest.raises(GeminiError) as exc_info:
        client.generate_json(sample_request(), SampleResponse)

    message = str(exc_info.value)
    assert "500" in message
    assert "backend exploded" in message


def test_error_payload_in_a_200_body_becomes_a_gemini_error() -> None:
    body = json.dumps(
        {
            "error": {
                "code": 400,
                "message": "API key not valid",
                "status": "INVALID_ARGUMENT",
            }
        }
    ).encode()
    client, _ = make_client([FakeResponse(body)])

    with pytest.raises(GeminiError, match="API key not valid"):
        client.generate_json(sample_request(), SampleResponse)


def test_malformed_json_body_becomes_a_gemini_error() -> None:
    client, _ = make_client([FakeResponse(b"not json at all")])

    with pytest.raises(GeminiError, match="JSON"):
        client.generate_json(sample_request(), SampleResponse)


def test_non_object_json_body_becomes_a_gemini_error() -> None:
    client, _ = make_client([FakeResponse(b"[1, 2, 3]")])

    with pytest.raises(GeminiError):
        client.generate_json(sample_request(), SampleResponse)


def test_response_without_candidates_becomes_a_gemini_error() -> None:
    client, _ = make_client([FakeResponse(b'{"candidates": []}')])

    with pytest.raises(GeminiError):
        client.generate_json(sample_request(), SampleResponse)


def test_malformed_candidate_text_becomes_a_gemini_error() -> None:
    client, _ = make_client([FakeResponse(gemini_body("definitely not json"))])

    with pytest.raises(GeminiError):
        client.generate_json(sample_request(), SampleResponse)


def test_network_failure_becomes_a_gemini_error() -> None:
    client, _ = make_client([urllib.error.URLError("connection refused")])

    with pytest.raises(GeminiError):
        client.generate_json(sample_request(), SampleResponse)


def test_a_failure_while_building_the_request_is_normalised_to_a_gemini_error() -> None:
    # Request/JSON construction runs inside the normalising try: a
    # non-serialisable body (TypeError) must surface as a redacted
    # GeminiError, never as a raw exception escaping the client.
    client, opener = make_client([FakeResponse(sample_body())])

    with pytest.raises(GeminiError, match="TypeError"):
        client.generate_json({"payload": object()}, SampleResponse)

    # The request never left the client.
    assert opener.requests == []


def test_protocol_failure_becomes_a_gemini_error() -> None:
    client, _ = make_client([http.client.IncompleteRead(b"half")])

    with pytest.raises(GeminiError):
        client.generate_json(sample_request(), SampleResponse)


@pytest.mark.parametrize(
    "error",
    [
        # A malformed URL raises a plain ValueError that embeds the URL (and
        # therefore the API key) — the client must normalise it, never let it
        # escape raw.
        ValueError(f"unknown url type: https://x?key={SECRET_KEY}"),
        RuntimeError(f"client exploded for key {SECRET_KEY}"),
    ],
)
def test_unexpected_urlopen_error_becomes_a_redacted_gemini_error(
    error: BaseException,
) -> None:
    client, _ = make_client([error])

    with pytest.raises(GeminiError) as exc_info:
        client.generate_json(sample_request(), SampleResponse)

    message = str(exc_info.value)
    traceback_text = "".join(traceback.format_exception(*sys.exc_info()))
    assert error.__class__.__name__ in message
    assert SECRET_KEY not in message
    assert SECRET_KEY not in traceback_text


def test_api_key_is_redacted_from_a_raised_error_message() -> None:
    body = json.dumps({"error": {"message": f"invalid key {SECRET_KEY}"}}).encode()
    client, _ = make_client([FakeResponse(body)])

    with pytest.raises(GeminiError) as exc_info:
        client.generate_json(sample_request(), SampleResponse)

    assert SECRET_KEY not in str(exc_info.value)
    assert "***" in str(exc_info.value)


def _gemini_failures() -> dict[str, Callable[[], BaseException]]:
    """One fresh, key-bearing failure per normalised client branch.

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


def _format_exception(exc_info: Any) -> str:
    record = logging.LogRecord(
        name="gemini.redaction",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="boom",
        args=(),
        exc_info=exc_info,
    )
    from telegram_documentaries.logging_config import JsonFormatter

    return JsonFormatter().format(record)


@pytest.mark.parametrize("label", sorted(_gemini_failures()))
def test_every_normalised_gemini_failure_redacts_the_api_key(label: str) -> None:
    client, _ = make_client([_gemini_failures()[label]()])

    with pytest.raises(GeminiError) as exc_info:
        client.generate_json(sample_request(), SampleResponse)

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
    client, _ = make_client(
        [real_invalid_url(f"/v1beta/models/m:generateContent?key={key}")],
        api_key=key,
    )

    with pytest.raises(GeminiError) as exc_info:
        client.generate_json(sample_request(), SampleResponse)

    error = exc_info.value
    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    json_line = _format_exception((type(error), error, error.__traceback__))

    assert "CANARY" not in str(error)
    assert "CANARY" not in formatted
    assert "CANARY" not in json_line
    assert error.__cause__ is None


def test_unreadable_http_error_body_warns_on_the_injected_logger() -> None:
    # Reading the error body can fail the same way the success path does
    # (dropped connection). That fallback must not be silent — the vision
    # adapter depends on this warning landing on the logger it injects.
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
    records: list[logging.LogRecord] = []
    logger = logging.getLogger("gemini.body.warning")
    handler = _RecordingHandler(records)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    client = GeminiJsonClient(
        SECRET_KEY,
        model="m",
        urlopen=RecordingUrlopen([http_error]),
        logger=logger,
    )
    try:
        with pytest.raises(GeminiError) as exc_info:
            client.generate_json(sample_request(), SampleResponse)
    finally:
        logger.removeHandler(handler)

    assert "500" in str(exc_info.value)
    assert any(
        record.levelno == logging.WARNING
        and getattr(record, "event", None) == "http_error_body_unreadable"
        for record in records
    )
