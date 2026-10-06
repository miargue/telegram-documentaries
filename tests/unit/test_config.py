"""Behaviour of the minimal .env loader and token resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from telegram_documentaries.config import ConfigError, get_token, parse_env_file


def test_parse_env_file_reads_simple_assignments() -> None:
    values = parse_env_file("FOO=bar\nBAZ=qux\n")

    assert values == {"FOO": "bar", "BAZ": "qux"}


def test_parse_env_file_ignores_blank_lines_comments_and_export_prefix() -> None:
    values = parse_env_file(
        "# a comment\n"
        "\n"
        "   \n"
        "export TELEGRAM_BOT_TOKEN=abc123\n"
        "GEMINI_API_KEY = key-with-spaces \n"
    )

    assert values == {"TELEGRAM_BOT_TOKEN": "abc123", "GEMINI_API_KEY": "key-with-spaces"}


def test_parse_env_file_strips_matching_quotes() -> None:
    values = parse_env_file(
        'DOUBLE="hello world"\n'
        "SINGLE='hello world'\n"
        'HASHED="value # not a comment"\n'
    )

    assert values == {
        "DOUBLE": "hello world",
        "SINGLE": "hello world",
        "HASHED": "value # not a comment",
    }


def test_parse_env_file_skips_lines_without_separator() -> None:
    values = parse_env_file("JUST_A_KEY\nFOO=bar\n")

    assert values == {"FOO": "bar"}


def test_parse_env_file_keeps_empty_value() -> None:
    assert parse_env_file("EMPTY=\n") == {"EMPTY": ""}


def test_get_token_prefers_process_environment_over_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=from-file\n", encoding="utf-8")

    token = get_token(
        environ={"TELEGRAM_BOT_TOKEN": "from-env"},
        env_file=env_file,
    )

    assert token == "from-env"


def test_get_token_falls_back_to_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('TELEGRAM_BOT_TOKEN="file-token"\n', encoding="utf-8")

    assert get_token(environ={}, env_file=env_file) == "file-token"


def test_get_token_strips_whitespace_from_environment_value() -> None:
    token = get_token(
        environ={"TELEGRAM_BOT_TOKEN": "  spaced-token  "}, env_file=None
    )

    assert token == "spaced-token"


def test_get_token_raises_when_nothing_configured(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        get_token(environ={}, env_file=tmp_path / "does-not-exist.env")


def test_get_token_raises_on_empty_token_value(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        get_token(environ={"TELEGRAM_BOT_TOKEN": ""}, env_file=env_file)


def test_get_token_missing_env_file_with_no_env_var_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        get_token(environ={}, env_file=tmp_path / ".env")
