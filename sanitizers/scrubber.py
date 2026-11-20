"""Regex scrub of email and phone in free text (invariant 4).

Known limit: misses obfuscated forms ("a at b dot com", digits spelled out or spaced oddly).
A phone is 7-15 digits with optional separators, so 4-6 digit order ids survive.
"""
from __future__ import annotations

import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
PHONE_RE = re.compile(r"(?<![\w])\+?\d[\d\s().\-]{5,}\d(?![\w])")

EMAIL_PLACEHOLDER = "[email removed]"
PHONE_PLACEHOLDER = "[phone removed]"


DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _is_phone(candidate: str) -> bool:
    digits = sum(c.isdigit() for c in DATE_RE.sub("", candidate))  # "2026-11-01" is a date, not a phone
    return 7 <= digits <= 15


def scrub(text: str) -> str:
    text = EMAIL_RE.sub(EMAIL_PLACEHOLDER, text)
    return PHONE_RE.sub(lambda m: PHONE_PLACEHOLDER if _is_phone(m.group(0)) else m.group(0), text)


def contains_pii(text: str) -> bool:
    """True if text holds an email or phone. Used by the pre-model guard on user-typed text."""
    return bool(EMAIL_RE.search(text)) or any(_is_phone(m.group(0)) for m in PHONE_RE.finditer(text))
