"""Trend aggregation over automation_run history (PRD Phase 2, dashboard #13).

Everything here reads automation_run.summary_json, which the supervisor
already writes on every run - no new integration, no new external calls. Cost
estimates are informational only: pricing changes over time and this is not
meant to replace your provider's own billing dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from ..repositories import RunRepository

#: $ per million tokens, (input, output). Anthropic pricing as of the models
#: this project ships with; update when models or prices change. An unlisted
#: model shows as "unknown cost" rather than silently guessing.
MODEL_PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}


@dataclass
class DayStats:
    run_date: str
    status: str = "—"
    dry_run: bool = True
    emails_processed: int = 0
    job_opportunities: int = 0
    requires_reply: int = 0
    labeled: int = 0
    greetings_sent: int = 0
    duplicates_skipped: int = 0
    failures: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float | None = 0.0
    has_unpriced_model: bool = False


@dataclass
class AnalyticsSummary:
    days: list[DayStats] = field(default_factory=list)

    @property
    def total_runs(self) -> int:
        return len(self.days)

    @property
    def total_emails_processed(self) -> int:
        return sum(d.emails_processed for d in self.days)

    @property
    def total_greetings_sent(self) -> int:
        return sum(d.greetings_sent for d in self.days)

    @property
    def total_job_opportunities(self) -> int:
        return sum(d.job_opportunities for d in self.days)

    @property
    def total_estimated_cost_usd(self) -> float:
        """Sum of whatever could be priced. See `any_unpriced_model` for
        whether this is the whole story or a partial ("at least") figure."""
        return sum(
            d.estimated_cost_usd for d in self.days if d.estimated_cost_usd is not None
        )

    @property
    def any_unpriced_model(self) -> bool:
        return any(d.has_unpriced_model for d in self.days)


def _estimate_cost(model: str | None, input_tokens: int, output_tokens: int) -> float | None:
    """$ for this call, or None if the model is unknown or unrecorded.

    None is a real answer, not zero: it means "we cannot honestly price this,"
    which older runs (before the model was recorded in AgentResult.details)
    and any future model not yet added to MODEL_PRICING_PER_MTOK will hit.
    """
    if not model:
        return None
    prices = MODEL_PRICING_PER_MTOK.get(model)
    if prices is None:
        return None
    input_price, output_price = prices
    return (input_tokens / 1_000_000) * input_price + (
        output_tokens / 1_000_000
    ) * output_price


def build_analytics(session: Session, *, limit: int = 30) -> AnalyticsSummary:
    """Summarise the most recent `limit` runs, newest first."""
    runs = RunRepository(session).recent(limit)
    days: list[DayStats] = []

    for run in runs:
        payload = run.summary_json or {}
        results = payload.get("results", {})
        stats = DayStats(
            run_date=run.run_date.isoformat()
            if isinstance(run.run_date, date)
            else str(run.run_date),
            status=run.status,
            dry_run=run.dry_run,
        )

        gmail = results.get("gmail_triage_agent", {})
        gmail_details = gmail.get("details", {}) or {}
        gmail_counts = gmail_details.get("counts", {}) or {}
        stats.emails_processed = gmail_counts.get("processed", 0)
        stats.job_opportunities = gmail_counts.get("job_opportunities", 0)
        stats.requires_reply = gmail_counts.get("requires_reply", 0)
        stats.labeled = (
            gmail_counts.get("would_label", 0)
            if run.dry_run
            else gmail_counts.get("labeled", 0)
        )

        occasion = results.get("occasion_agent", {})
        occasion_details = occasion.get("details", {}) or {}
        occasion_results = occasion_details.get("results", []) or []
        stats.greetings_sent = sum(
            1 for r in occasion_results if r.get("outcome") == "SENT"
        )
        stats.duplicates_skipped = sum(
            1 for r in occasion_results if r.get("outcome") == "SKIPPED_DUPLICATE"
        )
        stats.failures = sum(
            1 for r in occasion_results if r.get("outcome") == "FAILED"
        ) + sum(1 for r in results.values() if r.get("status") == "FAILED")

        cost_total = 0.0
        any_priced_activity = False
        for agent_name in ("gmail_triage_agent", "occasion_agent"):
            agent_details = results.get(agent_name, {}).get("details", {}) or {}
            usage = agent_details.get("token_usage") or {}
            in_tok = usage.get("input_tokens", 0)
            out_tok = usage.get("output_tokens", 0)
            stats.input_tokens += in_tok
            stats.output_tokens += out_tok
            if not (in_tok or out_tok):
                continue

            cost = _estimate_cost(agent_details.get("model"), in_tok, out_tok)
            if cost is None:
                stats.has_unpriced_model = True
            else:
                cost_total += cost
                any_priced_activity = True

        stats.estimated_cost_usd = (
            cost_total if (any_priced_activity or not stats.has_unpriced_model) else None
        )
        # If there was spend AND some of it couldn't be priced, show the
        # partial total but flag it so the UI can say "at least $X".
        days.append(stats)

    return AnalyticsSummary(days=days)
