"""Command line behaviour: output, argument checks and one-line errors."""

import json

import pytest
import yaml
from typer.testing import CliRunner

from rising.interfaces import cli

runner = CliRunner()


@pytest.fixture
def config_file(seeded, db_settings, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(db_settings.model_dump(mode="json")))
    return str(path)


def test_rank_json_lists_the_ranking(config_file):
    result = runner.invoke(cli.app, ["rank", "--json", "-c", config_file])
    assert result.exit_code == 0
    assert [r["full_name"] for r in json.loads(result.stdout)["repos"]] == [
        "good/agent",
        "hype/tool",
    ]


def test_runs_prints_the_audit_log(config_file):
    result = runner.invoke(cli.app, ["runs", "-c", config_file])
    assert result.exit_code == 0 and isinstance(json.loads(result.stdout), list)


def test_rank_rejects_a_non_positive_n(config_file):
    assert runner.invoke(cli.app, ["rank", "--n", "0", "-c", config_file]).exit_code == 2


def test_missing_token_prints_one_line_and_exits_2(config_file, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr("sys.argv", ["rising", "snapshot", "-c", config_file])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 2
    assert capsys.readouterr().err.strip() == (
        "error: GitHub token missing: set $GITHUB_TOKEN (see .env.example)"
    )


def test_digest_on_an_empty_database_prints_one_line(tmp_path, settings, monkeypatch, capsys):
    path = tmp_path / "empty.yaml"
    path.write_text(
        yaml.safe_dump(settings.model_dump(mode="json") | {"database_path": str(tmp_path / "e.db")})
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("sys.argv", ["rising", "digest", "-c", str(path)])
    monkeypatch.setattr("rising.interfaces.cli.bootstrap.github", lambda s: None)
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 2
    assert "No ranked repositories yet" in capsys.readouterr().err
