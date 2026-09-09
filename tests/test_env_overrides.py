"""Environment overrides for operator-specific settings.

config.yaml is committed to a public repository, so anything identifying the
operator - Drive file ID, real name, protected-sender addresses - lives in .env
instead and overrides the YAML at load time.
"""

from __future__ import annotations

import pytest
import yaml

from daily_ai_automation.config.settings import ENV_OVERRIDES, load_settings

ALL_ENV_VARS = [name for name, _path, _kind in ENV_OVERRIDES]


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """A template config.yaml with empty operator fields, as committed."""
    config = {
        "dry_run": True,
        "scheduler": {"timezone": "Asia/Kolkata", "daily_run_time": "09:00"},
        "database": {"url": f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}"},
        "gmail": {"enabled": True, "protected_senders": ["@placeholder.example"]},
        "occasion": {"enabled": True, "drive_file_id": "", "sender_name": ""},
        "notifications": {"enabled": True, "channel": "email", "recipient": ""},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    # A stray real .env must not leak into these assertions.
    for name in ALL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return path


def load(config_file, tmp_path):
    return load_settings(config_file, project_root=tmp_path)


class TestDefaults:
    def test_with_nothing_set_the_yaml_wins(self, config_file, tmp_path):
        settings = load(config_file, tmp_path)
        assert settings.dry_run is True
        assert settings.occasion.drive_file_id == ""
        assert settings.occasion.sender_name == ""
        assert settings.gmail.protected_senders == ["@placeholder.example"]


class TestOverrides:
    def test_drive_file_id(self, config_file, tmp_path, monkeypatch):
        monkeypatch.setenv("DAILY_DRIVE_FILE_ID", "abc123")
        assert load(config_file, tmp_path).occasion.drive_file_id == "abc123"

    def test_sender_name(self, config_file, tmp_path, monkeypatch):
        monkeypatch.setenv("DAILY_SENDER_NAME", "Some Person")
        assert load(config_file, tmp_path).occasion.sender_name == "Some Person"

    def test_notify_recipient(self, config_file, tmp_path, monkeypatch):
        monkeypatch.setenv("DAILY_NOTIFY_RECIPIENT", "me@example.com")
        assert load(config_file, tmp_path).notifications.recipient == "me@example.com"

    def test_timezone(self, config_file, tmp_path, monkeypatch):
        monkeypatch.setenv("DAILY_TIMEZONE", "Europe/London")
        assert load(config_file, tmp_path).scheduler.timezone == "Europe/London"

    def test_values_are_stripped(self, config_file, tmp_path, monkeypatch):
        monkeypatch.setenv("DAILY_SENDER_NAME", "  Padded Name  ")
        assert load(config_file, tmp_path).occasion.sender_name == "Padded Name"


class TestDryRunParsing:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("false", False),
            ("False", False),
            ("FALSE", False),
            ("0", False),
            ("no", False),
            ("off", False),
            ("true", True),
            ("True", True),
            ("1", True),
            ("yes", True),
            ("on", True),
        ],
    )
    def test_accepted_boolean_spellings(
        self, config_file, tmp_path, monkeypatch, value, expected
    ):
        monkeypatch.setenv("DAILY_DRY_RUN", value)
        assert load(config_file, tmp_path).dry_run is expected

    def test_a_nonsense_value_fails_loudly(self, config_file, tmp_path, monkeypatch):
        """Silently defaulting here could turn a dry run into a live one."""
        monkeypatch.setenv("DAILY_DRY_RUN", "maybe")
        with pytest.raises(ValueError, match="not a boolean"):
            load(config_file, tmp_path)


class TestProtectedSenders:
    def test_comma_separated_list(self, config_file, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "DAILY_PROTECTED_SENDERS", "@bank.example, mum@family.example"
        )
        senders = load(config_file, tmp_path).gmail.protected_senders
        assert senders == ["@bank.example", "mum@family.example"]

    def test_blank_entries_are_dropped(self, config_file, tmp_path, monkeypatch):
        monkeypatch.setenv("DAILY_PROTECTED_SENDERS", "@a.example,,  ,@b.example")
        assert load(config_file, tmp_path).gmail.protected_senders == [
            "@a.example",
            "@b.example",
        ]


class TestEmptyIsTreatedAsUnset:
    """A blank line in .env must not blank out a required field."""

    @pytest.mark.parametrize(
        ("var", "getter"),
        [
            ("DAILY_SENDER_NAME", lambda s: s.occasion.sender_name),
            ("DAILY_DRIVE_FILE_ID", lambda s: s.occasion.drive_file_id),
            ("DAILY_TIMEZONE", lambda s: s.scheduler.timezone),
        ],
    )
    def test_empty_value_falls_back_to_yaml(
        self, config_file, tmp_path, monkeypatch, var, getter
    ):
        yaml_value = getter(load(config_file, tmp_path))
        monkeypatch.setenv(var, "")
        assert getter(load(config_file, tmp_path)) == yaml_value

    def test_whitespace_only_also_falls_back(
        self, config_file, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("DAILY_DRY_RUN", "   ")
        assert load(config_file, tmp_path).dry_run is True


class TestNoOperatorDataInTheRepository:
    """The committed config.yaml must stay free of anyone's real details."""

    def test_committed_config_has_empty_operator_fields(self):
        from pathlib import Path

        repo_config = Path(__file__).resolve().parents[1] / "config.yaml"
        raw = yaml.safe_load(repo_config.read_text(encoding="utf-8"))

        assert raw["occasion"]["drive_file_id"] == "", (
            "config.yaml carries a real Drive file ID; move it to DAILY_DRIVE_FILE_ID"
        )
        assert raw["occasion"]["sender_name"] == "", (
            "config.yaml carries a real sender name; move it to DAILY_SENDER_NAME"
        )
        assert raw["dry_run"] is True, (
            "config.yaml must ship safe-by-default; set DAILY_DRY_RUN=false in .env"
        )
