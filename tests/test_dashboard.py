"""Dashboard routes: report browsing and the mark-done mutation.

Uses starlette's TestClient (an in-process ASGI client - no real socket, no
network) against the same session_factory the CLI would build.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

from daily_ai_automation.contracts import EmailCategory
from daily_ai_automation.dashboard import create_app
from daily_ai_automation.db import session_scope
from daily_ai_automation.repositories import EmailRepository
from daily_ai_automation.supervisor.supervisor import Supervisor
from tests.test_email_repository_actions import _triaged
from tests.test_supervisor import make_agent, succeeds


@pytest.fixture
def client(settings, session_factory) -> TestClient:
    app = create_app(settings, session_factory)
    return TestClient(app)


def _seed_run(session_factory, settings) -> str:
    """Produce one real automation_run row via the actual supervisor, so
    dashboard tests exercise the same reconstruction path production uses."""
    registrations = [make_agent("gmail_triage_agent", succeeds("ok"))]
    report = Supervisor(settings, session_factory, registrations).run(
        now=datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc)
    )
    return report.run_id


class TestRunsList:
    def test_empty_database_renders_without_error(self, client):
        r = client.get("/runs")
        assert r.status_code == 200
        assert "No runs recorded" in r.text

    def test_root_redirects_to_runs(self, client):
        r = client.get("/", follow_redirects=False)
        assert r.status_code in (302, 307)
        assert r.headers["location"] == "/runs"

    def test_a_seeded_run_appears(self, client, session_factory, settings):
        run_id = _seed_run(session_factory, settings)
        r = client.get("/runs")
        assert run_id in r.text


class TestRunDetail:
    def test_unknown_run_is_404(self, client):
        r = client.get("/runs/does-not-exist")
        assert r.status_code == 404

    def test_a_real_run_renders(self, client, session_factory, settings):
        run_id = _seed_run(session_factory, settings)
        r = client.get(f"/runs/{run_id}")
        assert r.status_code == 200
        assert run_id in r.text
        assert "COMPLETED" in r.text

    def test_action_items_for_the_run_are_shown_with_a_mark_done_form(
        self, client, session_factory, settings
    ):
        run_id = _seed_run(session_factory, settings)
        with session_scope(session_factory) as session:
            EmailRepository(session).record(
                run_id,
                _triaged(
                    "job1",
                    requires_reply=True,
                    category=EmailCategory.JOB_OPPORTUNITY,
                ),
            )

        r = client.get(f"/runs/{run_id}")
        assert "job1" in r.text or "/actions/job1/mark" in r.text
        assert '/actions/job1/mark' in r.text
        assert f'value="/runs/{run_id}"' in r.text  # redirect_to is run-scoped

    def test_a_non_actionable_email_gets_no_mark_done_form(
        self, client, session_factory, settings
    ):
        run_id = _seed_run(session_factory, settings)
        with session_scope(session_factory) as session:
            EmailRepository(session).record(
                run_id,
                _triaged("newsletter1", category=EmailCategory.NEWSLETTER),
            )
        r = client.get(f"/runs/{run_id}")
        assert "/actions/newsletter1/mark" not in r.text


class TestOpenActionsPage:
    def test_empty_state(self, client):
        r = client.get("/actions")
        assert r.status_code == 200
        assert "Nothing open" in r.text

    def test_lists_an_actionable_pending_email(self, client, session_factory):
        with session_scope(session_factory) as session:
            EmailRepository(session).record(
                "run-x", _triaged("job1", requires_reply=True)
            )
        r = client.get("/actions")
        assert "job1" in r.text or "/actions/job1/mark" in r.text

    def test_mark_done_removes_it_and_redirects(self, client, session_factory):
        with session_scope(session_factory) as session:
            EmailRepository(session).record(
                "run-x", _triaged("job1", requires_reply=True)
            )

        r = client.post(
            "/actions/job1/mark",
            data={"status": "DONE", "redirect_to": "/actions"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"].startswith("/actions")

        with session_scope(session_factory) as session:
            assert EmailRepository(session).open_actions() == []

    def test_mark_dismissed_also_removes_it(self, client, session_factory):
        with session_scope(session_factory) as session:
            EmailRepository(session).record(
                "run-x", _triaged("job1", requires_reply=True)
            )
        client.post("/actions/job1/mark", data={"status": "DISMISSED"})
        with session_scope(session_factory) as session:
            assert EmailRepository(session).open_actions() == []

    def test_unknown_message_id_is_404(self, client):
        r = client.post("/actions/does-not-exist/mark", data={"status": "DONE"})
        assert r.status_code == 404

    def test_marking_pending_is_rejected(self, client, session_factory):
        """PENDING is a starting state, not something to mark yourself into."""
        with session_scope(session_factory) as session:
            EmailRepository(session).record(
                "run-x", _triaged("job1", requires_reply=True)
            )
        r = client.post("/actions/job1/mark", data={"status": "PENDING"})
        assert r.status_code == 400

    def test_a_bogus_status_is_rejected(self, client, session_factory):
        with session_scope(session_factory) as session:
            EmailRepository(session).record(
                "run-x", _triaged("job1", requires_reply=True)
            )
        r = client.post("/actions/job1/mark", data={"status": "MAYBE"})
        assert r.status_code == 400

    def test_redirect_to_an_external_url_is_ignored(self, client, session_factory):
        """The redirect target is only ever trusted as a same-origin path."""
        with session_scope(session_factory) as session:
            EmailRepository(session).record(
                "run-x", _triaged("job1", requires_reply=True)
            )
        r = client.post(
            "/actions/job1/mark",
            data={"status": "DONE", "redirect_to": "https://evil.example/steal"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"].startswith("/actions")
        assert "evil.example" not in r.headers["location"]


class TestAnalyticsPage:
    def test_empty_database_renders_without_error(self, client):
        r = client.get("/analytics")
        assert r.status_code == 200
        assert "No runs yet" in r.text

    def test_a_seeded_run_contributes_to_totals(self, client, session_factory, settings):
        _seed_run(session_factory, settings)
        r = client.get("/analytics")
        assert r.status_code == 200
        assert ">1<" in r.text  # total_runs stat


class TestOpenActionCountInNav:
    def test_nav_badge_reflects_pending_count(self, client, session_factory):
        r = client.get("/runs")
        assert "Open Actions" in r.text

        with session_scope(session_factory) as session:
            EmailRepository(session).record(
                "run-x", _triaged("job1", requires_reply=True)
            )
        r = client.get("/runs")
        assert "Open Actions (1)" in r.text
