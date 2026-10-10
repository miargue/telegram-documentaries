"""Shared default values.

This module is deliberately dependency-free: it is the neutral home for
constants that both configuration (``config.py``) and adapters (``vision.py``)
need, so neither has to import the other just to agree on a default.
"""

from __future__ import annotations

# Gemini model used by the Phase 2 Bouncer when no override is configured.
DEFAULT_VISION_MODEL = "gemini-3.1-flash-lite"
