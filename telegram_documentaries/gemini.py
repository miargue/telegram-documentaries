"""Shared Gemini ``generateContent`` JSON client for every adapter.

The Bouncer (``vision.py``) and the Interviewer (``interviewer.py``) both call
Gemini's REST ``generateContent`` endpoint, post a JSON request, read a JSON
response and validate the model's output against a typed Pydantic schema. The
difference is only the request body (prompt + media vs. prompt + transcript)
and the schema. This module is the **single owner** of that plumbing — and of
the single API-key redaction implementation both adapters rely on.

The Converter (``converter.py``, Phase 4) reuses the same endpoint for *image*
output: :meth:`GeminiJsonClient.generate_image` posts the request through the
identical transport/normalisation/redaction path and decodes the candidate's
first ``inlineData`` part instead of parsing JSON text.

The Narrator (``narrator.py`` + ``GeminiNarrator``) uses a *different* endpoint
for *audio* output: :meth:`GeminiJsonClient.generate_interaction_audio` POSTs
to the newer Gemini **Interactions API** (``POST /v1beta/interactions``) — the
model travels in the request body, not the URL — and decodes the first block of
the first step's content whose ``type`` is ``"audio"``. It shares the same
POST/normalisation/redaction machinery as the ``generateContent`` methods.

Every failure — network, HTTP, Telegram-style error payload, malformed or
schema-invalid model output, or a missing/undecodable image or audio part — is
normalised to :class:`GeminiError`, and the API key is redacted from any
raised message or log line.

Secret guarantee: no raised :class:`GeminiError` — neither its message nor a
chained cause/context — ever contains the API key. The key travels in
``?key=<key>`` on the request URL, so every normalising branch redacts through
:meth:`GeminiJsonClient._redact` and raises with ``from None`` to keep the raw,
key-bearing cause out of a formatted traceback. Adapters wrap ``GeminiError``
into their own domain error the same way (``VisionError``, ``InterviewError``,
``ConverterError``, ``NarratorError``).
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
from collections.abc import Callable
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from pydantic import BaseModel, ValidationError

# Defaults shared by every Gemini adapter (mirrors ``transport.py``).
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"
DEFAULT_TIMEOUT = 30.0

# Injectable seam for tests: same call signature as urllib.request.urlopen.
UrlopenFn = Callable[..., Any]

LOGGER = logging.getLogger(__name__)


class GeminiError(Exception):
    """A failed Gemini ``generateContent`` request.

    Guarantee: the message and any chained cause never contain the API key
    (see :class:`GeminiJsonClient`).
    """


class InlineImagePayload(BaseModel):
    """A model image part, base64-decoded out of a ``generateContent`` candidate.

    The Phase 4 Converter asks Gemini for image output
    (``responseModalities: ["IMAGE"]``) and reads the first ``inlineData`` part
    of the first candidate: ``data`` is the decoded image itself (never logged
    and never sent anywhere but ``sendPhoto``) and ``mime_type`` is the
    model-declared ``mimeType`` such as ``image/png``.
    """

    data: bytes
    mime_type: str


class AudioPayload(BaseModel):
    """A model audio payload, base64-decoded out of a Gemini response.

    The Phase 6 Narrator asks the Interactions API for audio output
    (``response_format: {type: audio, mime_type: audio/mp3}``) and reads the
    first block of the first step's content whose ``type`` is ``"audio"``:
    ``data`` is the decoded audio (never logged) and ``mime_type`` is the
    model-declared value, e.g. ``audio/mp3; rate=24000; channels=1``.
    """

    data: bytes
    mime_type: str


class GeminiJsonClient:
    """POSTs a Gemini request and returns the parsed response.

    ``generate_json`` validates the candidate's JSON text against a schema;
    ``generate_image`` decodes its first inline image part (both on the
    ``generateContent`` endpoint). ``generate_interaction_audio`` posts to the
    Interactions API and decodes its audio block. All share the same POST,
    failure normalisation and API-key redaction implementation.

    ``urlopen`` is injectable so tests never contact Gemini. The API key
    travels in the request URL (as Gemini requires) and is redacted from every
    error message and log line this class emits. ``logger`` is injectable so
    adapters can route the client's warnings onto their own module logger.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        urlopen: UrlopenFn | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("gemini api key must not be empty")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._urlopen: UrlopenFn = (
            urllib_request.urlopen if urlopen is None else urlopen
        )
        self._log = LOGGER if logger is None else logger

    def generate_json(
        self,
        request_body: dict[str, Any],
        response_schema: type[BaseModel],
    ) -> dict[str, Any]:
        """POST ``request_body`` and return the schema-validated candidate.

        Everything that can fail — building the ``Request``/serialising the
        body, the network transfer, and parsing/validating the candidate — is
        normalised to a redacted :class:`GeminiError` raised with ``from None``:
        the raw exception can embed the key-bearing endpoint (e.g. a real
        path-validation ``InvalidURL``), and suppressing the cause keeps it out
        of any formatted traceback. The original exception type is kept in the
        message for debugging.
        """
        return self._parse(self._post(request_body), response_schema)

    def generate_image(self, request_body: dict[str, Any]) -> InlineImagePayload:
        """POST ``request_body`` and decode the first inline image part.

        Identical transport, normalisation and redaction path as
        :meth:`generate_json`; only the candidate extraction differs — image
        bytes arrive base64-encoded in an ``inlineData`` part instead of as
        JSON text. A candidate with no usable image part, or one whose data is
        empty or not valid base64, is a normalised redacted
        :class:`GeminiError`.
        """
        return self._candidate_image(
            self._candidates(self._load(self._post(request_body)))[0]
        )

    def generate_interaction_audio(
        self, request_body: dict[str, Any]
    ) -> AudioPayload:
        """POST ``request_body`` to the Interactions API and decode its audio.

        Unlike the ``generateContent`` methods, the model travels **in the
        request body** and the endpoint is ``<base>/v1beta/interactions``. The
        response carries a ``steps`` list; the audio is the first block of the
        first step's ``content`` whose ``type`` is ``"audio"``. Identical
        transport, normalisation and redaction path as :meth:`generate_json`
        (only the endpoint/extraction differ); a response with no steps, no
        content, no audio block, or an empty/undecodable payload is a
        normalised redacted :class:`GeminiError`.
        """
        return self._audio_block(
            self._steps(
                self._load(self._post_to(self._interactions_endpoint(), request_body))
            )
        )

    def _post(self, request_body: dict[str, Any]) -> bytes:
        """POST the JSON body to ``generateContent`` and return the raw bytes."""
        return self._post_to(self._generate_content_endpoint(), request_body)

    def _post_to(self, endpoint: str, request_body: dict[str, Any]) -> bytes:
        """POST the JSON body to ``endpoint`` and return the raw response bytes.

        The single place any request-level failure is normalised; every method
        (``generate_json``, ``generate_image``, ``generate_interaction_audio``)
        goes through it, so the redaction guarantee is literally shared code.
        """
        try:
            request = urllib_request.Request(
                endpoint,
                data=json.dumps(request_body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self._urlopen(request, timeout=self._timeout) as response:
                return response.read()
        except urllib_error.HTTPError as exc:
            raise self._fail("http error", self._http_detail(exc)) from None
        except urllib_error.URLError as exc:
            raise self._fail(
                "network error", f"{type(exc).__name__}: {exc.reason}"
            ) from None
        except OSError as exc:
            raise self._fail(
                "network error", f"{type(exc).__name__}: {exc}"
            ) from None
        except http.client.HTTPException as exc:
            raise self._fail(
                "protocol error", f"{type(exc).__name__}: {exc}"
            ) from None
        except Exception as exc:
            # Anything else (e.g. a ValueError from a malformed URL) must not
            # escape raw: its message can embed the key-bearing endpoint. The
            # cause is suppressed so the original value can't leak via the
            # formatted traceback either.
            raise self._fail(
                "unexpected error", f"{type(exc).__name__}: {exc}"
            ) from None

    def _generate_content_endpoint(self) -> str:
        return (
            f"{self._base_url}/v1beta/models/{self._model}:generateContent"
            f"?key={self._api_key}"
        )

    def _interactions_endpoint(self) -> str:
        return f"{self._base_url}/v1beta/interactions?key={self._api_key}"

    def _load(self, raw: bytes) -> dict[str, Any]:
        """Decode a response body into the payload object or fail."""
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise self._fail("response is not valid JSON") from None
        if not isinstance(payload, dict):
            raise self._fail(
                f"expected a JSON object, got {type(payload).__name__}"
            ) from None
        if "error" in payload:
            raise self._fail(
                "error payload", self._error_detail(payload)
            ) from None
        return payload

    def _candidates(self, payload: dict[str, Any]) -> list[Any]:
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise self._fail(
                "no candidates in response"
                f" (promptFeedback={self._prompt_feedback(payload)})"
            ) from None
        return candidates

    def _parse(
        self, raw: bytes, response_schema: type[BaseModel]
    ) -> dict[str, Any]:
        candidates = self._candidates(self._load(raw))
        text = self._candidate_text(candidates[0])
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise self._fail("candidate output is not valid JSON") from None
        if not isinstance(parsed, dict):
            raise self._fail("candidate output is not a JSON object") from None
        try:
            validated = response_schema.model_validate(parsed)
        except ValidationError:
            raise self._fail(
                "candidate output does not match the response schema"
            ) from None
        return validated.model_dump()

    def _candidate_text(self, candidate: Any) -> str:
        content = candidate.get("content") if isinstance(candidate, dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            raise self._fail("candidate has no content parts") from None
        texts = [
            part["text"]
            for part in parts
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        if not texts:
            raise self._fail("candidate has no text part") from None
        return "".join(texts)

    def _candidate_image(self, candidate: Any) -> InlineImagePayload:
        """Decode the first ``inlineData`` part of ``candidate``.

        Parts that carry text (a model may narrate alongside the image) or are
        otherwise not image parts are skipped; the first image part must still
        be complete and decodable.
        """
        content = candidate.get("content") if isinstance(candidate, dict) else None
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list):
            raise self._fail("candidate has no content parts") from None
        for part in parts:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData")
            if not isinstance(inline, dict):
                continue
            mime_type, data = self._decode_base64_part(
                inline, label="candidate image part", mime_key="mimeType"
            )
            return InlineImagePayload(data=data, mime_type=mime_type)
        raise self._fail("candidate has no inlineData image part") from None

    def _steps(self, payload: dict[str, Any]) -> list[Any]:
        """Return the Interactions response's non-empty ``steps`` list or fail."""
        steps = payload.get("steps")
        if not isinstance(steps, list) or not steps:
            raise self._fail("response has no steps") from None
        return steps

    def _audio_block(self, steps: list[Any]) -> AudioPayload:
        """Decode the first ``type == "audio"`` block of the first step.

        Only the first step is considered (the response is a single unary
        turn); non-dict blocks and blocks of another ``type`` are skipped, so a
        model that narrates alongside the audio still yields the audio. A first
        step with no content list, or no audio block at all, fails loudly.
        """
        first = steps[0]
        content = first.get("content") if isinstance(first, dict) else None
        if not isinstance(content, list):
            raise self._fail("first step has no content") from None
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "audio":
                mime_type, data = self._decode_base64_part(
                    block, label="audio block", mime_key="mime_type"
                )
                return AudioPayload(data=data, mime_type=mime_type)
        raise self._fail("response has no audio block") from None

    def _decode_base64_part(
        self,
        source: dict[str, Any],
        *,
        label: str,
        mime_key: str,
    ) -> tuple[str, bytes]:
        """Decode the base64 ``data`` and mime type shared by image and audio.

        Both the Converter's ``inlineData`` image part and the Narrator's
        Interactions audio block carry a model-declared mime under a
        (differently spelled) key and base64-encoded ``data``; ``label`` and
        ``mime_key`` let each caller keep its own redacted error wording.
        ``validate=True`` rejects characters outside the base64 alphabet
        instead of silently discarding them (e.g. ``"AAAA!"``), so a corrupted
        payload surfaces as a failure rather than bad bytes, and the
        model-declared mime is passed through verbatim.
        """
        mime_type = source.get(mime_key)
        if not isinstance(mime_type, str) or not mime_type:
            raise self._fail(f"{label} has no {mime_key}") from None
        data = source.get("data")
        if not isinstance(data, str):
            raise self._fail(f"{label} has no data") from None
        try:
            decoded = base64.b64decode(data, validate=True)
        except ValueError:  # binascii.Error is a ValueError subclass
            raise self._fail(f"{label} data is not valid base64") from None
        if not decoded:
            raise self._fail(f"{label} data is empty") from None
        return mime_type, decoded

    def _http_detail(self, exc: urllib_error.HTTPError) -> str:
        raw = self._read_error_body(exc)
        detail = f"HTTP {exc.code} {exc.reason}"
        payload = self._decode_error_body(raw)
        if payload is not None:
            detail = f"{detail}: {self._error_detail(payload)}"
        return detail

    def _read_error_body(self, exc: urllib_error.HTTPError) -> bytes:
        if getattr(exc, "fp", None) is None:
            return b""
        try:
            return exc.read()
        except (OSError, http.client.HTTPException) as read_exc:
            # Mirror the transport: never fall back to "no detail" silently, or
            # a dropped connection while reading the body looks like a clean
            # HTTP response with an empty payload.
            self._log.warning(
                "could not read HTTP error body",
                extra={
                    "event": "http_error_body_unreadable",
                    "model": self._model,
                    "error": self._redact(
                        f"{type(read_exc).__name__}: {read_exc}"
                    ),
                },
            )
            return b""

    def _decode_error_body(self, raw: bytes) -> dict[str, Any] | None:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _error_detail(self, payload: dict[str, Any]) -> str:
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        return "unspecified error"

    def _prompt_feedback(self, payload: dict[str, Any]) -> str:
        feedback = payload.get("promptFeedback")
        return str(feedback) if feedback is not None else "none"

    def _fail(self, kind: str, detail: str | None = None) -> GeminiError:
        """Build a redacted ``GeminiError``.

        The returned error's message is always passed through :meth:`_redact`;
        callers raise it with ``from None`` so the raw cause cannot leak the
        API key through a formatted traceback either.
        """
        message = self._redact(kind if detail is None else f"{kind}: {detail}")
        return GeminiError(message)

    def _redact(self, text: str) -> str:
        if not self._api_key:
            return text
        redacted = text.replace(self._api_key, "***")
        # A control character *inside* the key makes urlopen's InvalidURL
        # repr-escape the selector, so the raw secret no longer appears
        # verbatim; strip its escaped form too.
        escaped = self._api_key.encode("unicode_escape").decode("ascii")
        if escaped != self._api_key:
            redacted = redacted.replace(escaped, "***")
        return redacted
