"""Collection jobs against a fake GitHub and a real database."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from rising.domain.models import StarDay
from rising.domain.timeutil import to_iso, utcnow
from rising.github.client import GitHubError
from rising.pipeline.jobs import Collector


def search_item(rid: int, created_hours_ago: float = 24) -> dict[str, Any]:
    name = f"owner{rid}/repo{rid}"
    return {
        "id": rid,
        "node_id": f"R_{rid}",
        "full_name": name,
        "html_url": f"https://github.com/{name}",
        "description": f"project number {rid}",
        "topics": ["llm"],
        "owner": {"login": f"owner{rid}", "type": "User"},
        "fork": False,
        "created_at": to_iso(utcnow() - timedelta(hours=created_hours_ago)),
    }


def graphql_node(node_id: str) -> dict[str, Any]:
    count = {"totalCount": 1}
    return {
        "databaseId": int(node_id.removeprefix("R_")),
        "stargazerCount": 100,
        "forkCount": 5,
        "watchers": count,
        "openIssues": count,
        "closedIssues": count,
        "openPRs": count,
        "mergedPRs": count,
        "closedPRs": count,
        "releases": count,
        "latestRelease": None,
        "defaultBranchRef": None,
        "pushedAt": None,
        "diskUsage": 10,
        "isArchived": False,
    }


class FakeGitHub:
    """The three GitHub calls the collector makes, answered from memory."""

    def __init__(
        self,
        items: list[dict[str, Any]] | None = None,
        history: Callable[[str], list[StarDay] | None] = lambda name: [],
        graphql: Callable[[list[str]], dict[str, Any]] | None = None,
        search_error: GitHubError | None = None,
    ):
        self.stats: dict[str, float] = {"requests": 0, "retries": 0, "slept_s": 0.0}
        self.items = items or []
        self.history = history
        self.answer_graphql = graphql or (
            lambda ids: {"data": {"nodes": [graphql_node(i) for i in ids]}}
        )
        self.search_error = search_error
        self.queries: list[str] = []
        self.history_calls: list[str] = []

    def search_repositories(self, q: str, page: int = 1, per_page: int = 100) -> dict[str, Any]:
        if self.search_error:
            raise self.search_error
        self.queries.append(q)
        items = self.items if page == 1 else []
        return {"total_count": len(self.items), "items": items, "incomplete_results": False}

    def star_history(self, full_name: str) -> list[StarDay] | None:
        self.history_calls.append(full_name)
        return self.history(full_name)

    def graphql(
        self, query: str, variables: dict[str, Any], server_error_retries: int | None = None
    ) -> dict[str, Any]:
        return self.answer_graphql(variables["ids"])


def collector(store, settings, gh: FakeGitHub, **discovery: Any) -> Collector:
    if discovery:
        cfg = settings.discovery.model_copy(update=discovery)
        settings = settings.model_copy(update={"discovery": cfg})
    return Collector(store, gh, settings)


def test_discover_stores_each_repo_once_and_counts_new_ones(store, settings):
    gh = FakeGitHub([search_item(1), search_item(2), search_item(1)])
    result = collector(store, settings, gh).discover()
    assert (result["repos_seen"], result["repos_new"]) == (2, 2)
    assert collector(store, settings, gh).discover()["repos_new"] == 0
    assert [r.full_name for r in store.repos()] == ["owner2/repo2", "owner1/repo1"]


def test_discover_stops_querying_at_max_repos_per_run(store, settings):
    from rising.config import SearchFilter

    filters = [SearchFilter(name="a", topics=["llm"]), SearchFilter(name="b", topics=["mcp"])]
    gh = FakeGitHub([search_item(i) for i in range(1, 5)])
    result = collector(store, settings, gh, filters=filters, max_repos_per_run=2).discover()
    assert result["repos_seen"] == 2
    assert len(gh.queries) == 1  # the second filter is not searched once the cap is reached


def test_discover_skips_a_query_github_rejects(store, settings):
    gh = FakeGitHub(search_error=GitHubError(422, "Validation Failed"))
    assert collector(store, settings, gh).discover()["invalid_queries"] == 1


def test_backfill_keeps_days_since_creation_and_stops_when_endpoint_is_unavailable(store, settings):
    today = utcnow().date()
    week = [StarDay(day=today - timedelta(days=d), stars_added=d + 1) for d in range(7)]
    gh = FakeGitHub([search_item(1, created_hours_ago=30)], history=lambda name: week)
    c = collector(store, settings, gh)
    c.discover()
    assert c.backfill()["fetched"] == 1
    created = store.repo("owner1/repo1").created_at.date()
    assert [d.day for d in store.star_history(1)] == sorted(d.day for d in week if d.day >= created)

    refused = FakeGitHub([search_item(i) for i in range(10, 15)], history=lambda name: None)
    c = collector(store, settings, refused)
    c.discover()
    result = c.backfill()
    assert result["unavailable"] == 3
    assert len(refused.history_calls) == 3  # three refusals in a row stop the run


def test_backfill_continues_past_a_failing_repository(store, settings):
    def history(name):
        if name == "owner20/repo20":
            raise GitHubError(500, "boom")
        return []

    gh = FakeGitHub([search_item(i) for i in (20, 21, 22)], history=history)
    c = collector(store, settings, gh)
    c.discover()
    result = c.backfill()
    assert (result["fetched"], result["failed"]) == (2, 1)


def test_snapshot_records_measurements_with_their_run(store, settings):
    gh = FakeGitHub([search_item(1), search_item(2)])
    c = collector(store, settings, gh)
    c.discover()
    result = c.snapshot()
    assert (result["tracked"], result["snapshots_inserted"], result["missing"]) == (2, 2, 0)
    assert store.snapshots(1)[0].run_id == result["run_id"]


def test_snapshot_fails_when_no_repository_could_be_measured(store, settings):
    gh = FakeGitHub([search_item(1)], graphql=lambda ids: {"data": None, "errors": [{"m": "x"}]})
    c = collector(store, settings, gh)
    c.discover()
    with pytest.raises(RuntimeError, match="no repository could be measured"):
        c.snapshot()
    assert store.runs(1)[0].status == "failed"


def test_collect_runs_every_step_even_if_one_fails(store, settings):
    gh = FakeGitHub(search_error=GitHubError(500, "boom"))
    with pytest.raises(GitHubError):
        collector(store, settings, gh).collect()
    assert [r.kind for r in store.runs()] == ["snapshot", "backfill", "discover"]
