"""Settings loaded from config.yaml and validated with Pydantic."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_TOP_N = 50  # the digest tools return at most this many repositories


class UserError(RuntimeError):
    """A problem the user can fix (setup, data); the CLI prints it as one line."""


class MissingSecret(UserError):
    """A required token or API key is not set in the environment."""


class _Strict(BaseModel):
    """Base for config sections: unknown keys are an error, so typos fail fast."""

    model_config = ConfigDict(extra="forbid")


class SearchFilter(_Strict):
    """One discovery profile."""

    name: str
    keywords: list[str] = Field(
        default_factory=list, description="Free-text terms, matched in name/description/topics"
    )
    topics: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    min_stars: int = Field(0, ge=0)
    include_forks: bool = False
    extra_qualifiers: str = Field("", description="Raw GitHub search qualifiers appended as-is")

    @model_validator(mode="after")
    def _has_criteria(self) -> SearchFilter:
        if not (
            self.keywords
            or self.topics
            or self.languages
            or self.min_stars
            or self.extra_qualifiers
        ):
            raise ValueError(f"filter {self.name!r} has no criteria and would match all of GitHub")
        return self


class DiscoveryConfig(_Strict):
    """What to search for: the creation window and the search filters."""

    window_days: int = Field(7, ge=1, le=30)
    filters: list[SearchFilter]
    min_split_window_minutes: int = Field(
        30, ge=1, description="Stop splitting the created: window below this"
    )
    max_repos_per_run: int = Field(2000, ge=1)

    @model_validator(mode="after")
    def _unique_names(self) -> DiscoveryConfig:
        names = [f.name for f in self.filters]
        if len(names) != len(set(names)):
            raise ValueError(f"filter names must be unique: {names}")
        return self


class GitHubConfig(_Strict):
    """How to talk to the GitHub API: token, version, pacing and retries."""

    token_env: str = "GITHUB_TOKEN"
    api_url: str = "https://api.github.com"
    api_version: str = "2026-03-10"
    graphql_batch_size: int = Field(25, ge=1, le=100)
    search_min_interval_s: float = Field(
        2.1, ge=0, description="30 search req/min -> >= 2s between calls"
    )
    min_interval_s: float = Field(
        0.25, ge=0, description="Pause between any two requests (no concurrency)"
    )
    max_retries: int = Field(6, ge=0)
    timeout_s: float = 30.0

    @property
    def token(self) -> str:
        """The token from the environment variable named by `token_env`."""
        token = os.environ.get(self.token_env, "").strip()
        if not token:
            raise MissingSecret(f"GitHub token missing: set ${self.token_env} (see .env.example)")
        return token


class TrackingConfig(_Strict):
    """How long and how often tracked repositories are measured."""

    track_days: int = Field(
        30, ge=1, description="Keep snapshotting a repo this many days after creation"
    )
    snapshot_interval_minutes: int = Field(60, ge=5)
    discovery_interval_minutes: int = Field(360, ge=10)
    backfill_star_history: bool = Field(
        True, description="Use GET /repos/{o}/{r}/stargazers/history once per repo"
    )


class RankingConfig(_Strict):
    """Thresholds for the star-velocity calculation."""

    velocity_window_hours: float = Field(24, gt=0)
    min_stars: int = Field(10, ge=0)
    min_rate_hours: float = Field(
        24, gt=0, description="Never extrapolate a per-day rate from a shorter span"
    )


class RedFlagConfig(_Strict):
    """Thresholds for the red-flag rules, and which rules are switched off."""

    min_stars_to_judge: int = Field(50, ge=1)
    max_fork_ratio: float = Field(0.01, ge=0, le=1)
    max_commits_for_code_dump: int = 3
    spike_share: float = Field(0.5, gt=0, le=1)
    spike_penalty: float = Field(0.5, gt=0, le=1, description="Score factor for star_spike")
    clone_penalty: float = Field(0.1, gt=0, le=1, description="Score factor for clone_cluster")
    spike_min_days: int = Field(3, ge=2)
    stale_push_days: float = 3
    clone_cluster_min: int = Field(
        3, ge=2, description="Repos sharing a description, or a templated name and star count"
    )
    clone_name_overlap: float = Field(
        0.6, gt=0, le=1, description="Share of name words two repos must have in common"
    )
    clone_star_ratio: float = Field(
        1.25, ge=1, description="How far apart two clones' star counts may be (larger/smaller)"
    )
    disabled: list[str] = Field(
        default_factory=list, description="Rule codes to switch off, e.g. [stale_while_growing]"
    )


class LLMConfig(_Strict):
    """Model and limits for the digest agent."""

    model: str = "gpt-6.1-sol"
    api_key_env: str = "OPENAI_API_KEY"
    top_n: int = Field(10, ge=1, le=MAX_TOP_N)
    intent: str = "Weekly early-signal digest for a tech lead"
    language: str = "English"
    max_tool_rounds: int = Field(12, ge=1)
    max_repair_attempts: int = Field(2, ge=0)
    readme_chars: int = Field(3000, ge=0)


class Settings(_Strict):
    """The whole configuration file."""

    database_path: Path = Path("data/rising.db")
    digest_dir: Path = Path("data/digests")
    github: GitHubConfig = GitHubConfig()
    discovery: DiscoveryConfig
    tracking: TrackingConfig = TrackingConfig()
    ranking: RankingConfig = RankingConfig()
    red_flags: RedFlagConfig = RedFlagConfig()
    llm: LLMConfig = LLMConfig()

    @field_validator("database_path", "digest_dir")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return Path(os.path.expandvars(str(v))).expanduser()

    @property
    def database_url(self) -> str:
        """SQLAlchemy URL of the SQLite file."""
        return f"sqlite:///{self.database_path}"


def load_settings(path: str | Path | None = None) -> Settings:
    """Read and validate config.yaml (path from the argument or $RISING_CONFIG)."""
    path = Path(path or os.environ.get("RISING_CONFIG") or "config.yaml")
    if not path.is_file():
        raise FileNotFoundError(f"Config not found: {path}")
    data = yaml.safe_load(path.read_text()) or {}
    return Settings.model_validate(data)
