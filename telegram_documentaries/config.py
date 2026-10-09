"""Minimal ``.env`` support for secrets (no third-party dotenv dependency).

Secrets live in ``.env`` only (see ``SPECS/MISSION.md`` — never hardcode or
commit tokens). Parsing is deliberately tiny: ``KEY=VALUE`` lines, optional
``export`` prefix, ``#`` comments, matching quotes stripped. The functions are
pure/injectable so tests never read the real ``.env``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

DEFAULT_ENV_FILE = Path(".env")

_TOKEN_KEY = "TELEGRAM_BOT_TOKEN"


class ConfigError(Exception):
    """Raised when required configuration is missing or unusable."""


def parse_env_file(text: str) -> dict[str, str]:
    """Parse ``.env``-style *text* into a mapping of string values.

    Blank lines, comment lines and lines without a ``=`` separator are
    ignored. Values keep any inner ``#``; only surrounding whitespace and a
    matching pair of quotes are stripped.
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_env_file(path: Path | None) -> dict[str, str]:
    """Read and parse ``path``.

    A missing file yields ``{}`` (nothing to load — the caller decides whether
    that is an error). An unreadable file is a hard ``ConfigError``: silently
    proceeding with no secrets would hide the real problem.
    """
    if path is None:
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError(f"cannot read env file {path}: {exc}") from exc
    return parse_env_file(text)


def get_token(
    *,
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = DEFAULT_ENV_FILE,
) -> str:
    """Resolve ``TELEGRAM_BOT_TOKEN``.

    The process environment wins over the ``.env`` file (12-factor
    precedence). Raises ``ConfigError`` when no non-empty token is available
    anywhere — callers must not fall back to a hardcoded value.
    """
    env = os.environ if environ is None else environ
    token = env.get(_TOKEN_KEY, "").strip()
    if token:
        return token

    token = load_env_file(env_file).get(_TOKEN_KEY, "").strip()
    if token:
        return token

    raise ConfigError(
        f"{_TOKEN_KEY} is not set; put it in {env_file or '.env'} "
        "(see .env.example) or export it in the environment"
    )
