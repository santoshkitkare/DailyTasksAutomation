"""Analytics aggregation and cost estimation (dashboard M2)."""

from __future__ import annotations

from datetime import date

import pytest

from daily_ai_automation.dashboard.analytics import (
    MODEL_PRICING_PER_MTOK,
    _estimate_cost,
    build_analytics,
)
from daily_ai_automation.db import session_scope
from daily_ai_automation.models import AutomationRun


def _seed(session_factory, *, run_date: date, run_id: str, summary_json: dict) -> None:
    with session_scope(session_factory) as session:
        session.add(
            AutomationRun(
                id=run_id,
                run_date=run_date,
                status=summary_json.get("status", "COMPLETED"),
                dry_run=summary_json.get("dry_run", True),
                summary_json=summary_json,
            )
        )


def _run_payload(
    *,
    status="COMPLETED",
    dry_run=True,
    gmail_model="claude-haiku-4-5",
    gmail_in=1000,
    gmail_out=200,
    gmail_processed=5,
    gmail_labeled=2,
    job_opps=1,
    occasion_model="claude-opus-5",
    occasion_in=500,
    occasion_out=100,
    sent=1,
    duplicates=0,
) -> dict:
    return {
        "status": status,
        "dry_run": dry_run,
        "results": {
            "gmail_triage_agent": {
                "status": status,
                "details": {
                    "model": gmail_model,
                    "token_usage": {
                        "input_tokens": gmail_in,
                        "output_tokens": gmail_out,
                    },
                    "counts": {
                        "processed": gmail_processed,
                        "job_opportunities": job_opps,
                        "requires_reply": 1,
                        "labeled": gmail_labeled,
                        "would_label": gmail_labeled,
                    },
                },
            },
            "occasion_agent": {
                "status": status,
                "details": {
                    "model": occasion_model,
                    "token_usage": {
                        "input_tokens": occasion_in,
                        "output_tokens": occasion_out,
                    },
                    "results": [
                        {"outcome": "SENT"} for _ in range(sent)
                    ]
                    + [{"outcome": "SKIPPED_DUPLICATE"} for _ in range(duplicates)],
                },
            },
        },
    }


class TestEstimateCost:
    def test_known_model_prices_correctly(self):
        cost = _estimate_cost("claude-haiku-4-5", 1_000_000, 1_000_000)
        input_price, output_price = MODEL_PRICING_PER_MTOK["claude-haiku-4-5"]
        assert cost == input_price + output_price

    def test_unknown_model_returns_none(self):
        assert _estimate_cost("some-future-model", 1000, 1000) is None

    def test_missing_model_returns_none(self):
        assert _estimate_cost(None, 1000, 1000) is None
        assert _estimate_cost("", 1000, 1000) is None

    def test_zero_tokens_costs_zero(self):
        assert _estimate_cost("claude-opus-5", 0, 0) == 0.0


