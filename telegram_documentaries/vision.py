"""Typed vision gate: is a human present in the portrait?

The port (:class:`VisionGate`) is deliberately narrow and typed so the rest of
the pipeline never depends on a vendor SDK — Phase 2 uses a Gemini REST
adapter over stdlib ``urllib`` behind an injectable ``urlopen`` seam, matching
Phase 1's no-third-party-HTTP pattern. Full Google ADK adoption is deferred
(see ``SPECS/2026-10-09-bouncer/requirements.md``).

Since Phase 3 the REST plumbing (endpoint build, POST, failure normalisation,
candidate-text extraction, schema validation and the single API-key redaction
implementation) lives in :mod:`telegram_documentaries.gemini`
(:class:`GeminiJsonClient`), shared with the Interviewer adapter. This adapter
owns only its prompt, request body and the :class:`HumanVerdict` validation.

Every failure — network, HTTP, Telegram-style error payload, malformed or
schema-invalid model output — is normalised to :class:`VisionError`, and the
API key is redacted from any raised message or log line (the shared client
redacts; this adapter wraps the already-redacted :class:`GeminiError`).

Secret guarantee: no raised :class:`VisionError` — neither its message nor a
chained cause/context — ever contains the API key. The key travels in
``?key=<key>`` on the request URL, and :meth:`GeminiVisionGate.classify`
raises with ``from None`` to keep the raw, key-bearing cause out of a
formatted traceback.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Protocol

from pydantic import BaseModel

from telegram_documentaries.defaults import DEFAULT_VISION_MODEL
from telegram_documentaries.gemini import (  # re-exported for compatibility
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT,
    GeminiError,
    GeminiJsonClient,
    UrlopenFn,
)

LOGGER = logging.getLogger(__name__)

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
    in the request URL (as Gemini requires); all redaction and failure
    normalisation is delegated to the shared :class:`GeminiJsonClient`, whose
    errors are wrapped into redacted :class:`VisionError` values raised with
    ``from None``.
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
        self._model = model
        self._gemini = GeminiJsonClient(
            api_key,
            model=model,
            base_url=base_url,
            timeout=timeout,
            urlopen=urlopen,
            logger=LOGGER,
        )

    def classify(
        self, image: bytes, *, media_type: str = "image/jpeg"
    ) -> HumanVerdict:
        """Ask Gemini for a structured human/non-human verdict on ``image``.

        Every failure is normalised by the shared client into a redacted
        :class:`GeminiError`; this adapter wraps it into a redacted
        :class:`VisionError` raised with ``from None`` so the raw, key-bearing
        cause never appears in a formatted traceback.
        """
        try:
            payload = self._gemini.generate_json(
                self._request_body(image, media_type),
                HumanVerdict,
            )
        except GeminiError as exc:
            message = str(exc)
            LOGGER.error(
                "vision classification failed",
                extra={
                    "event": "vision_failed",
                    "model": self._model,
                    "error": message,
                },
            )
            raise VisionError(message) from None
        return HumanVerdict.model_validate(payload)

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
