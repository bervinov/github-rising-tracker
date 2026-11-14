"""Database tables as SQLAlchemy models, with append-only triggers."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, ClassVar

from sqlalchemy import DDL, JSON, ForeignKey, String, Text, UniqueConstraint, event
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """UTC timestamp stored as ISO-8601 text."""

    impl = String(20)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> str | None:
        """Python value to database text."""
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime; all timestamps must be UTC-aware")
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def process_result_value(self, value: str | None, dialect: Dialect) -> datetime | None:
        """Database text to a UTC datetime."""
        if value is None:
            return None
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    """Base class for the tables; maps Python types to column types."""

    type_annotation_map: ClassVar[dict[Any, Any]] = {
        str: Text,
        datetime: UTCDateTime,
        list[str]: JSON,
        dict[str, Any]: JSON,
    }


class Repo(Base):
    """A discovered repository: current description, refreshed on rediscovery."""

    __tablename__ = "repos"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    node_id: Mapped[str] = mapped_column(unique=True)
    full_name: Mapped[str] = mapped_column(index=True)
    html_url: Mapped[str]
    description: Mapped[str | None]
    homepage: Mapped[str | None]
    language: Mapped[str | None]
    topics: Mapped[list[str]] = mapped_column(default=list)
    license: Mapped[str | None]
    owner_login: Mapped[str]
    owner_type: Mapped[str | None]
    is_fork: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(index=True)
    first_seen_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    matched_filters: Mapped[list[str]] = mapped_column(default=list)
    star_history_status: Mapped[str | None]
    star_history_checked_at: Mapped[datetime | None]


class Run(Base):
    """Audit log of job executions."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str]
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(default="running")
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)


class Snapshot(Base):
    """One timestamped measurement of a repository; never updated or deleted."""

    __tablename__ = "snapshots"
    __table_args__ = (UniqueConstraint("repo_id", "captured_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id"))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"))
    captured_at: Mapped[datetime]
    stars: Mapped[int]
    forks: Mapped[int]
    watchers: Mapped[int | None]
    open_issues: Mapped[int | None]
    closed_issues: Mapped[int | None]
    open_prs: Mapped[int | None]
    merged_prs: Mapped[int | None]
    closed_prs: Mapped[int | None]
    releases: Mapped[int | None]
    commits: Mapped[int | None]
    commit_authors_recent: Mapped[int | None]
    last_commit_at: Mapped[datetime | None]
    latest_release_tag: Mapped[str | None]
    latest_release_at: Mapped[datetime | None]
    pushed_at: Mapped[datetime | None]
    disk_kb: Mapped[int | None]
    is_archived: Mapped[bool | None]


class StarHistory(Base):
    """Stars added per day from GitHub's star-history endpoint; one set per fetch."""

    __tablename__ = "star_history"

    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id"), primary_key=True)
    day: Mapped[date] = mapped_column(primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(primary_key=True)
    stars_added: Mapped[int]


# The database itself rejects UPDATE and DELETE on measurements, so history cannot be rewritten.
for _model in (Snapshot, StarHistory):
    _table = _model.__tablename__
    for _action in ("UPDATE", "DELETE"):
        event.listen(
            _model.__table__,
            "after_create",
            DDL(
                f"CREATE TRIGGER {_table}_no_{_action.lower()} BEFORE {_action} ON {_table} "
                f"BEGIN SELECT RAISE(ABORT, '{_table} is append-only'); END"
            ),
        )
