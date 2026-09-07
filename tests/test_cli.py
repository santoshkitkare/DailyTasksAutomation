"""CLI behaviour: exit codes and the empty-database edge cases."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from daily_ai_automation.main import app

runner = CliRunner()


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    """A throwaway project directory with its own config.yaml and database."""
    source = Path(__file__).resolve().parents[1] / "config.yaml"
    config = yaml.safe_load(source.read_text(encoding="utf-8"))
    config["database"]["url"] = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    config["logging"]["file"] = str(tmp_path / "log.jsonl")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    # Keep the real .env out of the test run.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(
        "daily_ai_automation.config.settings.PROJECT_ROOT", tmp_path
    )
    return tmp_path


def invoke(project: Path, *args):
    return runner.invoke(app, [*args, "--config", str(project / "config.yaml")])


class TestSetup:
    def test_it_exits_non_zero_while_prerequisites_are_missing(self, project):
        result = invoke(project, "setup")
        assert result.exit_code == 1
        assert "blocking item" in result.stdout

    def test_it_names_each_missing_item(self, project):
        result = invoke(project, "setup")
        for expected in (
            "OAuth client",
            "Google token",
            "ANTHROPIC_API_KEY",
            "occasion.drive_file_id",
            "occasion.sender_name",
        ):
            assert expected in result.stdout

    def test_it_warns_that_dry_run_is_on(self, project):
        assert "dry_run is true" in invoke(project, "setup").stdout


class TestReportAndHistory:
    def test_report_on_an_empty_database_says_so_and_exits_one(self, project):
        result = invoke(project, "report", "last")
        assert result.exit_code == 1
        assert "No runs recorded yet" in result.stdout

    def test_report_for_an_unknown_run_id_exits_one(self, project):
        result = invoke(project, "report", "1999-01-01-000000")
        assert result.exit_code == 1
        assert "No run with ID" in result.stdout

    def test_history_on_an_empty_database_succeeds(self, project):
        result = invoke(project, "history")
        assert result.exit_code == 0
        assert "Recent runs" in result.stdout


class TestRunRoundTrip:
    def test_a_run_is_persisted_and_re_renderable(self, project, monkeypatch):
        """run -> history -> report last, with no workers registered."""
        monkeypatch.setattr(
            "daily_ai_automation.app.build_registrations", lambda: []
        )

        first = invoke(project, "run")
        assert first.exit_code == 0
        assert "COMPLETED" in first.stdout

        history = invoke(project, "history")
        assert "COMPLETED" in history.stdout
        assert "dry run" in history.stdout

        report = invoke(project, "report", "last")
        assert report.exit_code == 0
        assert "Daily Automation Report" in report.stdout
        assert "DRY RUN" in report.stdout

    def test_a_second_run_on_the_same_day_exits_two(self, project, monkeypatch):
        monkeypatch.setattr(
            "daily_ai_automation.app.build_registrations", lambda: []
        )
        assert invoke(project, "run").exit_code == 0

        second = invoke(project, "run")
        assert second.exit_code == 2
        assert "already exists" in second.stdout

        assert invoke(project, "run", "--force").exit_code == 0
