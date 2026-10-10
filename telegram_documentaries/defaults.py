"""Shared default values.

This module is deliberately dependency-free: it is the neutral home for
constants that both configuration (``config.py``) and adapters (``vision.py``)
need, so neither has to import the other just to agree on a default.
"""

from __future__ import annotations

# Gemini model used by the Phase 2 Bouncer when no override is configured.
DEFAULT_VISION_MODEL = "gemini-3.1-flash-lite"

# Gemini model used by the Phase 3 Interviewer when no override is configured.
DEFAULT_INTERVIEW_MODEL = "gemini-3.1-flash-lite"

# Number of interview questions asked before the dossier is synthesised.
DEFAULT_QUESTION_COUNT = 5

# Longest user answer kept in the interview transcript.
MAX_ANSWER_LENGTH = 400
