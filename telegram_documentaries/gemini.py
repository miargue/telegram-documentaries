"""Shared Gemini ``generateContent`` JSON client for every adapter.

The Bouncer (``vision.py``) and the Interviewer (``interviewer.py``) both call
Gemini's REST ``generateContent`` endpoint, post a JSON request, read a JSON
response and validate the model's output against a typed Pydantic schema. The
difference is only the request body (prompt + media vs. prompt + transcript)
and the schema. This module is the **single owner** of that plumbing — and of
the single API-key redaction implementation both adapters rely on.

Every failure — network, HTTP, Telegram-style error payload, malformed or
schema-invalid model output — is normalised to :class:`GeminiError`, and the
API key is redacted from any raised message or log line.

Secret guarantee: no raised :class:`GeminiError` — neither its message nor a
chained cause/context — ever contains the API key. The key travels in
``?key=<key>`` on the request URL, so every normalising branch redacts through
:meth:`GeminiJsonClient._redact` and raises with ``from None`` to keep the raw,
key-bearing cause out of a formatted traceback. Adapters wrap ``GeminiError``
into their own domain error the same way (``VisionError``, ``InterviewError``).
"""

from __future__ import annotations

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


class GeminiJsonClient:
    """POSTs a ``generateContent`` request and returns the parsed candidate.

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
        try:
            request = urllib_request.Request(
                self._endpoint(),
                data=json.dumps(request_body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self._urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
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

        return self._parse(raw, response_schema)

    def _endpoint(self) -> str:
        return (
            f"{self._base_url}/v1beta/models/{self._model}:generateContent"
            f"?key={self._api_key}"
        )

    def _parse(
        self, raw: bytes, response_schema: type[BaseModel]
    ) -> dict[str, Any]:
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

        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise self._fail(
                "no candidates in response"
                f" (promptFeedback={self._prompt_feedback(payload)})"
            ) from None
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
