"""The Store: reads and writes domain models; the only module that runs SQL."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from time import sleep
from typing import Any

from sqlalchemy import ColumnElement, Engine, create_engine, event, func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from ..domain.models import (
    Repo,
    RepoHistory,
    Run,
    RunKind,
    RunStatus,
    Snapshot,
    StarDay,
    TrackedRepo,
)
from ..domain.timeutil import to_iso, utcnow
from . import tables


def make_engine(url: str) -> Engine:
    """SQLite engine with foreign keys on and a busy timeout."""
    u = make_url(url)
    if u.database and u.database != ":memory:":
        Path(u.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON")  # SQLite ignores foreign keys unless enabled
        cur.execute("PRAGMA busy_timeout = 30000")
        cur.execute(
            "PRAGMA recursive_triggers = ON"
        )  # INSERT OR REPLACE then fires DELETE triggers
        cur.close()

    return engine


def create_schema(engine: Engine, attempts: int = 10) -> None:
    """Create missing tables and enable WAL mode."""
    for attempt in range(1, attempts + 1):
        try:
            with engine.connect() as c:
                c.exec_driver_sql("PRAGMA journal_mode = WAL")
            tables.Base.metadata.create_all(engine)
            return
        except OperationalError as exc:
            # The scheduler and the API start together; one may briefly hold the file lock.
            transient = "locked" in str(exc) or "already exists" in str(exc)
            if not transient or attempt == attempts:
                raise
            sleep(0.1 * attempt)


def _jsonable(value: Any) -> Any:
    """datetimes/dates -> strings, recursively, for JSON columns."""
    if isinstance(value, datetime):
        return to_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


class Store:
    """Typed persistence for domain models."""

    def __init__(self, url: str):
        self.engine = make_engine(url)
        create_schema(self.engine)
        self._session = sessionmaker(self.engine, expire_on_commit=False)

    def close(self) -> None:
        """Close all database connections."""
        self.engine.dispose()

    def session(self) -> Session:
        """A new SQLAlchemy session."""
        return self._session()

    @staticmethod
    def _repo(row: tables.Repo) -> TrackedRepo:
        return TrackedRepo.model_validate(row, from_attributes=True)

    @staticmethod
    def _snapshot(row: tables.Snapshot) -> Snapshot:
        return Snapshot.model_validate(row, from_attributes=True)

    def start_run(self, kind: RunKind) -> int:
        """Record a job as running and return its id."""
        with self.session() as s, s.begin():
            run = tables.Run(kind=kind, started_at=utcnow(), status="running", details={})
            s.add(run)
            s.flush()
            return run.id

    def finish_run(self, run_id: int, status: RunStatus, details: dict[str, Any]) -> None:
        """Record how a job ended."""
        with self.session() as s, s.begin():
            s.execute(
                update(tables.Run)
                .where(tables.Run.id == run_id)
                .values(finished_at=utcnow(), status=status, details=_jsonable(details))
            )

    def runs(self, limit: int = 20) -> list[Run]:
        """Latest job runs, newest first."""
        with self.session() as s:
            rows = s.scalars(select(tables.Run).order_by(tables.Run.id.desc()).limit(limit))
            return [Run.model_validate(r, from_attributes=True) for r in rows]

    def upsert_repo(self, repo: Repo, filter_name: str, seen_at: datetime) -> bool:
        """Insert a repo or refresh its description; returns True if it was not stored before.

        Matched filter names are merged; id, creation time and first-seen time never change.
        """
        with self.session() as s, s.begin():
            prev = s.scalar(select(tables.Repo.matched_filters).where(tables.Repo.id == repo.id))
            values = repo.model_dump() | {
                "first_seen_at": seen_at,
                "last_seen_at": seen_at,
                "matched_filters": sorted(set(prev or []) | {filter_name}),
            }
            stmt = insert(tables.Repo).values(**values)
            keep = {"id", "created_at", "first_seen_at"}  # facts that never change on refresh
            s.execute(
                stmt.on_conflict_do_update(
                    index_elements=[tables.Repo.id],
                    set_={k: stmt.excluded[k] for k in values if k not in keep},
                )
            )
        return prev is None

    def tracked(self, created_after: datetime) -> list[TrackedRepo]:
        """Repositories created after `created_after`, newest first."""
        q = (
            select(tables.Repo)
            .where(tables.Repo.created_at >= created_after)
            .order_by(tables.Repo.created_at.desc())
        )
        with self.session() as s:
            return [self._repo(r) for r in s.scalars(q)]

    def repo(self, full_name: str) -> TrackedRepo | None:
        """One repository by owner/name (case-insensitive), or None."""
        q = select(tables.Repo).where(func.lower(tables.Repo.full_name) == full_name.lower())
        with self.session() as s:
            row = s.scalars(q).first()
            return self._repo(row) if row else None

    def repos(
        self, limit: int = 100, offset: int = 0, language: str | None = None
    ) -> list[TrackedRepo]:
        """Tracked repositories, newest first, optionally filtered by language."""
        q = select(tables.Repo).order_by(tables.Repo.created_at.desc()).limit(limit).offset(offset)
        if language:
            q = q.where(func.lower(tables.Repo.language) == language.lower())
        with self.session() as s:
            return [self._repo(r) for r in s.scalars(q)]

    def set_star_history_status(self, repo_id: int, status: str, at: datetime) -> None:
        """Remember whether star history could be fetched and when."""
        with self.session() as s, s.begin():
            s.execute(
                update(tables.Repo)
                .where(tables.Repo.id == repo_id)
                .values(star_history_status=status, star_history_checked_at=at)
            )

    def append_snapshots(self, snaps: Iterable[Snapshot], run_id: int) -> int:
        """Insert measurements; a duplicate (repo_id, captured_at) is skipped, never replaced."""
        rows = [s.model_dump() | {"run_id": run_id} for s in snaps]
        if not rows:
            return 0
        stmt = (
            insert(tables.Snapshot)
            .on_conflict_do_nothing(
                index_elements=[tables.Snapshot.repo_id, tables.Snapshot.captured_at]
            )
            .returning(tables.Snapshot.id)
        )
        with self.session() as s, s.begin():
            return len(s.execute(stmt, rows).all())

    def snapshots(self, repo_id: int, since: datetime | None = None) -> list[Snapshot]:
        """A repository's measurements in time order."""
        q = select(tables.Snapshot).where(tables.Snapshot.repo_id == repo_id)
        if since:
            q = q.where(tables.Snapshot.captured_at >= since)
        with self.session() as s:
            return [self._snapshot(r) for r in s.scalars(q.order_by(tables.Snapshot.captured_at))]

    def append_star_history(self, repo_id: int, days: list[StarDay], fetched_at: datetime) -> int:
        """Store one star-history fetch; returns how many rows were new."""
        if not days:
            return 0
        rows = [d.model_dump() | {"repo_id": repo_id, "fetched_at": fetched_at} for d in days]
        stmt = insert(tables.StarHistory).on_conflict_do_nothing().returning(tables.StarHistory.day)
        with self.session() as s, s.begin():
            return len(s.execute(stmt, rows).all())

    def _latest_star_history(
        self, *where: ColumnElement[bool]
    ) -> dict[int, tuple[list[StarDay], datetime]]:
        """Star history from the latest fetch per repo, with the time of that fetch."""
        h = tables.StarHistory
        latest = (
            select(h.repo_id, func.max(h.fetched_at).label("fetched_at"))
            .group_by(h.repo_id)
            .subquery()
        )
        q = (
            select(h)
            .join(latest, (latest.c.repo_id == h.repo_id) & (latest.c.fetched_at == h.fetched_at))
            .where(*where)
            .order_by(h.repo_id, h.day)
        )
        out: dict[int, tuple[list[StarDay], datetime]] = {}
        with self.session() as s:
            for r in s.scalars(q):
                days, _ = out.setdefault(r.repo_id, ([], r.fetched_at))
                days.append(StarDay(day=r.day, stars_added=r.stars_added))
        return out

    def star_history(self, repo_id: int) -> list[StarDay]:
        """A repository's stars per day from the latest fetch."""
        found = self._latest_star_history(tables.StarHistory.repo_id == repo_id).get(repo_id)
        return found[0] if found else []

    def histories(self, created_after: datetime) -> list[RepoHistory]:
        """Load everything the ranking needs."""
        candidates = self.tracked(created_after)
        ids = select(tables.Repo.id).where(tables.Repo.created_at >= created_after)
        q = (
            select(tables.Snapshot)
            .where(tables.Snapshot.repo_id.in_(ids))
            .order_by(tables.Snapshot.repo_id, tables.Snapshot.captured_at)
        )
        by_repo: dict[int, list[Snapshot]] = defaultdict(list)
        with self.session() as s:
            for r in s.scalars(q):
                by_repo[r.repo_id].append(self._snapshot(r))
        history = self._latest_star_history(tables.StarHistory.repo_id.in_(ids))
        return [
            RepoHistory(
                repo=r,
                snapshots=by_repo.get(r.id, []),
                star_history=history[r.id][0] if r.id in history else [],
                star_history_fetched_at=history[r.id][1] if r.id in history else None,
            )
            for r in candidates
        ]
