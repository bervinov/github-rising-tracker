from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from rising.domain.models import StarDay
from rising.domain.ranking import rank
from rising.pipeline.current_ranking import current_ranking
from rising.storage import Store

from .conftest import GOOD, NOW, snap


def test_snapshots_are_append_only_in_the_database(store):
    store.upsert_repo(GOOD, "ai", NOW)
    run = store.start_run("snapshot")
    assert store.append_snapshots([snap(1, NOW, 5)], run) == 1
    assert store.append_snapshots([snap(1, NOW, 999)], run) == 0
    assert store.snapshots(1)[0].stars == 5
    store.append_star_history(1, [StarDay(day=date(2026, 11, 12), stars_added=3)], NOW)
    for sql in (
        "UPDATE snapshots SET stars = 1",
        "DELETE FROM snapshots",
        "DELETE FROM star_history",
        (
            "INSERT OR REPLACE INTO snapshots (repo_id, captured_at, stars, forks) "
            "SELECT repo_id, captured_at, 0, 0 FROM snapshots"
        ),
    ):
        with pytest.raises(DBAPIError, match="append-only"), store.engine.begin() as c:
            c.execute(text(sql))
    assert store.snapshots(1)[0].stars == 5


def test_star_history_keeps_every_fetch_and_reads_latest(store):
    store.upsert_repo(GOOD, "ai", NOW)
    day = date(2026, 11, 12)
    store.append_star_history(1, [StarDay(day=day, stars_added=3)], NOW.replace(day=12))
    store.append_star_history(1, [StarDay(day=day, stars_added=4)], NOW)
    with store.engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM star_history")).scalar() == 2
    assert store.star_history(1) == [StarDay(day=day, stars_added=4)]


def test_round_trip_preserves_models(seeded):
    repo = seeded.repo("GOOD/AGENT")
    assert repo.full_name == "good/agent" and repo.matched_filters == ["ai"]
    assert repo.created_at.tzinfo is not None
    assert seeded.snapshots(1)[-1].captured_at == NOW


def test_upsert_refreshes_metadata_but_keeps_first_seen(store):
    assert store.upsert_repo(GOOD, "ai", NOW) is True
    later = NOW.replace(hour=18)
    assert (
        store.upsert_repo(GOOD.model_copy(update={"description": "new"}), "agents", later) is False
    )
    r = store.repo("good/agent")
    assert (r.description, r.first_seen_at, r.last_seen_at) == ("new", NOW, later)
    assert r.matched_filters == ["agents", "ai"]


def test_bulk_histories_feed_the_same_ranking(seeded, settings, histories):
    from_db = current_ranking(seeded, settings, NOW)
    in_memory = rank(histories, settings, NOW)
    assert all(m.latest.run_id is not None for m in from_db.repos)
    no_run = {"repos": {"__all__": {"latest": {"run_id"}}}}
    assert from_db.model_dump(exclude=no_run) == in_memory.model_dump(exclude=no_run)


def test_schema_is_created_once_with_its_triggers(db_url):
    Store(db_url).close()
    store = Store(db_url)
    with store.engine.connect() as c:
        triggers = {
            r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))
        }
    assert triggers == {
        "snapshots_no_update",
        "snapshots_no_delete",
        "star_history_no_update",
        "star_history_no_delete",
    }
    store.close()


def test_stores_opened_together_create_the_schema_once(db_url):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(8) as pool:
        stores = list(pool.map(lambda _: Store(db_url), range(8)))
    with stores[0].engine.connect() as c:
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
    for s in stores:
        s.close()
