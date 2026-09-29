"""Deterministic pre-filter for the listener request line (OVERHAUL 4.1).

Runs BEFORE the LLM and costs nothing. URL/contact-info attempts are rejected
without spending a moderation call, and the request text is never shared further
than the store.
"""
from __future__ import annotations

import re

REASON = "No links or contact info on the request line — just tell us what you want to hear."

# Bare domains: word.tld where tld is a known/very short suffix
_TLDS = "com|net|org|io|co|us|gg|tv|me|ly|app|dev|xyz|info|biz|link|site|online|[a-z]{2}"

_HTTP = re.compile(r"https?://", re.IGNORECASE)
_BARE_DOMAIN = re.compile(rf"\b[a-z0-9-]+\.({_TLDS})\b", re.IGNORECASE)
_SPELLED_DOMAIN = re.compile(
    r"\b\w+\s*(?:\(|\[)?\s*dot\s*(?:\)|\])?\s*(?:com|net|org|io)\b", re.IGNORECASE)
_EMAIL = re.compile(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", re.IGNORECASE)
_HANDLE = re.compile(r"(?<!\w)@\w{2,}")

_DIGITS_STRIP = re.compile(r"[\s.\-\[\]()]")


def prefilter(text: str) -> str | None:
    """Return the listener-facing rejection reason, or None if the text is clean."""
    low = text.lower()
    if _HTTP.search(low) or "www." in low:
        return REASON
    if _BARE_DOMAIN.search(low):
        return REASON
    if _SPELLED_DOMAIN.search(low):
        return REASON
    if _EMAIL.search(low):
        return REASON
    # phone numbers: 7+ digits after removing separators and brackets
    digits = re.sub(r"\D", "", _DIGITS_STRIP.sub("", text))
    if len(digits) >= 7:
        return REASON
    if _HANDLE.search(text):
        return REASON
    return None
