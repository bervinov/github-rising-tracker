"""Domain models shared by storage, the HTTP API, the CLI and the LLM tools."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    """Base for domain models: immutable, and unknown fields are an error."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Repo(_Model):
    """Descriptive metadata of a repository as returned by discovery."""

    id: int = Field(description="GitHub databaseId; stable across renames")
    node_id: str
    full_name: str
    html_url: str
    description: str | None = None
    homepage: str | None = None
    language: str | None = None
    topics: list[str] = Field(default_factory=list)
    license: str | None = None
    owner_login: str
    owner_type: str | None = None
    is_fork: bool = False
    created_at: datetime

    @property
    def name(self) -> str:
        """Repository name without the owner."""
        return self.full_name.split("/", 1)[1]


class TrackedRepo(Repo):
    """A repository plus our bookkeeping about it."""

    first_seen_at: datetime
    last_seen_at: datetime
    matched_filters: list[str] = Field(default_factory=list)
    star_history_status: Literal["ok", "unavailable"] | None = None
    star_history_checked_at: datetime | None = None


class Snapshot(_Model):
    """One timestamped measurement."""

    repo_id: int
    captured_at: datetime
    run_id: int | None = None
    stars: int
    forks: int
    watchers: int | None = None
    open_issues: int | None = None
    closed_issues: int | None = None
    open_prs: int | None = None
    merged_prs: int | None = None
    closed_prs: int | None = None
    releases: int | None = None
    commits: int | None = Field(None, description="Commits on the default branch")
    commit_authors_recent: int | None = Field(
        None, description="Distinct authors among the latest 50 commits"
    )
    last_commit_at: datetime | None = None
    latest_release_tag: str | None = None
    latest_release_at: datetime | None = None
    pushed_at: datetime | None = None
    disk_kb: int | None = None
    is_archived: bool | None = None


class StarDay(_Model):
    """Stars added on `day`."""

    day: date
    stars_added: int


RunKind = Literal["discover", "snapshot", "backfill", "digest"]
RunStatus = Literal["running", "ok", "failed"]


class Run(_Model):
    """One execution of a job (discover, backfill, snapshot, digest) and its outcome."""

    id: int
    kind: RunKind
    started_at: datetime
    finished_at: datetime | None = None
    status: RunStatus
    details: dict[str, Any] = Field(default_factory=dict)


VelocitySource = Literal["snapshots", "star_history", "lifetime_average"]


class Velocity(_Model):
    """Stars per day and where the number came from."""

    source: VelocitySource
    stars_gained: int
    span_hours: float
    stars_per_day: float
    lifetime_stars_per_day: float


class Activity(_Model):
    """Signs of real use behind the stars, combined into one factor from 0 to 1."""

    issues_total: int
    prs_total: int
    factor: float = Field(description="Mean of the four 0..1 signals")
    signals: dict[str, float]


FlagCode = Literal[
    "clone_cluster",
    "star_spike",
    "stars_without_forks",
    "no_issues_or_prs",
    "single_author",
    "code_dump",
    "stale_while_growing",
    "archived",
    "is_fork",
]


class RedFlag(_Model):
    """A warning sign with the numbers that triggered it."""

    code: FlagCode
    message: str
    suspicious: bool = Field(False, description="Suspicious flags lower the score")
    penalty: float = Field(1.0, description="Factor applied to the quality multiplier; 1 = none")
    evidence: dict[str, Any] = Field(default_factory=dict)


class RepoHistory(_Model):
    """Everything the ranking needs about one repo."""

    repo: TrackedRepo
    snapshots: list[Snapshot] = Field(description="Ordered by captured_at")
    star_history: list[StarDay] = Field(default_factory=list)
    star_history_fetched_at: datetime | None = Field(
        None, description="When the history was fetched; the last day is complete only up to here"
    )


class RankedRepo(_Model):
    """One repository in the ranking, with everything that explains its place."""

    rank: int
    full_name: str
    url: str
    description: str | None
    language: str | None
    topics: list[str]
    license: str | None
    owner_type: str | None
    created_at: datetime
    age_hours: float
    matched_filters: list[str]
    snapshot_count: int
    latest: Snapshot
    velocity: Velocity
    activity: Activity
    quality_multiplier: float
    score: float
    red_flags: list[RedFlag]


class RankingMeta(_Model):
    """When and how a ranking was computed."""

    generated_at: datetime
    window_days: int
    velocity_window_hours: float
    min_stars: int
    candidates: int
    data_as_of: datetime | None = Field(description="Time of the newest measurement used")
    score_formula: str


class Ranking(_Model):
    """The ranked list of repositories plus its metadata."""

    meta: RankingMeta
    repos: list[RankedRepo]
