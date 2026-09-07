"""Consolidated daily report (PRD section 33).

Renders one RunReport into plain text (for the console and the text/plain part)
and HTML (for the email and the on-disk copy). Both are built from the same
data so they can never disagree.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from ..contracts import AgentResult
from ..supervisor.state_manager import TaskState
from ..supervisor.supervisor import RunReport

GMAIL_AGENT = "gmail_triage_agent"
OCCASION_AGENT = "occasion_agent"

PRIORITY_MARKERS = {
    "CRITICAL": "\U0001f6a8",
    "HIGH": "\U0001f534",
    "MEDIUM": "\U0001f7e1",
    "LOW": "⚪",
    "NONE": "⚪",
}

STATUS_COLOURS = {
    "COMPLETED": "#1a7f37",
    "PARTIAL_SUCCESS": "#9a6700",
    "FAILED": "#cf222e",
}


@dataclass
class RenderedReport:
    subject: str
    text: str
    html: str


def render(report: RunReport) -> RenderedReport:
    mode = " [DRY RUN]" if report.dry_run else ""
    subject = (
        f"Daily Automation Report - {report.run_date} - "
        f"{report.status.replace('_', ' ').title()}{mode}"
    )
    return RenderedReport(
        subject=subject,
        text=_render_text(report),
        html=_render_html(report),
    )


# --------------------------------------------------------------------------
# Plain text
# --------------------------------------------------------------------------


def _render_text(report: RunReport) -> str:
    lines = [
        "Daily Automation Report",
        report.run_date,
        "",
        f"Overall Status: {report.status.replace('_', ' ')}",
    ]
    if report.dry_run:
        lines.append("Mode: DRY RUN - nothing was written to Gmail and no mail was sent.")
    lines.append("")

    gmail = report.results.get(GMAIL_AGENT)
    lines.extend(_gmail_text(gmail, report))
    occasion = report.results.get(OCCASION_AGENT)
    lines.extend(_occasion_text(occasion, report))
    lines.extend(_actions_text(gmail))
    lines.extend(_errors_text(report))
    return "\n".join(lines)


def _gmail_text(result: AgentResult | None, report: RunReport) -> list[str]:
    lines = ["Gmail", "-" * 6]
    if result is None:
        lines.append(_absent(GMAIL_AGENT, report))
        return lines + [""]

    counts = result.details.get("counts", {})
    verb = "would be labeled" if report.dry_run else "labeled"
    labeled = counts.get("would_label" if report.dry_run else "labeled", 0)
    lines += [
        f"Status: {result.status}",
        f"New emails processed: {counts.get('processed', result.items_processed)}",
        f"Job/recruiter emails: {counts.get('job_opportunities', 0)}",
        f"Needing a reply: {counts.get('requires_reply', 0)}",
        f"High priority: {counts.get('high_priority', 0)}",
        f"Emails {verb} ToDelete: {labeled}",
    ]
    if counts.get("injection_attempts"):
        lines.append(
            f"Prompt-injection attempts detected: {counts['injection_attempts']}"
        )
    return lines + [""]


def _occasion_text(result: AgentResult | None, report: RunReport) -> list[str]:
    lines = ["Occasions", "-" * 9]
    if result is None:
        lines.append(_absent(OCCASION_AGENT, report))
        return lines + [""]

    results = result.details.get("results", [])
    counted = _occasion_counts(results)
    lines += [
        f"Status: {result.status}",
        f"Birthdays today: {counted['birthdays']}",
        f"Anniversaries today: {counted['anniversaries']}",
        f"Greetings sent: {counted['SENT']}",
        f"Skipped duplicates: {counted['SKIPPED_DUPLICATE']}",
        f"Failures: {counted['FAILED']}",
    ]
    if counted["SKIPPED_DRY_RUN"]:
        lines.append(f"Held by dry run: {counted['SKIPPED_DRY_RUN']}")
    invalid = result.details.get("invalid_rows", [])
    if invalid:
        lines.append(f"Invalid contact rows: {len(invalid)}")
        lines += [f"  Row {row['row_number']}: {row['reason']}" for row in invalid[:10]]
    return lines + [""]


def _actions_text(result: AgentResult | None) -> list[str]:
    items = _action_items(result)
    lines = ["Action Required", "-" * 15]
    if not items:
        return lines + ["None", ""]
    for index, email in enumerate(items, start=1):
        c = email["classification"]
        lines.append(
            f"{index}. [{c['priority']}] {email['subject']} "
            f"- {_sender_name(email['sender'])}"
        )
        lines.append(f"   {c['summary']}")
        if c.get("suggested_deadline"):
            lines.append(f"   Deadline: {c['suggested_deadline']}")
        lines.append(f"   {email['gmail_link']}")
        if c.get("suggested_reply"):
            lines.append("   Suggested reply:")
            lines += [f"     {line}" for line in c["suggested_reply"].splitlines()]
        lines.append("")
    return lines


def _errors_text(report: RunReport) -> list[str]:
    lines = ["Errors", "-" * 6]
    errors = list(report.errors)
    for result in report.results.values():
        errors.extend(f"{result.agent}: {w}" for w in result.warnings)
    if not errors:
        return lines + ["None", ""]
    return lines + [f"- {message}" for message in errors] + [""]


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------


def _render_html(report: RunReport) -> str:
    colour = STATUS_COLOURS.get(report.status, "#57606a")
    gmail = report.results.get(GMAIL_AGENT)
    occasion = report.results.get(OCCASION_AGENT)

    banner = (
        '<p style="background:#fff8c5;border:1px solid #d4a72c;border-radius:6px;'
        'padding:10px 14px;margin:0 0 20px;">'
        "<strong>Dry run.</strong> Nothing was written to Gmail and no mail was "
        "sent. Set <code>dry_run: false</code> in config.yaml to act for real."
        "</p>"
        if report.dry_run
        else ""
    )

    sections = [
        _html_section("Gmail", _gmail_html(gmail, report)),
        _html_section("Occasions", _occasion_html(occasion, report)),
        _html_section("Action required", _actions_html(gmail)),
        _html_section("Errors and warnings", _errors_html(report)),
    ]

    return f"""<html>
