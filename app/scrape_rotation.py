"""Daily scrape-time rotation shared by the CI monitoring sweep.

The competitive-monitoring sweep used to run three times a day (morning /
afternoon / night) so results sampled different hours. That triples the Oxylabs
cost, so it now runs **once per day at a rotating time**: a single window runs each
day, cycling morning -> afternoon -> night across consecutive days. This keeps the
time-of-day variety while paying for one sweep per day.

The rotation is deterministic by calendar date (its ordinal), so every part of the
app agrees on which window is "today's" without storing state: the enqueuer gates
on it (the other two slots no-op) and the UI's next-run estimate reads from it.
Dates are resolved in Central time to match the slot wall-clock times.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

_CST = ZoneInfo("America/Chicago")

# The daily windows in order; the index aligns with ci_analysis.MONITORING_HOURS
# (morning=7 AM, afternoon=3 PM, night=11 PM CST).
SLOTS = ("morning", "afternoon", "night")


def _today(day: date | None) -> date:
    return day or datetime.now(_CST).date()


def active_index(day: date | None = None) -> int:
    """Index into :data:`SLOTS` of the window that runs on ``day`` (CST date)."""
    return _today(day).toordinal() % len(SLOTS)


def active_slot(day: date | None = None) -> str:
    """The one slot scheduled to run on ``day``; the others no-op that day."""
    return SLOTS[active_index(day)]


def is_active(slot: str, day: date | None = None) -> bool:
    """Whether ``slot`` is the window scheduled to run on ``day``."""
    return slot == active_slot(day)
