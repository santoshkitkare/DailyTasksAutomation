"""Contacts workbook parsing and validation (PRD section 43)."""

from __future__ import annotations

import io
from datetime import date

import pytest
from openpyxl import Workbook

from daily_ai_automation.contracts import EventType, Relationship
from daily_ai_automation.integrations.excel import (
    WorkbookError,
    parse_contacts,
)

HEADERS = [
    "EventType",
    "EventDate",
    "FullName",
    "EmailAddress",
    "MobileNumber",
    "Relationship",
]


def build_workbook(rows, *, headers=None, sheet_name="Contacts") -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    sheet.append(headers if headers is not None else HEADERS)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


GOOD_ROW = ["Birthday", date(1990, 9, 7), "Rahul Sharma", "rahul@example.com", "", "Friend"]


class TestHappyPath:
    def test_parses_the_prd_example(self):
        data = build_workbook(
            [
                ["Birthday", date(1990, 9, 7), "Rahul Sharma", "rahul@example.com"],
                ["Birthday", date(1988, 9, 8), "Priya Mehta", "priya@example.com"],
                ["Anniversary", date(2015, 9, 7), "Amit & Neha", "amit@example.com"],
            ]
        )
        result = parse_contacts(data, "Contacts")
        assert len(result.contacts) == 3
        assert result.invalid == []
        assert result.contacts[2].event_type is EventType.ANNIVERSARY

    def test_headers_are_matched_loosely(self):
        data = build_workbook(
            [["Birthday", date(1990, 9, 7), "Rahul", "rahul@example.com"]],
            headers=["event type", "Event_Date", "  FULL NAME ", "emailaddress"],
        )
        assert len(parse_contacts(data, "Contacts").contacts) == 1

    def test_relationship_is_optional_and_parsed(self):
        data = build_workbook(
            [
                [*GOOD_ROW[:5], "Close Friend"],
                [*GOOD_ROW[:3], "b@example.com", "", ""],
                [*GOOD_ROW[:3], "c@example.com", "", "colleague"],
                [*GOOD_ROW[:3], "d@example.com", "", "Sworn Nemesis"],
            ]
        )
        result = parse_contacts(data, "Contacts")
        relationships = [c.relationship for c in result.contacts]
        assert relationships == [
            Relationship.CLOSE_FRIEND,
            None,
            Relationship.COLLEAGUE,
            None,  # unrecognised falls back to the default tone, not an error
        ]

    def test_email_is_lowercased_and_blank_rows_skipped(self):
        data = build_workbook(
            [
                ["Birthday", date(1990, 9, 7), "Rahul", "Rahul@Example.COM"],
                [None, None, None, None],
            ]
        )
        result = parse_contacts(data, "Contacts")
        assert len(result.contacts) == 1
        assert result.contacts[0].email_address == "rahul@example.com"

    @pytest.mark.parametrize(
        ("written", "expected"),
        [
            ("1990-09-07", date(1990, 9, 7)),
            ("07-09-1990", date(1990, 9, 7)),
            ("07/09/1990", date(1990, 9, 7)),
            ("7 September 1990", date(1990, 9, 7)),
            ("7 Sep 1990", date(1990, 9, 7)),
            ("September 7, 1990", date(1990, 9, 7)),
        ],
    )
    def test_written_dates(self, written, expected):
        data = build_workbook([["Birthday", written, "Rahul", "r@example.com"]])
        assert parse_contacts(data, "Contacts").contacts[0].event_date == expected

    def test_a_date_without_a_year_uses_the_sentinel_year(self):
        data = build_workbook([["Birthday", "7 September", "Rahul", "r@example.com"]])
        parsed = parse_contacts(data, "Contacts").contacts[0]
        assert (parsed.event_date.month, parsed.event_date.day) == (9, 7)
        assert parsed.event_date.year == 1904

    def test_29_february_without_a_year_parses(self):
        """The sentinel year must be a leap year or this row is unreadable."""
        data = build_workbook([["Birthday", "29 February", "Leap", "l@example.com"]])
        result = parse_contacts(data, "Contacts")
        assert result.invalid == []
        assert (result.contacts[0].event_date.month,
                result.contacts[0].event_date.day) == (2, 29)


