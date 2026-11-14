import pytest
from fastapi.testclient import TestClient

from rising.interfaces import api
from rising.interfaces.scheduler import Job, Scheduler


@pytest.fixture
def client(seeded, db_settings):
    api.app.dependency_overrides[api.get_settings] = lambda: db_settings
    yield TestClient(api.app)
    api.app.dependency_overrides.clear()


def test_api_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_api_ranking_lists_repos_with_meta(client):
    rising = client.get("/rising", params={"n": 2}).json()
    assert [r["full_name"] for r in rising["repos"]] == ["good/agent", "hype/tool"]
    assert "score_formula" in rising["meta"] and rising["meta"]["data_as_of"]
    assert client.get("/rising", params={"n": 0}).status_code == 422


def test_api_repos_filter_by_language(client):
    assert len(client.get("/repos", params={"language": "python"}).json()) == 2
    assert client.get("/repos", params={"language": "Rust"}).json() == []


def test_api_snapshot_series_with_and_without_since(client):
    url = "/repos/good/agent/snapshots"
    assert [s["stars"] for s in client.get(url).json()] == [400, 640]
    since = {"since": "2026-11-13T00:00:00"}  # no zone: read as UTC
    assert [s["stars"] for s in client.get(url, params=since).json()] == [640]


def test_api_star_history_and_unknown_repo(client):
    assert client.get("/repos/hype/tool/star-history").json()[1]["stars_added"] == 1300
    assert client.get("/repos/nope/nope").status_code == 404


def test_api_publishes_the_domain_models_as_json_schema(client):
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    assert {"Snapshot", "RankedRepo", "RedFlag"} <= set(schemas)


def test_scheduler_runs_due_jobs_and_survives_failures():
    t = [0.0]
    calls = []

    def boom():
        raise RuntimeError("x")

    s = Scheduler([Job("a", 60, lambda: calls.append("a")), Job("b", 10, boom)], clock=lambda: t[0])
    assert s.run_pending() == ["a", "b"]
    t[0] = 30
    assert s.run_pending() == ["b"]
    t[0] = 61
    assert s.run_pending() == ["a", "b"]
    assert calls == ["a", "a"]


def test_scheduler_counts_the_interval_from_the_start_of_a_job():
    t = [0.0]

    def slow():
        t[0] += 50  # the job itself takes 50 s

    s = Scheduler([Job("snap", 60, slow)], clock=lambda: t[0])
    s.run_pending()
    t[0] = 60
    assert s.run_pending() == ["snap"]  # due at 60, not at 110
