import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "implementation"))

import jalali


def test_known_nowruz_dates():
    """Nowruz (Jalali New Year) dates are publicly documented facts -- verified anchors."""
    assert jalali.gregorian_to_jalali(2024, 3, 20) == (1403, 1, 1)
    assert jalali.gregorian_to_jalali(2025, 3, 21) == (1404, 1, 1)
    assert jalali.gregorian_to_jalali(2023, 3, 21) == (1402, 1, 1)


def test_known_historical_anchor_iranian_revolution_date():
    """1979-02-11 is widely documented as 22 Bahman 1357."""
    assert jalali.gregorian_to_jalali(1979, 2, 11) == (1357, 11, 22)


def test_year_boundary_just_before_nowruz():
    jy, jm, jd = jalali.gregorian_to_jalali(2025, 3, 20)
    assert (jy, jm) == (1403, 12)  # last day of Esfand 1403, not yet New Year


def test_format_jalali_date_produces_readable_string():
    out = jalali.format_jalali_date(date(2024, 3, 20))
    assert out == "1 Farvardin 1403"


def test_jalali_year_month_for_month_boundary_awareness():
    assert jalali.jalali_year_month_for(date(2024, 3, 19)) == (1402, 12)
    assert jalali.jalali_year_month_for(date(2024, 3, 20)) == (1403, 1)


def test_jalali_month_names_length_and_order():
    assert len(jalali.JALALI_MONTH_NAMES) == 12
    assert jalali.JALALI_MONTH_NAMES[0] == "Farvardin"
    assert jalali.JALALI_MONTH_NAMES[-1] == "Esfand"


def test_leap_year_esfand_has_30_days_sometimes():
    """Jalali leap years have a 30-day Esfand (month 12) instead of 29 -- 1403 is a
    documented Jalali leap year (its Esfand had 30 days, Nowruz 1404 landed on March 21
    rather than March 20)."""
    jy, jm, jd = jalali.gregorian_to_jalali(2025, 3, 20)
    assert (jy, jm, jd) == (1403, 12, 30)


def test_jalali_month_start_finds_first_day_of_current_month():
    from datetime import date
    # March 25, 2024 is 5 Farvardin 1403 (Nowruz was March 20 1403) -> month start = March 20
    assert jalali.jalali_month_start(date(2024, 3, 25)) == date(2024, 3, 20)


def test_jalali_month_start_handles_leap_year_boundary():
    from datetime import date
    # March 25, 2025 is within Esfand 1403 (the leap month, 30 days) -- month started Feb 19 2025
    result = jalali.jalali_month_start(date(2025, 3, 10))
    jy, jm, jd = jalali.gregorian_to_jalali(result.year, result.month, result.day)
    assert jd == 1


def test_jalali_month_start_on_the_first_day_itself_returns_same_date():
    from datetime import date
    assert jalali.jalali_month_start(date(2024, 3, 20)) == date(2024, 3, 20)
