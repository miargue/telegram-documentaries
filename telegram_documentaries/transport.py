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

Secret guarantee: no raised ``TelegramApiError`` — neither its message nor a
chained cause/context — ever contains the bot token. The token lives in the
request URL (``/bot<token>/...``), so every normalising branch redacts the
description through :meth:`UrllibTransport._redact` and raises with ``from
None`` to suppress the raw, token-bearing cause from a formatted traceback.
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

# ------------------------------------------------------------- outbound length
#
# Telegram rejects ``sendMessage`` text longer than 4096 *UTF-16 code units*
# (two units per astral character, one per BMP character — not Python
# ``len()``). Every outbound-message chokepoint bounds text through these
# helpers, so the whole "over-long outbound text" bug class is guarded at the
# mechanism level rather than patched per call site.

MAX_MESSAGE_LENGTH = 4096


def message_length(text: str) -> int:
    """Count Telegram message characters as UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def truncate_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> str:
    """Trim ``text`` to at most ``limit`` UTF-16 code units, whole pairs only.

    Text already at or under the limit is returned unchanged. The cut never
    splits a surrogate pair: slicing ``limit * 2`` UTF-16-le bytes always lands
    on a code-unit boundary, and any lone surrogate left at the end is dropped,
    so the result always re-encodes cleanly. A zero or negative limit yields
    the empty string.
    """
    if limit <= 0:
        return ""
    encoded = text.encode("utf-16-le")
    if len(encoded) <= limit * 2:
        return text
    cut = encoded[: limit * 2]
    truncated = cut.decode("utf-16-le", errors="surrogatepass")
    if truncated and 0xD800 <= ord(truncated[-1]) <= 0xDFFF:
        truncated = truncated[:-1]
    return truncated


# Injectable seam for tests: same call signature as urllib.request.urlopen.
UrlopenFn = Callable[..., Any]


class TelegramApiError(Exception):
    """A failed call to the Telegram Bot API (network, HTTP or ``ok: false``).

    Guarantee: the message and any chained cause never contain the bot token
    (see :class:`UrllibTransport`).
    """

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

    def download(self, file_path: str) -> bytes: ...


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
        raw = self._read(method, request)
        return self._decode(method, raw)

    def download(self, file_path: str) -> bytes:
        """GET raw file bytes from ``<base>/file/bot<token>/<file_path>``.

        ``file_path`` must already be validated as a safe relative path by the
        caller (``media.fetch_photo``) — this method does not build it from
        untrusted input itself.

        Like :meth:`call`, a failure never leaks the bot token: the description
        is redacted and the raw cause is suppressed.
        """
        url = f"{self._base_url}/file/bot{self._token}/{file_path}"
        request = urllib_request.Request(url, method="GET")
        return self._read("download", request)

    def _read(self, method: str, request: urllib_request.Request) -> bytes:
        """Run a request and normalise every failure into ``TelegramApiError``.

        Every branch redacts the description and raises ``from None``: the raw
        exception can embed the token-bearing URL (e.g. a real path-validation
        ``http.client.InvalidURL``), and ``from None`` keeps it out of any
        formatted traceback. The original exception type is kept in the message
        for debugging.
        """
        try:
            with self._urlopen(request, timeout=self._timeout) as response:
                return response.read()
        except urllib_error.HTTPError as exc:
            raise self._http_error(method, exc) from None
        except urllib_error.URLError as exc:
            raise TelegramApiError(
                method,
                self._redact(
                    f"network error: {type(exc).__name__}: {exc.reason}"
                ),
            ) from None
        except OSError as exc:
            # Timeouts and reset connections surface as plain OSError.
            raise TelegramApiError(
                method,
                self._redact(f"network error: {type(exc).__name__}: {exc}"),
            ) from None
        except http.client.HTTPException as exc:
            # Wire-level protocol failures (IncompleteRead from a dropped
            # long-poll body, BadStatusLine from a garbled response, ...) are
            # not OSError subclasses and must not escape unnormalised either.
            raise TelegramApiError(
                method,
                self._redact(
                    f"protocol error: {type(exc).__name__}: {exc}"
                ),
            ) from None
        except Exception as exc:
            # Last-resort normalisation: e.g. a malformed URL raises a plain
            # ValueError whose message embeds the token-bearing URL. Redact it
            # and suppress the cause so the raw value cannot leak through the
            # formatted traceback.
            raise TelegramApiError(
                method,
                self._redact(f"unexpected error: {type(exc).__name__}: {exc}"),
            ) from None

    def _redact(self, text: str) -> str:
        if not self._token:
            return text
        redacted = text.replace(self._token, "***")
        # A control character *inside* the token makes urlopen's InvalidURL
        # repr-escape the selector, so the raw secret no longer appears
        # verbatim; strip its escaped form too.
        escaped = self._token.encode("unicode_escape").decode("ascii")
        if escaped != self._token:
            redacted = redacted.replace(escaped, "***")
        return redacted

    def _decode(self, method: str, raw: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise TelegramApiError(
                method,
                self._redact(
                    f"response is not valid JSON: {raw[:200]!r}"
                ),
            ) from None
        if not isinstance(payload, dict):
            raise TelegramApiError(
                method,
                f"expected a JSON object, got {type(payload).__name__}",
            )
        if payload.get("ok") is not True:
            description = payload.get("description")
            raise TelegramApiError(
                method,
                self._redact(
                    str(description)
                    if description
                    else "request reported ok=false"
                ),
                error_code=_as_int(payload.get("error_code")),
            )
        return payload

    def _http_error(self, method: str, exc: urllib_error.HTTPError) -> TelegramApiError:
        """Normalise an ``HTTPError``; the description never leaks the token.

        Either the decoded Telegram ``description`` or the HTTP status line can
        embed the token-bearing URL, so the final description is redacted.
        """
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
                        "error": self._redact(
                            f"{type(read_exc).__name__}: {read_exc}"
                        ),
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
            self._redact(str(description)),
            error_code=error_code,
            status_code=exc.code,
        )


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
