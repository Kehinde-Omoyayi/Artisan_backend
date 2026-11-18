"""Shared validation for user-authored support case descriptions."""

import re


# Count letter-based words (including accented letters and common contractions),
# not punctuation, underscores, emoji, or numeric tokens.
_MEANINGFUL_WORD = re.compile(r"[^\W\d_]+(?:[’'-][^\W\d_]+)*", re.UNICODE)
MIN_SUPPORT_DESCRIPTION_WORDS = 6
SUPPORT_DESCRIPTION_MINIMUM_MESSAGE = "Description must contain at least six meaningful words."


def meaningful_word_count(value):
    if not isinstance(value, str):
        return 0
    return len(_MEANINGFUL_WORD.findall(value.strip()))


def normalize_support_description(value):
    description = str(value or "").strip()
    if meaningful_word_count(description) < MIN_SUPPORT_DESCRIPTION_WORDS:
        raise ValueError(SUPPORT_DESCRIPTION_MINIMUM_MESSAGE)
    return description
