"""Campaign calendar for station self-promotion (ANNOUNCER_OVERHAUL).

Pure functions over the injected wall clock and the station timezone. No
hardcoded dates (AGENTS rule 4): the Thanksgiving campaign lives in config.
The code — never the LLM — supplies day-level facts (revision 6), so a promo
can never invent a date or countdown.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pilgrim.config import Campaign

# Days-out phrasing, operator decision. Timing is optional: most promos just
# name the day. When a promo does mention timing, this is the phrase the code
# supplies and the copy uses verbatim.
DAYS_PHRASES: list[tuple[int, str]] = [
    (22, "coming up"),
    (15, "a few weeks away"),
    (8, "a couple weeks away"),
    (5, "about a week away"),
    (3, "a few days away"),
    (2, "a couple days away"),
    (1, "tomorrow"),
    (0, "Happy Thanksgiving"),
]

# Timing words an LLM must never write: they cannot be re-filled, and the
# tokens already carry the correct phrase (revision 5/6).
_BANNED_TIMING = re.compile(
    r"\b(?:tonight|tomorrow|today|next week|last week|"
    r"this (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"next (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b",
    re.IGNORECASE)


def local_date(now_wall: float, tz: str) -> date:
    return datetime.fromtimestamp(now_wall, ZoneInfo(tz)).date()


def days_until(now_wall: float, tz: str, campaign: Campaign) -> int:
    """Calendar days from the station-local 'today' to the campaign date."""
    return (date.fromisoformat(campaign.date) - local_date(now_wall, tz)).days


def phrase_for(days: int) -> str:
    """The colloquial timing phrase for a days-out count ('' when negative)."""
    if days < 0:
        return ""
    for threshold, phrase in DAYS_PHRASES:
        if days >= threshold:
            return phrase
    return ""


def target_per_hour(now_wall: float, tz: str, campaign: Campaign) -> float:
    """Promos/hour for the current station-local date (0 outside the ramp)."""
    today = local_date(now_wall, tz).isoformat()
    for step in campaign.schedule:
        if step.from_date <= today <= step.until:
            return step.per_hour
    return 0.0


def after_news(now_wall: float, tz: str, campaign: Campaign) -> bool:
    today = local_date(now_wall, tz).isoformat()
    for step in campaign.schedule:
        if step.from_date <= today <= step.until:
            return step.after_news
    return False


def active_campaign(now_wall: float, tz: str,
                    campaigns: list[Campaign]) -> Campaign | None:
    """The campaign with the highest target right now, or None."""
    best: Campaign | None = None
    best_t = 0.0
    for c in campaigns:
        t = target_per_hour(now_wall, tz, c)
        if t > best_t:
            best, best_t = c, t
    return best


def next_local_midnight(now_wall: float, tz: str) -> float:
    """Wall epoch of the next station-local midnight (day-level clip expiry)."""
    zone = ZoneInfo(tz)
    dt = datetime.fromtimestamp(now_wall, zone)
    tomorrow = (dt + timedelta(days=1)).date()
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day,
                    tzinfo=zone).timestamp()


def fill_day_tokens(text: str, now_wall: float, tz: str,
                    campaign: Campaign) -> str:
    """Replace the code-supplied day-level tokens. No LLM prose here."""
    days = days_until(now_wall, tz, campaign)
    out = text.replace("[[event_name]]", campaign.label)
    out = out.replace("[[days_until]]", phrase_for(days))
    out = out.replace("[[cta]]", campaign.cta)
    out = out.replace("[[event_date]]", campaign.date_label or campaign.label)
    return out


def validate_promo_template(raw: str, campaign: Campaign,
                            station_name: str) -> str | None:
    """Reject LLM copy that would air a wrong claim. Returns the reason, or
    None when the copy is acceptable.

    Checked on the RAW copy (before token fill): the station name and the
    event-name token must be present, and no timing word may be invented.
    """
    if station_name.lower() not in raw.lower():
        return "missing station name"
    if "[[event_name]]" not in raw and campaign.label.lower() not in raw.lower():
        return "missing event name token"
    m = _BANNED_TIMING.search(raw)
    if m:
        return f"invented timing word: {m.group(0)}"
    return None


def validate_promo_copy(text: str, campaign: Campaign,
                        station_name: str) -> str | None:
    """Post-fill check: the filled copy must actually name the station and the
    event. Returns the reason, or None."""
    norm = text.lower()
    if station_name.lower() not in norm:
        return "missing station name"
    if campaign.label.lower() not in norm:
        return "missing event name"
    return None
