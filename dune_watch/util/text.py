"""Text normalisation shared by every adapter's keyword matching.

Venues are inconsistent about how they write the format, and a missed format keyword
means a missed alert. Observed in real Science Museum Bluesky posts within three
consecutive announcements of the same film:

    "...in magnificent IMAX 70mm are now on sale."
    "...in glorious IMAX 70 mm are now on sale."

A plain `"70mm" in text` check matches the first and silently drops the second.
"""
from __future__ import annotations

import re

_MM_SPACED = re.compile(r"(\d+)\s*mm\b", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")


def normalize_for_matching(text: str) -> str:
    """Lowercase, collapse whitespace, and join millimetre counts to their unit.

    Use this on both the haystack and the keywords so the comparison is symmetric.
    """
    if not text:
        return ""
    lowered = _WHITESPACE.sub(" ", text.lower())
    return _MM_SPACED.sub(r"\1mm", lowered)


def matches_any(haystack: str, keywords) -> bool:
    normalized = normalize_for_matching(haystack)
    return any(normalize_for_matching(k) in normalized for k in keywords if k)


def first_match(haystack: str, keywords):
    """The first keyword present, returned in its original spelling for display."""
    normalized = normalize_for_matching(haystack)
    for keyword in keywords:
        if keyword and normalize_for_matching(keyword) in normalized:
            return keyword
    return None
