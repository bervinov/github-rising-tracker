import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from rising.config import GitHubConfig, SearchFilter
from rising.domain.timeutil import to_iso
from rising.github.client import GitHubClient, GitHubError
from rising.github.search import RepoSearch, build_queries
from rising.github.snapshots import fetch_snapshots, parse_node

from .conftest import NOW


def _client(handler):
    slept: list[float] = []
    gh = GitHubClient(
        GitHubConfig(min_interval_s=0, search_min_interval_s=0),
        "t",
        sleep=slept.append,
        transport=httpx.MockTransport(handler),
    )
    return gh, slept


def test_secondary_rate_limit_honours_retry_after():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                403,
                headers={"retry-after": "7"},
                json={"message": "You have exceeded a secondary rate limit"},
            )
        return httpx.Response(200, json={"total_count": 0, "items": []})

    gh, slept = _client(handler)
    assert gh.search_repositories("x")["total_count"] == 0
    assert len(calls) == 2 and 8 in slept


def test_waits_for_reset_when_bucket_is_empty():
    def handler(request):
        return httpx.Response(
            200,
            json={"items": []},
            headers={
                "x-ratelimit-resource": "search",
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": "1000100",
            },
        )

    gh, slept = _client(handler)
    gh.clock = lambda: 1_000_000
    gh.search_repositories("a")
    gh.search_repositories("b")
    assert slept[-1] == pytest.approx(101)


def test_star_history_parsing_and_unavailability():
    week = 1794096000
    gh, _ = _client(
        lambda r: httpx.Response(
            200, json=[{"week": week, "total": 6, "days": [1, 2, 3, 0, 0, 0, 0]}]
        )
    )
    days = {d.day: d.stars_added for d in gh.star_history("a/b")}
    assert days[date(2026, 11, 8)] == 1 and days[date(2026, 11, 10)] == 3
    gone, _ = _client(lambda r: httpx.Response(404, json={"message": "Not Found"}))
    assert gone.star_history("a/b") == []  # the repository was deleted
    hidden, _ = _client(
        lambda r: httpx.Response(404 if r.url.path.endswith("/history") else 200, json={})
    )
    assert hidden.star_history("a/b") is None  # the repo exists, the endpoint is hidden
    blocked, _ = _client(lambda r: httpx.Response(451, json={"message": "Unavailable"}))
    assert blocked.star_history("a/b") == []  # this repo is blocked, the endpoint may work
    refused, _ = _client(
        lambda r: httpx.Response(403 if r.url.path.endswith("/history") else 200, json={})
    )
    assert refused.star_history("a/b") is None  # the endpoint is not available to this token


def test_star_history_parses_a_recorded_github_response():
    recorded = json.loads(
        (Path(__file__).parent / "fixtures/star_history_response.json").read_text()
    )
    gh, _ = _client(lambda r: httpx.Response(200, json=recorded))
    days = gh.star_history("FrigadeHQ/yogurt")
    assert len(days) == 7 * len(recorded)
    assert sum(d.stars_added for d in days) == sum(week["total"] for week in recorded)


def test_build_queries_makes_one_query_per_combination_of_list_values():
    f = SearchFilter(name="x", topics=["llm", "mcp"], languages=["Python"], min_stars=5)
    assert build_queries(f) == [
        'topic:llm language:"Python" stars:>=5 fork:false',
        'topic:mcp language:"Python" stars:>=5 fork:false',
    ]


class FakeSearchClient:
    def __init__(self):
        self.queries: list[tuple[str, int]] = []

    def search_repositories(self, q, page=1, per_page=100):
        self.queries.append((q, page))
        start, end = q.split("created:")[1].split("..")
        full = start.startswith("2026-11-06") and end.startswith("2026-11-13")
        total = 1500 if full else 750
        offset = 0 if start.startswith("2026-11-06") else 10_000
        items = [
            {
                "id": offset + (page - 1) * 100 + i,
                "node_id": "n",
                "full_name": "o/r",
                "html_url": "u",
                "owner": {"login": "o"},
                "created_at": to_iso(NOW - timedelta(days=1))
                if offset
                else to_iso(NOW - timedelta(days=5)),
            }
            for i in range(min(100, total - (page - 1) * 100))
        ]
        return {"total_count": total, "items": items}


def test_search_splits_windows_over_1000_results():
    fake = FakeSearchClient()
    search = RepoSearch(fake, timedelta(minutes=30))
    repos = list(search.created_between("topic:llm", NOW - timedelta(days=7), NOW))
    assert len({r.id for r in repos}) == 1500
    assert len({q for q, _ in fake.queries}) == 3
    assert search.stats["truncated_windows"] == 0


