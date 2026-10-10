"""Behaviour of the minimal .env loader and token resolution."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from telegram_documentaries.config import (
    ConfigError,
    get_api_key,
    get_interview_model,
    get_token,
    get_vision_model,
    parse_env_file,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


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

    assert values == {
        "TELEGRAM_BOT_TOKEN": "abc123",
        "GEMINI_API_KEY": "key-with-spaces",
    }


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


def test_get_api_key_prefers_process_environment_over_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=from-file\n", encoding="utf-8")

    key = get_api_key(
        environ={"GEMINI_API_KEY": "from-env"}, env_file=env_file
    )

    assert key == "from-env"


def test_get_api_key_falls_back_to_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('GEMINI_API_KEY="file-key"\n', encoding="utf-8")

    assert get_api_key(environ={}, env_file=env_file) == "file-key"


def test_get_api_key_strips_whitespace() -> None:
    key = get_api_key(
        environ={"GEMINI_API_KEY": "  spaced-key  "}, env_file=None
    )

    assert key == "spaced-key"


def test_get_api_key_raises_when_missing(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        get_api_key(environ={}, env_file=tmp_path / "absent.env")


def test_get_api_key_raises_on_blank_value(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        get_api_key(environ={"GEMINI_API_KEY": "   "}, env_file=env_file)


def test_get_vision_model_defaults_to_gemini_flash_lite() -> None:
    assert get_vision_model(environ={}, env_file=None) == "gemini-3.1-flash-lite"


def test_get_vision_model_env_override_wins(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_VISION_MODEL=from-file\n", encoding="utf-8")

    model = get_vision_model(
        environ={"GEMINI_VISION_MODEL": "from-env"}, env_file=env_file
    )

    assert model == "from-env"


def test_get_vision_model_falls_back_to_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_VISION_MODEL=file-model\n", encoding="utf-8")

    assert (
        get_vision_model(environ={}, env_file=env_file) == "file-model"
    )


def test_get_vision_model_blank_override_falls_back_to_default() -> None:
    model = get_vision_model(
        environ={"GEMINI_VISION_MODEL": "   "}, env_file=None
    )

    assert model == "gemini-3.1-flash-lite"


def test_default_vision_model_lives_in_a_neutral_module() -> None:
    # The constant must have a single neutral home so both config and the
    # adapter read the same value without either owning the other.
    from telegram_documentaries import defaults, vision

    assert (
        get_vision_model(environ={}, env_file=None)
        == defaults.DEFAULT_VISION_MODEL
    )
    assert vision.DEFAULT_VISION_MODEL is defaults.DEFAULT_VISION_MODEL


def test_get_interview_model_defaults_to_gemini_flash_lite() -> None:
    assert (
        get_interview_model(environ={}, env_file=None)
        == "gemini-3.1-flash-lite"
    )


def test_get_interview_model_env_override_wins(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_INTERVIEW_MODEL=from-file\n", encoding="utf-8")

    model = get_interview_model(
        environ={"GEMINI_INTERVIEW_MODEL": "from-env"}, env_file=env_file
    )

    assert model == "from-env"


def test_get_interview_model_falls_back_to_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_INTERVIEW_MODEL=file-model\n", encoding="utf-8")

    assert get_interview_model(environ={}, env_file=env_file) == "file-model"


def test_get_interview_model_blank_override_falls_back_to_default() -> None:
    model = get_interview_model(
        environ={"GEMINI_INTERVIEW_MODEL": "   "}, env_file=None
    )

    assert model == "gemini-3.1-flash-lite"


def test_default_interview_model_lives_in_a_neutral_module() -> None:
    from telegram_documentaries import defaults

    assert (
        get_interview_model(environ={}, env_file=None)
        == defaults.DEFAULT_INTERVIEW_MODEL
    )


def test_importing_config_does_not_pull_in_the_interview_adapter() -> None:
    # Same N3 guarantee as the vision adapter: resolving the interview model
    # must not drag the Gemini interviewer (and its urllib machinery) into
    # every config-only caller.
    code = (
        "import sys\n"
        "import telegram_documentaries.config\n"
        "assert 'telegram_documentaries.interviewer' not in sys.modules, "
        "'config must not import the interviewer adapter'\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_importing_config_does_not_pull_in_the_vision_adapter() -> None:
    # N3: resolving the model must not drag the whole HTTP/vendor adapter
    # surface (and its urllib machinery) into every config-only caller.
    code = (
        "import sys\n"
        "import telegram_documentaries.config\n"
        "assert 'telegram_documentaries.vision' not in sys.modules, "
        "'config must not import the vision adapter'\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
