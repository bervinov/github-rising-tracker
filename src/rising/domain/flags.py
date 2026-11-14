"""Red-flag rules: one small class per warning sign."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from ..config import RedFlagConfig
from .models import Activity, FlagCode, RedFlag, Snapshot, StarDay, TrackedRepo, Velocity
from .timeutil import hours_between, to_iso

FlagEvidence = dict[str, Any]


@dataclass(frozen=True)
class FlagContext:
    """Everything a rule may look at."""

    repo: TrackedRepo
    latest: Snapshot
    velocity: Velocity
    activity: Activity
    history: list[StarDay]
    cfg: RedFlagConfig
    now: datetime
    same_description: int = 1
    similar_name: int = 1


class Rule(ABC):
    """A red-flag rule: subclasses return the evidence that triggers them, or None."""

    code: ClassVar[FlagCode]
    message: ClassVar[str]
    suspicious: ClassVar[bool] = False
    requires_min_stars: ClassVar[bool] = True

    @abstractmethod
    def evidence(self, ctx: FlagContext) -> FlagEvidence | None:
        """Return the numbers that trigger this flag, or None if it does not apply."""

    def check(self, ctx: FlagContext) -> RedFlag | None:
        """Apply the rule, skipping repos with too few stars when the rule needs them."""
        if self.requires_min_stars and ctx.latest.stars < ctx.cfg.min_stars_to_judge:
            return None
        found = self.evidence(ctx)
        if found is None:
            return None
        return RedFlag(
            code=self.code,
            message=self.message,
            suspicious=self.suspicious,
            penalty=self.penalty(ctx.cfg),
            evidence=found,
        )

    def penalty(self, cfg: RedFlagConfig) -> float:
        """Factor this flag applies to the quality multiplier (1 = explains, no penalty)."""
        return 1.0


class CloneCluster(Rule):
    """Several repos in the window look like copies of each other: likely a spam network."""

    code = "clone_cluster"
    message = (
        "Several repos created in the window share this description, or a templated name with "
        "a near-identical star count (typical of star-farming or spam networks)"
    )
    suspicious = True
    requires_min_stars = False

    def penalty(self, cfg: RedFlagConfig) -> float:
        return cfg.clone_penalty

    def evidence(self, ctx: FlagContext) -> FlagEvidence | None:
        if max(ctx.same_description, ctx.similar_name) < ctx.cfg.clone_cluster_min:
            return None
        return {
            "repos_with_same_description": ctx.same_description,
            "repos_with_similar_name_and_stars": ctx.similar_name,
        }


class Archived(Rule):
    """The repository is archived."""

    code = "archived"
    message = "Repository is archived"
    requires_min_stars = False

    def evidence(self, ctx: FlagContext) -> FlagEvidence | None:
        return {} if ctx.latest.is_archived else None


class IsFork(Rule):
    """The repository is a fork."""

    code = "is_fork"
    message = "Repository is a fork"
    requires_min_stars = False

    def evidence(self, ctx: FlagContext) -> FlagEvidence | None:
        return {} if ctx.repo.is_fork else None


class StarSpike(Rule):
    """Most of the stars arrived on a single day."""

    code = "star_spike"
    message = "Most stars arrived on a single day"
    suspicious = True

    def penalty(self, cfg: RedFlagConfig) -> float:
        return cfg.spike_penalty

    def evidence(self, ctx: FlagContext) -> FlagEvidence | None:
        total = sum(d.stars_added for d in ctx.history)
        # A repo only a day or two old always has most of its stars on one day; its history has
        # partial first and last days, so the repo's age is checked, not the number of entries.
        too_young = hours_between(ctx.repo.created_at, ctx.now) < ctx.cfg.spike_min_days * 24
        if too_young or total < ctx.cfg.min_stars_to_judge:
            return None
        peak = max(ctx.history, key=lambda d: d.stars_added)
        share = peak.stars_added / total
        if share < ctx.cfg.spike_share:
            return None
        return {
            "peak_day": peak.day.isoformat(),
            "peak_day_stars": peak.stars_added,
            "history_stars": total,
            "peak_share_pct": round(share * 100, 2),
        }


class StarsWithoutForks(Rule):
    """Many stars but almost no forks."""

    code = "stars_without_forks"
    message = "Many stars but almost no forks"

    def evidence(self, ctx: FlagContext) -> FlagEvidence | None:
        ratio = ctx.latest.forks / ctx.latest.stars
        if ratio >= ctx.cfg.max_fork_ratio:
            return None
        return {
            "stars": ctx.latest.stars,
            "forks": ctx.latest.forks,
            "fork_to_star_pct": round(ratio * 100, 2),
        }


class NoIssuesOrPrs(Rule):
    """Attention but no issues or pull requests at all."""

    code = "no_issues_or_prs"
    message = "No issues or pull requests despite attention"

    def evidence(self, ctx: FlagContext) -> FlagEvidence | None:
        if ctx.activity.issues_total + ctx.activity.prs_total > 0:
            return None
        return {"stars": ctx.latest.stars, "issues_total": 0, "prs_total": 0}


class SingleAuthor(Rule):
    """All recent commits come from one author."""

    code = "single_author"
    message = "All recent commits come from one author"

    def evidence(self, ctx: FlagContext) -> FlagEvidence | None:
        authors = ctx.latest.commit_authors_recent
        if not authors or authors > 1:  # unknown, or only bots: not evidence of a single person
            return None
        return {"commit_authors_recent": authors}


class CodeDump(Rule):
    """Very few commits: the code was uploaded in one go."""

    code = "code_dump"
    message = "Very few commits: looks like a one-shot code dump"

    def evidence(self, ctx: FlagContext) -> FlagEvidence | None:
        commits = ctx.latest.commits
        if commits is None or commits > ctx.cfg.max_commits_for_code_dump:
            return None
        return {"commits": commits}


class StaleWhileGrowing(Rule):
    """Stars keep growing while nobody pushes code."""

    code = "stale_while_growing"
    message = "Stars keep growing but no pushes recently"

    def evidence(self, ctx: FlagContext) -> FlagEvidence | None:
        pushed = ctx.latest.pushed_at
        if not pushed or ctx.velocity.stars_per_day <= 0:
            return None
        idle_days = hours_between(pushed, ctx.now) / 24
        if idle_days < ctx.cfg.stale_push_days:
            return None
        return {"days_since_push": round(idle_days, 1), "pushed_at": to_iso(pushed)}


RULES: tuple[Rule, ...] = (
    CloneCluster(),
    StarSpike(),
    StarsWithoutForks(),
    NoIssuesOrPrs(),
    SingleAuthor(),
    CodeDump(),
    StaleWhileGrowing(),
    Archived(),
    IsFork(),
)
REGISTRY: dict[str, Rule] = {r.code: r for r in RULES}


def active_rules(cfg: RedFlagConfig) -> tuple[Rule, ...]:
    """Rules minus `cfg.disabled`."""
    unknown = set(cfg.disabled) - REGISTRY.keys()
    if unknown:
        raise ValueError(
            f"red_flags.disabled has unknown codes {sorted(unknown)}; known: {sorted(REGISTRY)}"
        )
    return tuple(r for r in RULES if r.code not in cfg.disabled)


def detect(ctx: FlagContext, rules: tuple[Rule, ...]) -> list[RedFlag]:
    """Run every rule and return the flags that fired."""
    return [flag for rule in rules if (flag := rule.check(ctx)) is not None]
