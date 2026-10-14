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

# Gemini image model used by the Phase 4 Converter when no override is set.
DEFAULT_CONVERTER_MODEL = "gemini-3.1-flash-image"

# Gemini model used by the Phase 5 Scripter when no override is configured.
# The narration is a pure text call, so the cheap flash-lite tier is enough.
DEFAULT_SCRIPT_MODEL = "gemini-3.1-flash-lite"

# Gemini model used by the Phase 6 Narrator when no override is configured.
DEFAULT_NARRATOR_MODEL = "gemini-3.1-flash-tts-preview"

# Default narrator voice (deep, confident male voice, posh British accent,
# measured warm wildlife-documentary delivery).
DEFAULT_NARRATOR_VOICE = (
    "deep, confident male voice; posh British accent; measured, warm "
    "wildlife-documentary delivery"
)

# Narrator TTS call is slow, so it gets its own longer timeout.
DEFAULT_NARRATOR_TIMEOUT = 90.0

# Narration length contract: the Scripter prompts for at least
# ``MIN_SCRIPT_WORDS`` and hard-rejects anything over ``MAX_SCRIPT_WORDS``
# (the over-long output bug class). ``MIN`` is a prompting target only —
# never enforced; the adapter enforces ``MAX`` so every delivered script
# stays voice-friendly.
MIN_SCRIPT_WORDS = 60
MAX_SCRIPT_WORDS = 90

# The Converter's image call is slow: Flash Image routinely exceeds the 30 s
# shared default, so the adapter gets its own longer timeout.
DEFAULT_CONVERTER_TIMEOUT = 120.0

# Largest generated portrait the bot will upload (Telegram's practical photo
# cap). Checked before any ``sendPhoto`` so an oversized model output degrades
# to a retry instead of a rejected upload.
MAX_PHOTO_BYTES = 10 * 1024 * 1024

# Largest generated narration the bot will upload (Telegram's practical voice
# note cap). Checked before any ``sendVoice`` so an oversized model output
# degrades to a retry instead of a rejected upload.
MAX_NARRATION_BYTES = 5 * 1024 * 1024

# Number of interview questions asked before the dossier is synthesised.
DEFAULT_QUESTION_COUNT = 5

# Longest user answer kept in the interview transcript.
MAX_ANSWER_LENGTH = 400
