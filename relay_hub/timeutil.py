"""Time, date and timezone helpers.

All stored timestamps are UTC ISO-8601 strings ending in "Z".
Business dates (joining date, bench start, due dates) are plain ISO dates
(YYYY-MM-DD) interpreted in the application timezone (default Asia/Kolkata).
"""
from __future__ import annotations

import calendar
import datetime as dt
import re
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Kolkata"
_tz_name = DEFAULT_TZ
# Tests can freeze "now" by setting this to an aware datetime.
_frozen_now: dt.datetime | None = None


def set_timezone(name: str) -> None:
    global _tz_name
    ZoneInfo(name)  # raises if unknown
    _tz_name = name


def tz() -> ZoneInfo:
    return ZoneInfo(_tz_name)


def tz_name() -> str:
    return _tz_name


def freeze(now: dt.datetime | None) -> None:
    """Freeze the clock (tests only)."""
    global _frozen_now
    _frozen_now = now


def now_utc() -> dt.datetime:
    if _frozen_now is not None:
        return _frozen_now.astimezone(dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc)


def now_local() -> dt.datetime:
    return now_utc().astimezone(tz())


def today() -> dt.date:
    return now_local().date()


def iso_utc(value: dt.datetime | None = None) -> str:
    value = value or now_utc()
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    v = value.replace("Z", "+00:00")
    d = dt.datetime.fromisoformat(v)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def local_datetime(day: dt.date, time_str: str) -> dt.datetime:
    hh, mm = [int(x) for x in (time_str or "10:00").split(":")[:2]]
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz())


def clamp_day(year: int, month: int, day: int) -> dt.date:
    """Monthly schedules: a day past the month end uses that month's last day."""
    last = calendar.monthrange(year, month)[1]
    return dt.date(year, month, min(max(day, 1), last))


MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})
MONTHS["sept"] = 9

_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T].*)?$")
_NUMERIC = re.compile(r"^(\d{1,4})[./-](\d{1,2})[./-](\d{1,4})$")
_TEXT_MONTH = re.compile(r"^(\d{1,2})[\s./-]*([A-Za-z]{3,9})[\s.,/-]*(\d{2,4})$")
_TEXT_MONTH_FIRST = re.compile(r"^([A-Za-z]{3,9})[\s./-]*(\d{1,2})[\s.,/-]+(\d{2,4})$")
EXCEL_EPOCH = dt.date(1899, 12, 30)


def _year(y: int) -> int:
    if y < 100:
        return 2000 + y if y < 50 else 1900 + y
    return y


def parse_date(value, day_first: bool = True) -> dt.date:
    """Parse a user-supplied date consistently.

    Accepted: datetime/date objects (from XLSX cells), Excel serial numbers,
    ISO dates (2026-10-01), numeric dates (01/10/2026, day-first by default
    because Indian HR sheets use DD/MM/YYYY), and month names (1-Oct-2026,
    Oct 1 2026). Raises ValueError with a readable message otherwise.
    """
    if value is None:
        raise ValueError("empty date")
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        n = float(value)
        if 1 <= n < 2958466:
            return EXCEL_EPOCH + dt.timedelta(days=int(n))
        raise ValueError(f"'{value}' is not a valid Excel date number")
    s = str(value).strip()
    if not s:
        raise ValueError("empty date")
    m = _ISO.match(s)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _NUMERIC.match(s)
    if m:
        a, b, c = m.groups()
        if len(a) == 4:  # YYYY/MM/DD
            return dt.date(int(a), int(b), int(c))
        a, b, c = int(a), int(b), _year(int(c))
        if day_first:
            return dt.date(c, b, a)
        return dt.date(c, a, b)
    m = _TEXT_MONTH.match(s)
    if m:
        d, mon, y = m.groups()
        if mon.lower() in MONTHS:
            return dt.date(_year(int(y)), MONTHS[mon.lower()], int(d))
    m = _TEXT_MONTH_FIRST.match(s)
    if m:
        mon, d, y = m.groups()
        if mon.lower() in MONTHS:
            return dt.date(_year(int(y)), MONTHS[mon.lower()], int(d))
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return parse_date(float(s), day_first)
    raise ValueError(f"'{s}' is not a recognised date (use YYYY-MM-DD or DD/MM/YYYY)")


def bench_days(assignment_status: str | None, bench_start: str | None, bench_end: str | None,
               on: dt.date | None = None) -> int | None:
    """Current bench days.

    Elapsed calendar days from the bench start date to today (Asia/Kolkata by
    default): 0 on the start date, 1 the next day, and so on. Only shown while
    the employee's assignment status is Bench, the start date is not in the
    future, and any bench end date has not passed. Otherwise None.
    """
    if assignment_status != "Bench" or not bench_start:
        return None
    on = on or today()
    start = dt.date.fromisoformat(bench_start)
    if start > on:
        return None
    if bench_end:
        end = dt.date.fromisoformat(bench_end)
        if end < on:
            return None
    return (on - start).days