class TestRowValidation:
    @pytest.mark.parametrize(
        ("row", "fragment"),
        [
            (["Wedding", date(1990, 9, 7), "R", "r@example.com"], "not supported"),
            (["", date(1990, 9, 7), "R", "r@example.com"], "EventType is empty"),
            (["Birthday", "not a date", "R", "r@example.com"], "could not be read"),
            (["Birthday", None, "R", "r@example.com"], "EventDate is empty"),
            (["Birthday", date(1990, 9, 7), "", "r@example.com"], "FullName is empty"),
            (["Birthday", date(1990, 9, 7), "R", ""], "EmailAddress is empty"),
            (["Birthday", date(1990, 9, 7), "R", "not-an-email"], "not a valid address"),
            (["Birthday", date(1990, 9, 7), "R", "a@b"], "not a valid address"),
        ],
    )
    def test_bad_rows_are_reported_not_raised(self, row, fragment):
        data = build_workbook([GOOD_ROW, row])
        result = parse_contacts(data, "Contacts")
        assert len(result.contacts) == 1, "the good row must survive"
        assert len(result.invalid) == 1
        assert fragment in result.invalid[0].reason
        assert result.invalid[0].row_number == 3

    def test_plural_event_types_are_accepted(self):
        data = build_workbook([["Birthdays", date(1990, 9, 7), "R", "r@example.com"]])
        assert parse_contacts(data, "Contacts").contacts[0].event_type is EventType.BIRTHDAY

    def test_duplicate_identities_are_reported_and_dropped(self):
        data = build_workbook([GOOD_ROW, GOOD_ROW])
        result = parse_contacts(data, "Contacts")
        assert len(result.contacts) == 1
        assert len(result.duplicates) == 1
        assert "duplicate" in result.duplicates[0].lower()

    def test_the_prd_scenario_97_valid_of_100(self):
        """PRD section 43: 100 records, 97 valid, 2 bad emails, 1 missing type."""
        rows = [
            ["Birthday", date(1990, 1, 1), f"Person {i}", f"person{i}@example.com"]
            for i in range(97)
        ]
        rows.append(["Birthday", date(1990, 1, 1), "Bad One", "not-an-email"])
        rows.append(["Birthday", date(1990, 1, 1), "Bad Two", "also bad"])
        rows.append(["", date(1990, 1, 1), "No Type", "notype@example.com"])

        result = parse_contacts(build_workbook(rows), "Contacts")
        assert result.total_rows == 100
        assert len(result.contacts) == 97
        assert len(result.invalid) == 3
        assert result.summary() == "100 records, 97 valid, 3 invalid"


class TestWorkbookLevelFailures:
    def test_a_missing_worksheet_names_what_is_available(self):
        data = build_workbook([GOOD_ROW], sheet_name="People")
        with pytest.raises(WorkbookError, match="People"):
            parse_contacts(data, "Contacts")

    def test_missing_required_columns_are_named(self):
        data = build_workbook(
            [["Birthday", date(1990, 9, 7)]], headers=["EventType", "EventDate"]
        )
        with pytest.raises(WorkbookError) as exc:
            parse_contacts(data, "Contacts")
        assert "FullName" in str(exc.value)
        assert "EmailAddress" in str(exc.value)

    def test_an_empty_sheet_is_rejected(self):
        workbook = Workbook()
        workbook.active.title = "Contacts"
        buffer = io.BytesIO()
        workbook.save(buffer)
        with pytest.raises(WorkbookError):
            parse_contacts(buffer.getvalue(), "Contacts")
