"""CLI entrypoint: wire config → transport → session/gate → long-polling loop.

Kept thin on purpose: argument parsing, structured logging setup, secret
resolution, and normalising top-level failures into exit codes. The process
owns exactly one ``SessionStore`` and one ``VisionGate``, built here and
threaded into the poll loop.

Exit codes: ``0`` clean stop, ``1`` runtime/API failure (including a failed
startup ``getMe`` probe), ``2`` configuration problem or invalid command-line
argument, ``130`` deliberate Ctrl-C stop.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NoReturn

from telegram_documentaries.config import (
    DEFAULT_ENV_FILE,
    ConfigError,
    get_api_key,
    get_token,
    get_vision_model,
)
from telegram_documentaries.logging_config import configure_logging
from telegram_documentaries.polling import run_polling
from telegram_documentaries.session import SessionStore
from telegram_documentaries.transport import (
    TelegramApiError,
    Transport,
    UrllibTransport,
)
from telegram_documentaries.vision import GeminiVisionGate, VisionGate

LOGGER = logging.getLogger(__name__)

TransportFactory = Callable[[str], Transport]
# Builds the vision gate from the resolved API key and model id.
GateFactory = Callable[[str, str], VisionGate]


def _build_gate(api_key: str, model: str) -> VisionGate:
    """Default gate factory: a Gemini REST adapter for the resolved secrets."""
    return GeminiVisionGate(api_key, model=model)


def _probe_bot_username(transport: Transport) -> str | None:
    """Call ``getMe`` once and return the bot's username.

    Failing here surfaces a bad token immediately at startup instead of on the
    first ``getUpdates``; a payload without a username still counts as healthy.
    """
    payload = transport.call("getMe")
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    username = result.get("username")
    return username if isinstance(username, str) else None


def _stopped_for_keyboard_interrupt() -> int:
    """Log a clean stop for a deliberate Ctrl-C and return its exit code."""
    LOGGER.info(
        "gateway stopped",
        extra={"event": "gateway_stopped", "reason": "keyboard_interrupt"},
    )
    return 130


def positive_int(value: str) -> int:
    """Argparse type for counts that must be at least 1.

    ``--max-polls 0`` (or a negative value) would otherwise be accepted and
    then silently no-op the polling loop.
    """
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected an integer, got {value!r}"
        ) from None
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


class _CliParser(argparse.ArgumentParser):
    """Argument parser that JSON-logs usage errors before exiting 2."""

    def error(self, message: str) -> NoReturn:
        LOGGER.error(
            "invalid command-line arguments",
            extra={"event": "cli_usage_error", "error": message},
        )
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _CliParser(
        prog="telegram_documentaries",
        description=(
            "Phase 2 gateway: long-poll Telegram getUpdates and gate incoming "
            "portraits with the Gemini vision Bouncer."
        ),
    )
    parser.add_argument(
        "--max-polls",
        type=positive_int,
        default=None,
        metavar="N",
        help="stop after N getUpdates polls (default: poll forever)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="structured-log verbosity (default: INFO)",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: dict[str, str] | None = None,
    env_file: Path | None = DEFAULT_ENV_FILE,
    transport_factory: TransportFactory = UrllibTransport,
    gate_factory: GateFactory = _build_gate,
) -> int:
    """Run the gateway; return a process exit code instead of raising."""
    # Configure a sane level first so even an argparse usage error is emitted
    # as a structured JSON line (argparse exits before the level is parsed).
    configure_logging("INFO")
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)

    try:
        token = get_token(environ=environ, env_file=env_file)
        api_key = get_api_key(environ=environ, env_file=env_file)
    except ConfigError as exc:
        LOGGER.error(
            "configuration error",
            extra={"event": "config_error", "error": str(exc)},
            exc_info=True,
        )
        return 2

    transport = transport_factory(token)
    try:
        bot_username = _probe_bot_username(transport)
    except KeyboardInterrupt:
        return _stopped_for_keyboard_interrupt()
    except (TelegramApiError, OSError) as exc:
        LOGGER.error(
            "startup getMe probe failed; check TELEGRAM_BOT_TOKEN",
            extra={"event": "get_me_failed", "error": str(exc)},
            exc_info=True,
        )
        return 1

    session = SessionStore()
    gate = gate_factory(
        api_key, get_vision_model(environ=environ, env_file=env_file)
    )

    LOGGER.info(
        "gateway starting",
        extra={
            "event": "gateway_starting",
            "bot_username": bot_username,
            "max_polls": args.max_polls,
            "long_polling": True,
        },
    )
    try:
        final_offset = run_polling(
            transport,
            session=session,
            gate=gate,
            max_polls=args.max_polls,
        )
    except KeyboardInterrupt:
        return _stopped_for_keyboard_interrupt()
    except (TelegramApiError, OSError) as exc:
        LOGGER.error(
            "gateway stopped after a transport failure",
            extra={"event": "gateway_failed", "error": str(exc)},
            exc_info=True,
        )
        return 1

    LOGGER.info(
        "gateway stopped",
        extra={"event": "gateway_stopped", "offset": final_offset},
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - module execution path
    sys.exit(main())
