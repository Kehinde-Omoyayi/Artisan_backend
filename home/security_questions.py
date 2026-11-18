"""Shared, non-secret prompts and normalization for staff security answers."""
from __future__ import annotations

import unicodedata


SECURITY_QUESTION_OPTIONS = (
    ("first_school", "What was the name of your first school?"),
    ("first_job", "What was your first paid job?"),
    ("childhood_friend", "What was the first name of a childhood friend?"),
    ("favorite_teacher", "What was the surname of a favourite teacher?"),
    ("first_pet", "What was the name of your first pet?"),
    ("mother_birth_city", "In what city was your mother born?"),
    ("childhood_street", "What was the name of the street where you grew up?"),
    ("first_concert", "What was the first concert you attended?"),
    ("favorite_book", "What was the title of a favourite book?"),
    ("childhood_nickname", "What nickname did your family use for you as a child?"),
    ("first_teacher", "What was the first name of your primary-school teacher?"),
    ("first_travel_city", "Which city did you first travel to outside your home state?"),
)
SECURITY_QUESTION_LABELS = dict(SECURITY_QUESTION_OPTIONS)


def normalize_security_answer(value: str) -> str:
    """Normalize user-entered answers consistently without storing their plaintext."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    normalized = []
    for character in text:
        normalized.append(character if character.isalnum() else " ")
    return " ".join("".join(normalized).split())
