"""CLI entrypoint: wire config → transport → long-polling loop.

Kept thin on purpose: argument parsing, structured logging setup, token
resolution, and normalising top-level failures into exit codes.

Exit codes: ``0`` clean stop, ``1`` runtime/API failure, ``2`` configuration
problem (e.g. missing ``TELEGRAM_BOT_TOKEN``).
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from telegram_documentaries.config import DEFAULT_ENV_FILE, ConfigError, get_token
from telegram_documentaries.logging_config import configure_logging
from telegram_documentaries.polling import run_polling
from telegram_documentaries.transport import TelegramApiError, Transport, UrllibTransport

LOGGER = logging.getLogger(__name__)

TransportFactory = Callable[[str], Transport]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telegram_documentaries",
        description=(
            "Phase 1 gateway: long-poll Telegram getUpdates and reply to "
            "every incoming message with 'hey mate!'."
        ),
    )
    parser.add_argument(
        "--max-polls",
        type=int,
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
) -> int:
    """Run the gateway; return a process exit code instead of raising."""
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)

    try:
        token = get_token(environ=environ, env_file=env_file)
    except ConfigError as exc:
        LOGGER.error(
            "configuration error",
            extra={"event": "config_error", "error": str(exc)},
            exc_info=True,
        )
        return 2

    transport = transport_factory(token)
    LOGGER.info(
        "gateway starting",
        extra={
            "event": "gateway_starting",
            "max_polls": args.max_polls,
            "long_polling": True,
        },
    )
    try:
        final_offset = run_polling(transport, max_polls=args.max_polls)
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
