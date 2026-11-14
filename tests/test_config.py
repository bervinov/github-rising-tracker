"""The shipped config.yaml and the validation rules."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from rising.config import Settings, load_settings

SHIPPED = Path(__file__).parent.parent / "config.yaml"


def test_shipped_config_is_valid():
    s = load_settings(SHIPPED)
    assert s.discovery.window_days == 7
    assert s.database_url == "sqlite:///data/rising.db"


def test_typo_in_a_key_is_an_error():
    with pytest.raises(ValidationError, match="widow_days"):
        Settings.model_validate({"discovery": {"widow_days": 7, "filters": []}})


def test_filter_without_criteria_is_an_error():
    with pytest.raises(ValidationError, match="would match all of GitHub"):
        Settings.model_validate({"discovery": {"filters": [{"name": "everything"}]}})


def test_filter_names_must_be_unique():
    same = {"name": "a", "topics": ["llm"]}
    with pytest.raises(ValidationError, match="unique"):
        Settings.model_validate({"discovery": {"filters": [same, same]}})


def test_missing_config_file_is_reported(tmp_path):
    with pytest.raises(FileNotFoundError, match="Config not found"):
        load_settings(tmp_path / "nope.yaml")
    with pytest.raises(FileNotFoundError, match="Config not found"):
        load_settings(tmp_path)  # a directory, as Docker creates for a missing bind mount


def test_empty_rising_config_falls_back_to_the_default_path(monkeypatch):
    monkeypatch.chdir(SHIPPED.parent)
    monkeypatch.setenv("RISING_CONFIG", "")
    assert load_settings().discovery.window_days == 7
