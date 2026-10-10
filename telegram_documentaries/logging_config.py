"""Structured logging: JSON-lines records plus a lifecycle decorator.

The whole gateway logs structured events (one JSON object per line) so
failures are greppable and machine-readable. Cross-cutting start/success/
failure logging is applied through the ``log_lifecycle`` decorator rather
than scattered through business logic (see ``SPECS/TECH.md``).

No bare ``except`` and no swallowed exceptions: the decorator always
re-raises after logging.
"""

from __future__ import annotations

import functools
import json
import logging
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


class Skipped:
    """Sentinel for a deliberate, benign no-op from a decorated callable.

    Deliberately returned (instead of ``False``) when a callable intentionally
    does nothing — e.g. a Telegram update with no message, or an album
    follow-up already handled. ``log_lifecycle`` logs it at INFO as ``skipped``
    rather than WARNING as ``degraded``, so an expected no-op never raises a
    false operational warning. It is falsy so existing truthiness checks keep
    treating it as "nothing happened".
    """

    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "SKIPPED"


# The single shared instance; compare with ``is``.
SKIPPED = Skipped()

# Attributes every LogRecord already carries; anything else came from `extra=`.
_STANDARD_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    """Render a log record as a single JSON object.

    Core fields are ``ts``/``level``/``logger``/``message``; every structured
    field passed via ``extra=`` rides along; exception text (when present) is
    captured under ``exc``. Non-serialisable values fall back to ``str`` so a
    log line can never break the process.
    """

    def format(self, record: logging.LogRecord) -> str:
        created = datetime.fromtimestamp(record.created, tz=timezone.utc)
        payload: dict[str, object] = {
            "ts": created.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_ATTRS or key in payload:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _resolve_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    resolved = logging.getLevelNamesMapping().get(level.upper())
    if resolved is None:
        raise ValueError(f"unknown log level: {level!r}")
    return resolved


def configure_logging(level: str | int = "INFO") -> None:
    """Install the JSON formatter on the root logger (idempotent)."""
    global _CONFIGURED
    root = logging.getLogger()
    root.setLevel(_resolve_level(level))
    if not _CONFIGURED:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
        _CONFIGURED = True


def log_lifecycle(
    logger: logging.Logger, event: str
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator: log ``event`` start/success/skipped/degraded/failure.

    Failures are logged loudly with the traceback and **always re-raised** —
    the decorator never swallows anything. A callable that returns the boolean
    ``False`` handled its request in a degraded way (e.g. a reply could not be
    sent), so it is logged at WARNING as ``degraded`` rather than ``success``;
    that keeps a swallowed ``False`` from being obscured by a happy-path line.
    A callable that returns :data:`SKIPPED` deliberately did nothing (a benign
    skip), so it is logged at INFO as ``skipped`` instead. Only the exact
    boolean ``False`` counts as degraded — a falsy ``0`` (an offset or a
    count) is a normal success.
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            logger.debug(
                "lifecycle start",
                extra={"event": event, "stage": "start"},
            )
            try:
                result = func(*args, **kwargs)
            except Exception:
                logger.error(
                    "lifecycle failed",
                    extra={"event": event, "stage": "failure"},
                    exc_info=True,
                )
                raise
            if result is SKIPPED:
                logger.info(
                    "lifecycle skipped",
                    extra={"event": event, "stage": "skipped"},
                )
                return result
            if result is False:
                logger.warning(
                    "lifecycle degraded",
                    extra={"event": event, "stage": "degraded"},
                )
                return result
            logger.info(
                "lifecycle ok",
                extra={"event": event, "stage": "success"},
            )
            return result

        return wrapper

    return decorator