class TestBuildAnalytics:
    def test_empty_database(self, session_factory):
        with session_scope(session_factory) as session:
            summary = build_analytics(session)
        assert summary.total_runs == 0
        assert summary.total_estimated_cost_usd == 0.0
        assert summary.any_unpriced_model is False

    def test_a_single_run_is_summarised(self, session_factory):
        _seed(
            session_factory,
            run_date=date(2026, 9, 7),
            run_id="r1",
            summary_json=_run_payload(),
        )
        with session_scope(session_factory) as session:
            summary = build_analytics(session)

        assert summary.total_runs == 1
        assert summary.total_emails_processed == 5
        assert summary.total_job_opportunities == 1
        assert summary.total_greetings_sent == 1
        day = summary.days[0]
        assert day.duplicates_skipped == 0
        assert day.input_tokens == 1500  # 1000 + 500
        assert day.output_tokens == 300  # 200 + 100

    def test_cost_is_computed_per_agent_model(self, session_factory):
        _seed(
            session_factory,
            run_date=date(2026, 9, 7),
            run_id="r1",
            summary_json=_run_payload(
                gmail_model="claude-haiku-4-5",
                gmail_in=1_000_000,
                gmail_out=0,
                occasion_model="claude-opus-5",
                occasion_in=1_000_000,
                occasion_out=0,
            ),
        )
        with session_scope(session_factory) as session:
            summary = build_analytics(session)

        expected = (
            MODEL_PRICING_PER_MTOK["claude-haiku-4-5"][0]
            + MODEL_PRICING_PER_MTOK["claude-opus-5"][0]
        )
        assert summary.days[0].estimated_cost_usd == pytest.approx(expected)
        assert summary.any_unpriced_model is False

    def test_an_unrecognised_model_is_flagged_not_guessed(self, session_factory):
        """This is the bug that was caught and fixed during implementation:
        the old code priced unknown models by taking the cheapest known price,
        silently underestimating spend. It must now be flagged, not guessed."""
        _seed(
            session_factory,
            run_date=date(2026, 9, 7),
            run_id="r1",
            summary_json=_run_payload(gmail_model="some-future-model", gmail_in=1000, gmail_out=1000),
        )
        with session_scope(session_factory) as session:
            summary = build_analytics(session)

        assert summary.days[0].has_unpriced_model is True
        assert summary.any_unpriced_model is True

    def test_partial_pricing_still_sums_what_is_known(self, session_factory):
        """One agent priced, one not: the total should reflect the priced part,
        not be nulled out entirely and lose real information."""
        _seed(
            session_factory,
            run_date=date(2026, 9, 7),
            run_id="r1",
            summary_json=_run_payload(
                gmail_model="claude-haiku-4-5",
                gmail_in=1_000_000,
                gmail_out=0,
                occasion_model="unknown-model",
                occasion_in=1_000_000,
                occasion_out=0,
            ),
        )
        with session_scope(session_factory) as session:
            summary = build_analytics(session)

        day = summary.days[0]
        assert day.has_unpriced_model is True
        assert day.estimated_cost_usd == pytest.approx(
            MODEL_PRICING_PER_MTOK["claude-haiku-4-5"][0]
        )
        assert summary.total_estimated_cost_usd == pytest.approx(
            MODEL_PRICING_PER_MTOK["claude-haiku-4-5"][0]
        )

    def test_a_run_with_no_token_usage_is_not_flagged_unpriced(self, session_factory):
        """A run that made no LLM calls at all (e.g. nothing new to classify)
        must not show as 'unpriced' - there was nothing to price."""
        _seed(
            session_factory,
            run_date=date(2026, 9, 7),
            run_id="r1",
            summary_json=_run_payload(gmail_in=0, gmail_out=0, occasion_in=0, occasion_out=0),
        )
        with session_scope(session_factory) as session:
            summary = build_analytics(session)

        assert summary.days[0].has_unpriced_model is False
        assert summary.days[0].estimated_cost_usd == 0.0

    def test_dry_run_reports_would_label_not_labeled(self, session_factory):
        _seed(
            session_factory,
            run_date=date(2026, 9, 7),
            run_id="r1",
            summary_json=_run_payload(dry_run=True, gmail_labeled=7),
        )
        with session_scope(session_factory) as session:
            summary = build_analytics(session)
        assert summary.days[0].labeled == 7

    def test_limit_controls_how_many_runs_are_included(self, session_factory):
        for i in range(5):
            _seed(
                session_factory,
                run_date=date(2026, 9, 1 + i),
                run_id=f"r{i}",
                summary_json=_run_payload(),
            )
        with session_scope(session_factory) as session:
            summary = build_analytics(session, limit=2)
        assert summary.total_runs == 2

    def test_failures_are_counted(self, session_factory):
        _seed(
            session_factory,
            run_date=date(2026, 9, 7),
            run_id="r1",
            summary_json=_run_payload(status="PARTIAL_SUCCESS"),
        )
        with session_scope(session_factory) as session:
            summary = build_analytics(session)
        assert summary.days[0].failures >= 0  # no crash on a non-COMPLETED run
