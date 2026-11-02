"""Campaign calendar (ANNOUNCER_OVERHAUL): pure, config-driven, no hardcoded
dates. Thanksgiving 2026-11-26 is the operator's campaign."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from pilgrim.config import Campaign, CampaignStep
from pilgrim.promotions import (
    active_campaign,
    after_news,
    days_until,
    fill_day_tokens,
    next_local_midnight,
    phrase_for,
    target_per_hour,
    validate_promo_copy,
    validate_promo_template,
)

TZ = "America/Detroit"


def wall_at(y, m, d, hh=12, mm=0) -> float:
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(TZ)).timestamp()


def camp() -> Campaign:
    return Campaign(
        id="thanksgiving-2026",
        label="Thanksgiving",
        date="2026-11-26",
        cta="the request line",
        lines=["a song for the table"],
        schedule=[
            CampaignStep.model_validate(
                {"from": "2026-11-05", "until": "2026-11-18", "per_hour": 1}),
            CampaignStep.model_validate(
                {"from": "2026-11-19", "until": "2026-11-25", "per_hour": 2}),
            CampaignStep.model_validate(
                {"from": "2026-11-26", "until": "2026-11-26",
                 "per_hour": 3, "after_news": True}),
        ],
    )


def test_days_until_uses_station_local_date():
    c = camp()
    assert days_until(wall_at(2026, 11, 26, 0, 1), TZ, c) == 0
    assert days_until(wall_at(2026, 11, 25, 23, 59), TZ, c) == 1
    assert days_until(wall_at(2026, 11, 1), TZ, c) == 25


def test_phrase_table():
    assert phrase_for(25) == "coming up"
    assert phrase_for(21) == "a few weeks away"
    assert phrase_for(14) == "a couple weeks away"
    assert phrase_for(7) == "about a week away"
    assert phrase_for(4) == "a few days away"
    assert phrase_for(2) == "a couple days away"
    assert phrase_for(1) == "tomorrow"
    assert phrase_for(0) == "Happy Thanksgiving"
    assert phrase_for(-1) == ""


def test_ramp_and_after_news():
    c = camp()
    assert target_per_hour(wall_at(2026, 11, 1), TZ, c) == 0
    assert target_per_hour(wall_at(2026, 11, 5), TZ, c) == 1
    assert target_per_hour(wall_at(2026, 11, 19), TZ, c) == 2
    assert target_per_hour(wall_at(2026, 11, 26), TZ, c) == 3
    assert target_per_hour(wall_at(2026, 11, 27), TZ, c) == 0
    assert after_news(wall_at(2026, 11, 26), TZ, c) is True
    assert after_news(wall_at(2026, 11, 20), TZ, c) is False


def test_active_campaign_is_none_before_the_ramp():
    c = camp()
    assert active_campaign(wall_at(2026, 11, 1), TZ, [c]) is None
    assert active_campaign(wall_at(2026, 11, 20), TZ, [c]) is c


def test_next_local_midnight():
    now = wall_at(2026, 11, 20, 23, 30)
    nxt = datetime.fromtimestamp(next_local_midnight(now, TZ), ZoneInfo(TZ))
    assert nxt.date() == date(2026, 11, 21)
    assert (nxt.hour, nxt.minute, nxt.second) == (0, 0, 0)


def test_fill_day_tokens_and_no_spoken_date_by_default():
    c = camp()
    now = wall_at(2026, 11, 20)
    out = fill_day_tokens("[[event_name]] is [[days_until]] at [[cta]].", now, TZ, c)
    assert out == "Thanksgiving is about a week away at the request line."
    # date_label empty -> [[event_date]] falls back to the label (never spoken)
    assert "the twenty-sixth" not in fill_day_tokens("[[event_date]]", now, TZ, c)


def test_validate_rejects_invented_timing_and_missing_tokens():
    c = camp()
    ok = "Pilgrim Dot Farm is pandering about [[event_name]] [[days_until]]."
    assert validate_promo_template(ok, c, "Pilgrim Dot Farm", 25) is None
    # 25 days out: the copy must say it is coming, not that it is today
    assert validate_promo_template(
        "Pilgrim Dot Farm says [[event_name]] is here.", c, "Pilgrim Dot Farm",
        25) is not None
    assert validate_promo_template(
        "Pilgrim Dot Farm. It's Thanksgiving! [[event_name]]", c,
        "Pilgrim Dot Farm", 25) is not None
    # on the day, "Happy Thanksgiving" is fine and [[days_until]] is optional
    assert validate_promo_template(
        "Happy Thanksgiving from Pilgrim Dot Farm!", c, "Pilgrim Dot Farm",
        0) is None
    assert validate_promo_template("Come to the farm tonight!", c,
                                   "Pilgrim Dot Farm") is not None
    assert validate_promo_template("Thanksgiving at the farm.", c,
                                   "Pilgrim Dot Farm") is not None  # no token
    assert validate_promo_template("[[event_name]] at the farm.", c,
                                   "Pilgrim Dot Farm") is not None  # no station
    filled = "Pilgrim Dot Farm: Thanksgiving tomorrow."
    assert validate_promo_copy(filled, c, "Pilgrim Dot Farm") is None
    assert validate_promo_copy("Thistledown says hi.", c,
                               "Pilgrim Dot Farm") is not None
