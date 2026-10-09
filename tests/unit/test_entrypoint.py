"""The ``python -m telegram_documentaries`` entrypoint must be wired up.

Running the module as a subprocess is the only way to exercise
``telegram_documentaries/__main__.py`` for real.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_module_help_exits_zero_without_a_token() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "telegram_documentaries", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "telegram_documentaries" in result.stdout
