"""Generate a contacts workbook for a safe first test.

Every row is dated *today* and addressed to you, so the first live run greets
nobody but yourself. Upload the result to Google Drive and put its file ID in
config.yaml.

    python scripts/make_test_contacts.py you@gmail.com
    python scripts/make_test_contacts.py you@gmail.com --name "Santosh" --out test_contacts.xlsx

Add --include-edge-cases to append rows that should be reported as invalid, so
you can confirm the validator reports them without derailing the run.
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Font

HEADERS = [
    "EventType",
    "EventDate",
    "FullName",
    "EmailAddress",
    "MobileNumber",
    "Relationship",
]


def build(email: str, name: str, today: date, *, edge_cases: bool) -> Workbook:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Contacts"

    sheet.append(HEADERS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)

    # Fires today. A birth year 30 years back keeps the data realistic.
    sheet.append(
        [
            "Birthday",
            date(today.year - 30, today.month, today.day),
            name,
            email,
            "",
            "Friend",
        ]
    )
    # Fires today, and exercises the anniversary year-count phrasing.
    sheet.append(
        [
            "Anniversary",
            date(today.year - 11, today.month, today.day),
            f"{name} (anniversary test)",
            email,
            "",
            "Family",
        ]
    )
    # Must NOT fire: proves the date filter works. It needs a distinct address,
    # because contacts are de-duplicated on (event_type, email) - a second
    # Birthday row for the same address is dropped as a duplicate before the
    # date filter ever sees it. A +tag alias still delivers to the same inbox.
    local, _, domain = email.partition("@")
    sheet.append(
        [
            "Birthday",
            date(1988, 1, 1),
            "Should Not Fire (wrong date)",
            f"{local}+nofire@{domain}",
            "",
            "Colleague",
        ]
    )

    if edge_cases:
        # Each of these should appear in the report as an invalid row while the
        # valid rows above still send.
        sheet.append(["Birthday", today, "Bad Email", "not-an-email", "", ""])
        sheet.append(["Wedding", today, "Bad Event Type", email, "", ""])
        sheet.append(["Birthday", "not a date", "Bad Date", email, "", ""])
        sheet.append(["Birthday", today, "", email, "", ""])

    for column, width in zip("ABCDEF", (14, 14, 28, 30, 16, 14)):
        sheet.column_dimensions[column].width = width

    return workbook


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email", help="Your own address - every row is sent here.")
    parser.add_argument("--name", default="Test Recipient", help="Recipient name.")
    parser.add_argument("--out", default="test_contacts.xlsx", help="Output path.")
    parser.add_argument(
        "--timezone",
        default="Asia/Kolkata",
        help="Timezone that decides what 'today' means. Match config.yaml.",
    )
    parser.add_argument(
        "--include-edge-cases",
        action="store_true",
        help="Append rows that should be reported as invalid.",
    )
    args = parser.parse_args()

    if "@" not in args.email:
        parser.error(f"{args.email!r} does not look like an email address")

    today = datetime.now(ZoneInfo(args.timezone)).date()
    workbook = build(
        args.email, args.name, today, edge_cases=args.include_edge_cases
    )

    out = Path(args.out)
    workbook.save(out)

    print(f"Wrote {out.resolve()}")
    print(f"Today in {args.timezone} is {today.isoformat()}.")
    print("Rows that will fire today: 1 birthday, 1 anniversary (11 years).")
    print(f"Both are addressed to {args.email}.")
    if args.include_edge_cases:
        print("Plus 4 deliberately invalid rows that should be reported, not sent.")
    print()
    print("Next: upload this file to Google Drive, open it, and copy the ID from")
    print("the URL - the part between /d/ and /edit - into occasion.drive_file_id.")


if __name__ == "__main__":
    main()