<body style="font-family:Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;
             line-height:1.55;color:#1f2328;max-width:720px;margin:0 auto;padding:16px;">
  <h1 style="font-size:20px;margin:0 0 4px;">Daily Automation Report</h1>
  <p style="color:#57606a;margin:0 0 16px;">{html.escape(report.run_date)}
     &middot; run <code>{html.escape(report.run_id)}</code></p>
  <p style="margin:0 0 20px;">Overall status:
     <strong style="color:{colour};">{html.escape(report.status.replace('_', ' '))}</strong></p>
  {banner}
  {''.join(sections)}
</body>
</html>"""


def _html_section(title: str, body: str) -> str:
    return (
        f'<h2 style="font-size:15px;margin:24px 0 8px;padding-bottom:4px;'
        f'border-bottom:1px solid #d0d7de;">{html.escape(title)}</h2>{body}'
    )


def _stat_list(pairs: list[tuple[str, object]]) -> str:
    rows = "".join(
        f'<tr><td style="padding:3px 16px 3px 0;color:#57606a;">{html.escape(label)}</td>'
        f'<td style="padding:3px 0;font-weight:600;">{html.escape(str(value))}</td></tr>'
        for label, value in pairs
    )
    return f'<table style="border-collapse:collapse;">{rows}</table>'


def _gmail_html(result: AgentResult | None, report: RunReport) -> str:
    if result is None:
        return f"<p>{html.escape(_absent(GMAIL_AGENT, report))}</p>"
    counts = result.details.get("counts", {})
    verb = "Would be labeled" if report.dry_run else "Labeled"
    labeled = counts.get("would_label" if report.dry_run else "labeled", 0)
    pairs = [
        ("Status", result.status),
        ("New emails processed", counts.get("processed", result.items_processed)),
        ("Job / recruiter emails", counts.get("job_opportunities", 0)),
        ("Needing a reply", counts.get("requires_reply", 0)),
        ("High priority", counts.get("high_priority", 0)),
        (f"{verb} ToDelete", labeled),
    ]
    if counts.get("injection_attempts"):
        pairs.append(("Prompt-injection attempts", counts["injection_attempts"]))
    return _stat_list(pairs)


def _occasion_html(result: AgentResult | None, report: RunReport) -> str:
    if result is None:
        return f"<p>{html.escape(_absent(OCCASION_AGENT, report))}</p>"
    counted = _occasion_counts(result.details.get("results", []))
    pairs = [
        ("Status", result.status),
        ("Birthdays today", counted["birthdays"]),
        ("Anniversaries today", counted["anniversaries"]),
        ("Greetings sent", counted["SENT"]),
        ("Skipped duplicates", counted["SKIPPED_DUPLICATE"]),
        ("Failures", counted["FAILED"]),
    ]
    if counted["SKIPPED_DRY_RUN"]:
        pairs.append(("Held by dry run", counted["SKIPPED_DRY_RUN"]))

    body = _stat_list(pairs)
    invalid = result.details.get("invalid_rows", [])
    if invalid:
        items = "".join(
            f"<li>Row {row['row_number']}: {html.escape(row['reason'])}</li>"
            for row in invalid[:20]
        )
        body += (
            '<p style="margin:12px 0 4px;color:#57606a;">Invalid contact rows '
            f"({len(invalid)}):</p><ul>{items}</ul>"
        )
    return body


def _actions_html(result: AgentResult | None) -> str:
    items = _action_items(result)
    if not items:
        return '<p style="color:#57606a;">Nothing needs your attention today.</p>'

    cards = []
    for email in items:
        c = email["classification"]
        marker = PRIORITY_MARKERS.get(c["priority"], "")
        deadline = (
            f'<p style="margin:6px 0 0;color:#57606a;">Suggested deadline: '
            f'{html.escape(c["suggested_deadline"])}</p>'
            if c.get("suggested_deadline")
            else ""
        )
        reply = (
            '<details style="margin-top:10px;"><summary style="cursor:pointer;'
            'color:#0969da;">Suggested reply</summary>'
            f'<pre style="white-space:pre-wrap;background:#f6f8fa;padding:10px;'
            f'border-radius:6px;font-family:inherit;margin:8px 0 0;">'
            f'{html.escape(c["suggested_reply"])}</pre></details>'
            if c.get("suggested_reply")
            else ""
        )
        cards.append(
            '<div style="border:1px solid #d0d7de;border-radius:8px;padding:12px 14px;'
            'margin-bottom:12px;">'
            f'<p style="margin:0 0 2px;font-weight:600;">{marker} {html.escape(c["priority"])}'
            f' &middot; {html.escape(c["category"].replace("_", " ").title())}</p>'
            f'<p style="margin:0 0 2px;">{html.escape(email["subject"])}</p>'
            f'<p style="margin:0 0 8px;color:#57606a;">From: '
            f'{html.escape(email["sender"])}</p>'
            f'<p style="margin:0;">{html.escape(c["summary"])}</p>'
            f"{deadline}"
            f'<p style="margin:8px 0 0;"><a href="{html.escape(email["gmail_link"])}" '
            'style="color:#0969da;">Open in Gmail</a></p>'
            f"{reply}"
            "</div>"
        )
    return "".join(cards)


def _errors_html(report: RunReport) -> str:
    errors = list(report.errors)
    for result in report.results.values():
        errors.extend(f"{result.agent}: {w}" for w in result.warnings)
    if not errors:
        return '<p style="color:#57606a;">None.</p>'
    items = "".join(f"<li>{html.escape(message)}</li>" for message in errors)
    return f"<ul>{items}</ul>"


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _absent(agent: str, report: RunReport) -> str:
    state = report.states.get(agent)
    if state is TaskState.SKIPPED:
        return "Disabled in configuration; not run."
    return "Did not run."


def _action_items(result: AgentResult | None) -> list[dict]:
    """Reply-required and job-related emails, most urgent first."""
    if result is None:
        return []
    notify = result.details.get("notify", [])
    ranking = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "NONE": 4}
    actionable = [
        email
        for email in notify
        if email["classification"]["requires_reply"]
        or email["classification"]["category"] in ("JOB_OPPORTUNITY", "RECRUITER")
    ]
    return sorted(
        actionable, key=lambda e: ranking.get(e["classification"]["priority"], 9)
    )


def _occasion_counts(results: list[dict]) -> dict:
    counts = {
        "birthdays": 0,
        "anniversaries": 0,
        "SENT": 0,
        "SKIPPED_DUPLICATE": 0,
        "SKIPPED_DRY_RUN": 0,
        "SKIPPED_DISABLED": 0,
        "FAILED": 0,
    }
    for item in results:
        if item["contact"]["event_type"] == "Birthday":
            counts["birthdays"] += 1
        else:
            counts["anniversaries"] += 1
        outcome = item["outcome"]
        if outcome in counts:
            counts[outcome] += 1
    return counts


def _sender_name(sender: str) -> str:
    return sender.split("<")[0].strip().strip('"') or sender
