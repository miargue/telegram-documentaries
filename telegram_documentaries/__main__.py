"""``python -m telegram_documentaries`` → the gateway entrypoint."""

from __future__ import annotations

import sys

from telegram_documentaries.cli import main

if __name__ == "__main__":
    sys.exit(main())
