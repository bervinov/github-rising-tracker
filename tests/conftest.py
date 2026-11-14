from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta

import pytest

from rising.config import Settings
from rising.domain.models import Repo, RepoHistory, Snapshot, StarDay, TrackedRepo
from rising.storage import Store

NOW = datetime.fromisoformat("2026-11-13T12:00:00Z")


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "t.db"


@pytest.fixture
def db_url(db_path) -> str:
    return f"sqlite:///{db_path}"


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Settings for tests without a database."""
    return Settings.model_validate(
        {
            "database_path": str(tmp_path / "unused.db"),
            "digest_dir": str(tmp_path / "digests"),
            "discovery": {"filters": [{"name": "ai", "topics": ["llm"]}]},
            "llm": {"top_n": 2, "max_repair_attempts": 2},
        }
    )


def repo(rid: int, name: str, created: datetime, description: str | None = None) -> Repo:
    return Repo(
        id=rid,
        node_id=f"R_{rid}",
        full_name=name,
        html_url=f"https://github.com/{name}",
        description=description if description is not None else f"{name} does things",
        language="Python",
        topics=["llm"],
        license="MIT",
        owner_login=name.split("/")[0],
        owner_type="User",
        created_at=created,
    )


def tracked(r: Repo) -> TrackedRepo:
    return TrackedRepo(
        **r.model_dump(),
        first_seen_at=r.created_at,
        last_seen_at=r.created_at,
        matched_filters=["ai"],
    )


def snap(rid: int, at: datetime, stars: int, forks: int = 0, **kw) -> Snapshot:
    return Snapshot(
        repo_id=rid,
        captured_at=at,
        stars=stars,
        forks=forks,
        watchers=1,
        open_issues=kw.get("open_issues", 0),
        closed_issues=0,
        open_prs=0,
        merged_prs=kw.get("merged_prs", 0),
        closed_prs=0,
        releases=0,
        commits=kw.get("commits", 1),
        commit_authors_recent=kw.get("authors", 1),
        pushed_at=kw.get("pushed_at", at),
        is_archived=False,
    )


CREATED = NOW - timedelta(days=3)
GOOD = repo(1, "good/agent", CREATED)
HYPE = repo(2, "hype/tool", CREATED, description="Tool 2 for 10x speed")
GOOD_SNAPS = [
    snap(
        1, NOW - timedelta(hours=24), 400, 20, open_issues=12, merged_prs=8, commits=60, authors=5
    ),
    snap(1, NOW, 640, 35, open_issues=18, merged_prs=11, commits=75, authors=6),
]
HYPE_SNAPS = [
    snap(2, NOW - timedelta(hours=24), 900, 1, commits=2),
    snap(2, NOW, 1500, 2, commits=2, pushed_at=NOW - timedelta(days=3)),
]
HYPE_HISTORY = [
    StarDay(day=date(2026, 11, 10), stars_added=50),
    StarDay(day=date(2026, 11, 11), stars_added=1300),
    StarDay(day=date(2026, 11, 12), stars_added=150),
]


@pytest.fixture
def histories() -> list[RepoHistory]:
    """In-memory input for the domain layer."""
    return [
        RepoHistory(repo=tracked(GOOD), snapshots=GOOD_SNAPS),
        RepoHistory(repo=tracked(HYPE), snapshots=HYPE_SNAPS, star_history=HYPE_HISTORY),
    ]


@pytest.fixture
def db_settings(settings, db_path) -> Settings:
    """Settings pointing at the test database."""
    return settings.model_copy(update={"database_path": db_path})


@pytest.fixture
def store(db_url) -> Iterator[Store]:
    s = Store(db_url)
    yield s
    s.close()


@pytest.fixture
def seeded(store) -> Store:
    """The same two repos, persisted."""
    for r in (GOOD, HYPE):
        store.upsert_repo(r, "ai", r.created_at)
    run = store.start_run("snapshot")
    store.append_snapshots(GOOD_SNAPS + HYPE_SNAPS, run)
    store.append_star_history(2, HYPE_HISTORY, NOW)
    return store
