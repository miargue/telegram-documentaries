"""Telegram Bot API transport over stdlib ``urllib`` (no third-party HTTP).

The transport is the only module that talks to the network. ``urlopen`` is
injectable so the whole gateway can be exercised in tests without a live bot
or a socket (see ``tests/unit/test_transport.py`` and
``tests/component/test_full_gateway_flow.py``).

Failures never come back as bare exceptions from deep inside the stack: every
problem is normalised into ``TelegramApiError`` with the failing method,
description and (when available) HTTP/Telegram error codes — including network
failures, wire-protocol failures (``http.client.HTTPException`` subclasses such
as ``IncompleteRead``) and Telegram's own ``ok: false`` payloads.
"""

from __future__ import annotations

import http.client
import json
import logging
from collections.abc import Callable, Mapping
from typing import Any, Protocol
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

LOGGER = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.telegram.org"
DEFAULT_TIMEOUT = 30.0

# Injectable seam for tests: same call signature as urllib.request.urlopen.
UrlopenFn = Callable[..., Any]


class TelegramApiError(Exception):
    """A failed call to the Telegram Bot API (network, HTTP or ``ok: false``)."""

    def __init__(
        self,
        method: str,
        description: str,
        *,
        error_code: int | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(f"{method} failed: {description}")
        self.method = method
        self.description = description
        self.error_code = error_code
        self.status_code = status_code


class Transport(Protocol):
    """Minimal contract the polling loop depends on (fakeable in tests)."""

    def call(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]: ...


class UrllibTransport:
    """POSTs form-encoded requests to ``https://api.telegram.org/bot<token>``."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        urlopen: UrlopenFn | None = None,
    ) -> None:
        if not token:
            raise ValueError("telegram token must not be empty")
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._urlopen: UrlopenFn = (
            urllib_request.urlopen if urlopen is None else urlopen
        )

    def call(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Invoke ``<base>/bot<token>/<method>`` and return the decoded payload."""
        url = f"{self._base_url}/bot{self._token}/{method}"
        body = urllib_parse.urlencode(params or {}).encode("utf-8")
        request = urllib_request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with self._urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib_error.HTTPError as exc:
            raise self._http_error(method, exc) from exc
        except urllib_error.URLError as exc:
            raise TelegramApiError(
                method, f"network error: {exc.reason}"
            ) from exc
        except OSError as exc:
            # Timeouts and reset connections surface as plain OSError.
            raise TelegramApiError(method, f"network error: {exc}") from exc
        except http.client.HTTPException as exc:
            # Wire-level protocol failures (IncompleteRead from a dropped
            # long-poll body, BadStatusLine from a garbled response, ...) are
            # not OSError subclasses and must not escape unnormalised either.
            raise TelegramApiError(
                method, f"protocol error: {type(exc).__name__}: {exc}"
            ) from exc

        return self._decode(method, raw)

    def _decode(self, method: str, raw: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise TelegramApiError(
                method, f"response is not valid JSON: {raw[:200]!r}"
            ) from exc
        if not isinstance(payload, dict):
            raise TelegramApiError(
                method,
                f"expected a JSON object, got {type(payload).__name__}",
            )
        if payload.get("ok") is not True:
            description = payload.get("description")
            raise TelegramApiError(
                method,
                str(description) if description else "request reported ok=false",
                error_code=_as_int(payload.get("error_code")),
            )
        return payload

    def _http_error(self, method: str, exc: urllib_error.HTTPError) -> TelegramApiError:
        raw = b""
        if getattr(exc, "fp", None) is not None:
            try:
                raw = exc.read()
            except (OSError, http.client.HTTPException) as read_exc:
                # Reading the error body can fail the same way the success
                # path does (dropped connection); fall back to the status line
                # rather than letting it escape this handler unnormalised.
                LOGGER.warning(
                    "could not read HTTP error body",
                    extra={
                        "event": "http_error_body_unreadable",
                        "method": method,
                        "error": f"{type(read_exc).__name__}: {read_exc}",
                    },
                )
        description = None
        error_code = None
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict) and payload.get("ok") is False:
            description = payload.get("description")
            error_code = _as_int(payload.get("error_code"))
        if not description:
            description = f"HTTP {exc.code} {exc.reason}"
        return TelegramApiError(
            method,
            str(description),
            error_code=error_code,
            status_code=exc.code,
        )


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
