"""Behaviour of the CLI entrypoint (`python -m telegram_documentaries`)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeTransport, text_update
from telegram_documentaries.cli import main
from telegram_documentaries.logging_config import JsonFormatter


def test_main_returns_config_error_when_token_missing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse_transport(token: str) -> Any:
        raise AssertionError(f"transport must not be built without a token: {token}")

    with caplog.at_level(logging.ERROR, logger="telegram_documentaries.cli"):
        exit_code = main(
            ["--max-polls", "1"],
            environ={},
            env_file=tmp_path / "absent.env",
            transport_factory=refuse_transport,
        )

    assert exit_code == 2
    assert any(record.levelno >= logging.ERROR for record in caplog.records)


def test_main_runs_the_echo_loop_with_the_configured_token(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=env-file-token\n", encoding="utf-8")
    transport = FakeTransport(batches=[[text_update(1, chat_id=5)], []])
    seen_tokens: list[str] = []

    def factory(token: str) -> FakeTransport:
        seen_tokens.append(token)
        return transport

    exit_code = main(
        ["--max-polls", "2"],
        environ={},
        env_file=env_file,
        transport_factory=factory,
    )

    assert exit_code == 0
    assert seen_tokens == ["env-file-token"]
    replies = transport.calls_for("sendMessage")
    assert [reply["text"] for reply in replies] == ["hey mate!"]
    assert len(transport.calls_for("getUpdates")) == 2


def test_main_reads_token_from_environment_first(tmp_path: Path) -> None:
    transport = FakeTransport(batches=[[]])
    seen_tokens: list[str] = []

    exit_code = main(
        ["--max-polls", "1"],
        environ={"TELEGRAM_BOT_TOKEN": "env-token"},
        env_file=tmp_path / "absent.env",
        transport_factory=lambda token: (seen_tokens.append(token) or transport),
    )

    assert exit_code == 0
    assert seen_tokens == ["env-token"]


def test_main_installs_structured_logging(tmp_path: Path) -> None:
    main(
        ["--max-polls", "1"],
        environ={"TELEGRAM_BOT_TOKEN": "t"},
        env_file=tmp_path / "absent.env",
        transport_factory=lambda token: FakeTransport(batches=[[]]),
    )

    root = logging.getLogger()
    assert any(
        isinstance(handler.formatter, JsonFormatter) for handler in root.handlers
    )


def test_main_rejects_unknown_log_level() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--log-level", "CHATTY"], environ={}, env_file=None)

    assert exc_info.value.code == 2


def test_main_logs_get_updates_failure_and_exits_non_zero(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from telegram_documentaries.transport import TelegramApiError

    transport = FakeTransport(
        fail_on={"getUpdates": TelegramApiError("getUpdates", "network down")}
    )

    with caplog.at_level(logging.ERROR, logger="telegram_documentaries.cli"):
        exit_code = main(
            ["--max-polls", "1"],
            environ={"TELEGRAM_BOT_TOKEN": "t"},
            env_file=tmp_path / "absent.env",
            transport_factory=lambda token: transport,
        )

    assert exit_code == 1
    assert any(record.levelno >= logging.ERROR for record in caplog.records)
