"""Typed vision gate: is a human present in the portrait?

The port (:class:`VisionGate`) is deliberately narrow and typed so the rest of
the pipeline never depends on a vendor SDK — Phase 2 uses a Gemini REST
adapter over stdlib ``urllib`` behind an injectable ``urlopen`` seam, matching
Phase 1's no-third-party-HTTP pattern. Full Google ADK adoption is deferred
(see ``SPECS/2026-10-09-bouncer/requirements.md``).

Every failure — network, HTTP, Telegram-style error payload, malformed or
schema-invalid model output — is normalised to :class:`VisionError`, and the
API key is redacted from any raised message or log line.

Secret guarantee: no raised :class:`VisionError` — neither its message nor a
chained cause/context — ever contains the API key. The key travels in
``?key=<key>`` on the request URL, so every normalising branch redacts through
:meth:`GeminiVisionGate._redact` and raises with ``from None`` to keep the raw,
key-bearing cause out of a formatted traceback.
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
from collections.abc import Callable
from typing import Any, Protocol
from urllib import error as urllib_error
from urllib import request as urllib_request

from pydantic import BaseModel, ValidationError

from telegram_documentaries.defaults import DEFAULT_VISION_MODEL

LOGGER = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"
DEFAULT_TIMEOUT = 30.0

# Injectable seam for tests: same call signature as urllib.request.urlopen.
UrlopenFn = Callable[..., Any]

# Ask for a structured, schema-constrained verdict — never a regex scrape.
_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_human": {
            "type": "boolean",
            "description": (
                "True only when a human is clearly visible in the image; "
                "false for animals, objects, food, landscapes or empty images."
            ),
        },
        "reason": {
            "type": "string",
            "description": "A short explanation of the decision.",
        },
    },
    "required": ["is_human"],
}

_BOUNCER_PROMPT = (
    "You are the bouncer for a comedy-wildlife documentary bot. Decide "
    "whether the supplied image clearly contains a human. Reply only with the "
    "requested JSON: is_human is true only for a visible human, false for "
    "animals, pets, objects, food, landscapes, or an empty/unclear image."
)


class HumanVerdict(BaseModel):
    """The gate's decision for one image."""

    is_human: bool
    reason: str | None = None


class VisionError(Exception):
    """A failed vision classification (network, HTTP, or malformed output).

    Guarantee: the message and any chained cause never contain the API key
    (see :class:`GeminiVisionGate`).
    """


class VisionGate(Protocol):
    """Port: classify an image, returning a typed human/non-human verdict."""

    def classify(
        self, image: bytes, *, media_type: str = "image/jpeg"
    ) -> HumanVerdict: ...


class GeminiVisionGate:
    """Gemini ``generateContent`` adapter with a typed JSON response schema.

    ``urlopen`` is injectable so tests never contact Gemini. The API key travels
    in the request URL (as Gemini requires) and is redacted from every error
    message and log line this class emits.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_VISION_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        urlopen: UrlopenFn | None = None,
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

    def classify(
        self, image: bytes, *, media_type: str = "image/jpeg"
    ) -> HumanVerdict:
        """Ask Gemini for a structured human/non-human verdict on ``image``.

        Every failure is normalised to a redacted :class:`VisionError` raised
        with ``from None``: the raw exception can embed the key-bearing
        endpoint (e.g. a real path-validation ``InvalidURL``), and suppressing
        the cause keeps it out of any formatted traceback. The original
        exception type is kept in the message for debugging.
        """
        request = urllib_request.Request(
            self._endpoint(),
            data=json.dumps(self._request_body(image, media_type)).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
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

        return self._parse(raw)

    def _endpoint(self) -> str:
        return (
            f"{self._base_url}/v1beta/models/{self._model}:generateContent"
            f"?key={self._api_key}"
        )

    def _request_body(self, image: bytes, media_type: str) -> dict[str, Any]:
        return {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": _BOUNCER_PROMPT},
                        {
                            "inline_data": {
                                "mime_type": media_type,
                                "data": base64.b64encode(image).decode("ascii"),
                            }
                        },
                    ],
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _RESPONSE_SCHEMA,
            },
        }

    def _parse(self, raw: bytes) -> HumanVerdict:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise self._fail("response is not valid JSON") from None
        if not isinstance(payload, dict):
            raise self._fail(
                f"expected a JSON object, got {type(payload).__name__}"
            ) from None
        if "error" in payload:
            raise self._fail("error payload", self._error_detail(payload)) from None

        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise self._fail(
                "no candidates in response"
                f" (promptFeedback={self._prompt_feedback(payload)})"
            ) from None
        text = self._candidate_text(candidates[0])
        try:
            verdict_payload = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise self._fail("candidate output is not valid JSON") from None
        if not isinstance(verdict_payload, dict):
            raise self._fail("candidate output is not a JSON object") from None
        try:
            return HumanVerdict.model_validate(verdict_payload)
        except ValidationError:
            raise self._fail(
                "candidate output does not match the HumanVerdict schema"
            ) from None

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
            LOGGER.warning(
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

    def _fail(self, kind: str, detail: str | None = None) -> VisionError:
        """Build (and log) a redacted ``VisionError``.

        The returned error's message is always passed through :meth:`_redact`;
        callers raise it with ``from None`` so the raw cause cannot leak the
        API key through a formatted traceback either.
        """
        message = self._redact(kind if detail is None else f"{kind}: {detail}")
        LOGGER.error(
            "vision classification failed",
            extra={
                "event": "vision_failed",
                "model": self._model,
                "error": message,
            },
        )
        return VisionError(message)

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
