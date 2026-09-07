"""Annual recurrence matching (PRD section 16)."""

from __future__ import annotations

from datetime import date

import pytest

from daily_ai_automation.contracts import ContactRecord, EventType
from daily_ai_automation.occasion_dates import (
    UNKNOWN_YEAR,
    occurs_on,
    ordinal,
    todays_events,
    years_elapsed,
)


class TestOccursOn:
    def test_the_prd_example_matches(self):
        assert occurs_on(date(1990, 9, 7), date(2026, 9, 7))

    def test_the_year_is_ignored(self):
        assert occurs_on(date(1955, 9, 7), date(2026, 9, 7))
        assert occurs_on(date(2015, 9, 7), date(2026, 9, 7))

    def test_a_different_day_does_not_match(self):
        assert not occurs_on(date(1990, 9, 8), date(2026, 9, 7))

    def test_the_same_day_in_a_different_month_does_not_match(self):
        assert not occurs_on(date(1990, 8, 7), date(2026, 9, 7))

    def test_new_year_boundary(self):
        assert occurs_on(date(1990, 1, 1), date(2027, 1, 1))
        assert not occurs_on(date(1990, 12, 31), date(2027, 1, 1))


class TestLeapDay:
    def test_leap_day_matches_itself_in_a_leap_year(self):
        assert occurs_on(date(1992, 2, 29), date(2028, 2, 29))

    def test_leap_day_is_observed_on_28_february_in_a_common_year(self):
        assert occurs_on(date(1992, 2, 29), date(2026, 2, 28))

    def test_leap_day_is_not_observed_on_1_march(self):
        assert not occurs_on(date(1992, 2, 29), date(2026, 3, 1))

    def test_a_real_28_february_birthday_is_unaffected_in_a_leap_year(self):
        """28 February people must not be skipped just because 29 exists."""
        assert occurs_on(date(1990, 2, 28), date(2028, 2, 28))
        assert not occurs_on(date(1990, 2, 28), date(2028, 2, 29))

    def test_both_fire_on_28_february_in_a_common_year(self):
        assert occurs_on(date(1990, 2, 28), date(2026, 2, 28))
        assert occurs_on(date(1992, 2, 29), date(2026, 2, 28))


class TestYearsElapsed:
    def test_counts_completed_years(self):
        assert years_elapsed(date(2015, 9, 7), date(2026, 9, 7)) == 11

    def test_returns_none_when_the_year_is_unknown(self):
        assert years_elapsed(date(UNKNOWN_YEAR, 9, 7), date(2026, 9, 7)) is None

    def test_returns_none_for_the_same_year(self):
        assert years_elapsed(date(2026, 9, 7), date(2026, 9, 7)) is None

    def test_returns_none_for_a_future_year(self):
        """A typo'd future date must not produce 'Happy -2nd Anniversary'."""
        assert years_elapsed(date(2028, 9, 7), date(2026, 9, 7)) is None


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        (1, "1st"),
        (2, "2nd"),
        (3, "3rd"),
        (4, "4th"),
        (11, "11th"),
        (12, "12th"),
        (13, "13th"),
        (21, "21st"),
        (22, "22nd"),
        (25, "25th"),
        (50, "50th"),
        (101, "101st"),
        (111, "111th"),
    ],
)
def test_ordinal(number, expected):
    assert ordinal(number) == expected


def _contact(event_type: EventType, event_date: date, row: int = 2) -> ContactRecord:
    return ContactRecord(
        row_number=row,
        event_type=event_type,
        event_date=event_date,
        full_name="Test Person",
        email_address="test@example.com",
    )


class TestTodaysEvents:
    @pytest.fixture
    def contacts(self):
        return [
            _contact(EventType.BIRTHDAY, date(1990, 9, 7), 2),
            _contact(EventType.ANNIVERSARY, date(2015, 9, 7), 3),
            _contact(EventType.BIRTHDAY, date(1988, 9, 8), 4),
        ]

    def test_selects_only_today(self, contacts):
        due = todays_events(contacts, date(2026, 9, 7))
        assert {c.row_number for c in due} == {2, 3}

    def test_birthdays_can_be_disabled(self, contacts):
        due = todays_events(contacts, date(2026, 9, 7), birthday_enabled=False)
        assert {c.row_number for c in due} == {3}

    def test_anniversaries_can_be_disabled(self, contacts):
        due = todays_events(contacts, date(2026, 9, 7), anniversary_enabled=False)
        assert {c.row_number for c in due} == {2}

    def test_both_disabled_yields_nothing(self, contacts):
        assert (
            todays_events(
                contacts,
                date(2026, 9, 7),
                birthday_enabled=False,
                anniversary_enabled=False,
            )
            == []
        )
