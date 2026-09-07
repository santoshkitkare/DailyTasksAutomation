"""Annual recurrence matching (PRD section 16).

Anniversary dates recur on month and day; the stored year is history, not part
of the match. The only genuinely awkward case is 29 February, which does not
exist in three years out of four.
"""

from __future__ import annotations

import calendar
from datetime import date

from .contracts import ContactRecord

#: Sentinel year used by the parser for dates written without one.
UNKNOWN_YEAR = 1904


def is_leap(year: int) -> bool:
    return calendar.isleap(year)


def occurs_on(event_date: date, today: date) -> bool:
    """Does this annual event fall on ``today``?

    29 February events are observed on 28 February in non-leap years. Observing
    them on 1 March instead would push the greeting into the following month,
    and skipping them entirely would mean a leap-day contact hears from us once
    every four years.
    """
    if event_date.month == 2 and event_date.day == 29 and not is_leap(today.year):
        return today.month == 2 and today.day == 28

    return (event_date.month, event_date.day) == (today.month, today.day)


def years_elapsed(event_date: date, today: date) -> int | None:
    """Completed years since the event, or None when the year is unknown.

    Used for phrasing such as "Happy 11th Anniversary". Returns None rather
    than a wrong number when the source row carried no year, and also when the
    stored year is in the future, which indicates a data-entry error.
    """
    if event_date.year == UNKNOWN_YEAR:
        return None
    years = today.year - event_date.year
    if years <= 0:
        return None
    return years


def ordinal(number: int) -> str:
    """1 -> '1st', 2 -> '2nd', 11 -> '11th'."""
    if 10 <= number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def todays_events(
    contacts: list[ContactRecord],
    today: date,
    *,
    birthday_enabled: bool = True,
    anniversary_enabled: bool = True,
) -> list[ContactRecord]:
    """Filter the contact list down to the events happening today."""
    enabled = {
        "Birthday": birthday_enabled,
        "Anniversary": anniversary_enabled,
    }
    return [
        contact
        for contact in contacts
        if enabled.get(str(contact.event_type), False)
        and occurs_on(contact.event_date, today)
    ]