def test_parse_node_counts_authors_without_storing_identities():
    node = {
        "databaseId": 7,
        "stargazerCount": 10,
        "forkCount": 2,
        "pushedAt": None,
        "diskUsage": 1,
        "isArchived": False,
        "watchers": {"totalCount": 1},
        "openIssues": {"totalCount": 1},
        "closedIssues": {"totalCount": 0},
        "openPRs": {"totalCount": 0},
        "mergedPRs": {"totalCount": 3},
        "closedPRs": {"totalCount": 0},
        "releases": {"totalCount": 1},
        "latestRelease": {"tagName": "v1", "publishedAt": None},
        "defaultBranchRef": {
            "target": {
                "history": {
                    "totalCount": 4,
                    "nodes": [
                        {
                            "committedDate": "2026-11-13T10:00:00Z",
                            "author": {"name": "A", "user": {"login": "a"}},
                        },
                        {
                            "committedDate": "2026-11-12T10:00:00Z",
                            "author": {"name": "A2", "user": {"login": "a"}},
                        },
                        {
                            "committedDate": "2026-11-11T10:00:00Z",
                            "author": {"name": "Bob", "user": None},
                        },
                    ],
                }
            }
        },
    }
    s = parse_node(node, NOW)
    assert s.commit_authors_recent == 2 and s.commits == 4
    assert to_iso(s.last_commit_at) == "2026-11-13T10:00:00Z"
    assert "login" not in s.model_dump_json() and "Bob" not in s.model_dump_json()


def test_graphql_failure_is_split_down_to_single_repos_and_skipped():
    gh, _ = _client(
        lambda r: httpx.Response(200, json={"errors": [{"message": "timeout"}], "data": None})
    )
    stats: dict[str, int] = {}
    assert fetch_snapshots(gh, ["R_1", "R_2"], stats) == ([], 2)
    assert stats["splits"] == 1


def test_secondary_limit_on_graphql_gets_full_retries():
    responses = iter(
        [httpx.Response(403, headers={"retry-after": "5"}, text="secondary rate limit")] * 3
        + [httpx.Response(200, json={"data": {"nodes": []}})]
    )
    gh, slept = _client(lambda r: next(responses))
    assert gh.graphql("q", {}, server_error_retries=1) == {"data": {"nodes": []}}
    assert slept == [6.0, 6.0, 6.0]


def test_readme_is_optional_when_github_refuses():
    gh, _ = _client(lambda r: httpx.Response(451, json={"message": "Unavailable"}))
    assert gh.readme("a/b", 100) is None


def test_retry_after_given_as_an_http_date_is_honoured():
    responses = iter(
        [
            httpx.Response(429, headers={"retry-after": "Thu, 01 Jan 1970 00:01:40 GMT"}),
            httpx.Response(200, json={"items": [], "total_count": 0}),
        ]
    )
    gh, slept = _client(lambda r: next(responses))
    gh.clock = lambda: 40.0
    gh.search_repositories("q")
    assert slept[-1] == pytest.approx(61)  # 100 s mark minus clock 40 s, plus 1


def test_graphql_rate_limit_inside_a_200_waits_and_retries():
    limited = {
        "errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}],
        "data": {"rateLimit": {"cost": 1, "remaining": 0, "resetAt": "1970-01-01T00:02:00Z"}},
    }
    ok = {"data": {"rateLimit": {"cost": 1, "remaining": 4000, "resetAt": "1970-01-01T01:00:00Z"}}}
    responses = iter([httpx.Response(200, json=limited), httpx.Response(200, json=ok)])
    gh, slept = _client(lambda r: next(responses))
    gh.clock = lambda: 0.0
    assert gh.graphql("q", {}) == ok
    assert 121.0 in slept


def test_server_errors_give_up_after_the_retry_budget():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, text="unavailable")

    gh, _ = _client(handler)
    with pytest.raises(GitHubError) as err:
        gh.search_repositories("q")
    assert err.value.status == 503 and len(calls) == gh.cfg.max_retries + 1


def test_graphql_timeout_splits_the_batch_like_a_502():
    def handler(request):
        ids = json.loads(request.content)["variables"]["ids"]
        if len(ids) > 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"data": {"nodes": [None]}})

    gh, _ = _client(handler)
    stats: dict[str, int] = {}
    assert fetch_snapshots(gh, ["R_1", "R_2"], stats) == ([], 2)
    assert stats["splits"] == 1


def test_bot_commits_are_not_counted_as_authors():
    def commit(login: str) -> dict:
        return {
            "committedDate": "2026-11-13T10:00:00Z",
            "author": {"name": login, "user": {"login": login}},
        }

    node = {
        "databaseId": 1,
        "stargazerCount": 10,
        "forkCount": 0,
        "watchers": {"totalCount": 0},
        "openIssues": {"totalCount": 0},
        "closedIssues": {"totalCount": 0},
        "openPRs": {"totalCount": 0},
        "mergedPRs": {"totalCount": 0},
        "closedPRs": {"totalCount": 0},
        "releases": {"totalCount": 0},
        "latestRelease": None,
        "defaultBranchRef": {
            "target": {
                "history": {
                    "totalCount": 3,
                    "nodes": [
                        commit("alice"),
                        commit("dependabot[bot]"),
                        commit("github-actions[bot]"),
                    ],
                }
            }
        },
    }
    assert parse_node(node, NOW).commit_authors_recent == 1
