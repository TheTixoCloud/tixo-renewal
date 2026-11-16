"""Gregorian <-> Jalali (Persian solar) calendar conversion, pure stdlib, no third-party
dependency. Implements the standard arithmetic algorithm used by common Persian-calendar
libraries (based on the 2820-year grand cycle approximation) -- a widely published, public
algorithm, not proprietary to any single implementation.

Used only for DISPLAY when the operator's configured dashboard timezone is Asia/Tehran (see
menu.py's format_local_time) -- all internal/stored timestamps remain UTC ISO-8601 Gregorian
always; this module never touches storage, only presentation.
"""
from __future__ import annotations

_G_DAYS_IN_MONTH = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
_J_DAYS_IN_MONTH = (31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29)


def _is_gregorian_leap(year: int) -> bool:
    return (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple:
    gy2 = gy - 1600
    gm2 = gm - 1
    gd2 = gd - 1

    g_day_no = 365 * gy2 + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
    for i in range(gm2):
        g_day_no += _G_DAYS_IN_MONTH[i]
    if gm2 > 1 and _is_gregorian_leap(gy):
        g_day_no += 1
    g_day_no += gd2

    j_day_no = g_day_no - 79
    j_np = j_day_no // 12053
    j_day_no %= 12053
    jy = 979 + 33 * j_np + 4 * (j_day_no // 1461)
    j_day_no %= 1461
    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365

    remaining = j_day_no
    jm, jd = 12, remaining + 1
    for i in range(11):
        if remaining < _J_DAYS_IN_MONTH[i]:
            jm, jd = i + 1, remaining + 1
            break
        remaining -= _J_DAYS_IN_MONTH[i]
    else:
        jd = remaining + 1
    return jy, jm, jd


JALALI_MONTH_NAMES = (
    "Farvardin", "Ordibehesht", "Khordad", "Tir", "Mordad", "Shahrivar",
    "Mehr", "Aban", "Azar", "Dey", "Bahman", "Esfand",
)


def format_jalali_date(dt) -> str:
    """`dt` is any object with .year/.month/.day (a datetime). Returns e.g.
    '15 Dey 1404'."""
    jy, jm, jd = gregorian_to_jalali(dt.year, dt.month, dt.day)
    return f"{jd} {JALALI_MONTH_NAMES[jm - 1]} {jy}"


def jalali_year_month_for(dt) -> tuple:
    """Returns (jalali_year, jalali_month) for month-boundary-aware aggregation (e.g.
    monthly traffic reports when Asia/Tehran + Jalali calendar is configured)."""
    jy, jm, _ = gregorian_to_jalali(dt.year, dt.month, dt.day)
    return jy, jm


def jalali_month_start(local_date):
    """Returns the Gregorian `date` (same type as the input) that is the FIRST day of the
    Jalali month containing `local_date`. Walks backward day-by-day (at most ~31 iterations,
    correctness over cleverness -- there is no commonly-published closed-form Jalali-to-
    Gregorian inverse used here, so this avoids implementing/trusting a separate, harder-to-
    verify inverse algorithm)."""
    from datetime import timedelta
    jy, jm, _ = gregorian_to_jalali(local_date.year, local_date.month, local_date.day)
    d = local_date
    while True:
        prev = d - timedelta(days=1)
        pjy, pjm, _ = gregorian_to_jalali(prev.year, prev.month, prev.day)
        if (pjy, pjm) != (jy, jm):
            return d
        d = prev
