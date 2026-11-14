"""Star velocity, activity and the score: pure functions, no I/O."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from ..config import RankingConfig
from .models import Activity, RedFlag, Snapshot, StarDay, Velocity
from .timeutil import hours_between

SCORE_FORMULA = (
    "score = stars_per_day x quality_multiplier, where quality_multiplier = "
    "(0.2 + 0.8 x activity_factor) x the penalty of each suspicious red flag "
    "(star_spike x 0.5, clone_cluster x 0.1 by default). "
    "activity_factor is the mean of four 0..1 signals: forks relative to 2% of stars, issues+PRs "
    "(3 = full), distinct recent commit authors beyond the first (2 extra = full), commits "
    "(10 = full). stars_per_day is measured between our own snapshots over the trailing window "
    "when possible, otherwise from the official star-history endpoint, otherwise the lifetime "
    "average; rates are never extrapolated from less than one day."
)


def _round(x: float, nd: int = 2) -> float:
    return round(float(x), nd)


def _baseline(snaps: list[Snapshot], target: datetime) -> Snapshot:
    """Latest snapshot at or before `target`; the earliest one if none is that old."""
    base = snaps[0]
    for s in snaps:
        if s.captured_at > target:
            break
        base = s
    return base


def star_velocity(
    snaps: list[Snapshot],
    history: list[StarDay],
    created_at: datetime,
    now: datetime,
    cfg: RankingConfig,
    history_fetched_at: datetime | None = None,
) -> Velocity:
    """Stars per day, from the best source available (see SCORE_FORMULA)."""
    latest = snaps[-1]
    floor_h = cfg.min_rate_hours  # rates are never extrapolated from less than this many hours
    age_h = hours_between(created_at, latest.captured_at)
    lifetime = _round(latest.stars / max(age_h, floor_h) * 24)

    base = _baseline(snaps, latest.captured_at - timedelta(hours=cfg.velocity_window_hours))
    span_h = hours_between(base.captured_at, latest.captured_at)
    if span_h >= floor_h:
        gained = latest.stars - base.stars
        return Velocity(
            source="snapshots",
            stars_gained=gained,
            span_hours=_round(span_h, 1),
            stars_per_day=_round(gained / max(span_h, floor_h) * 24),
            lifetime_stars_per_day=lifetime,
        )

    today = now.date()
    yesterday = today - timedelta(days=1)
    recent = [d for d in history if d.day in (today, yesterday)]
    if recent:
        gained = sum(d.stars_added for d in recent)
        start = datetime.combine(yesterday, datetime.min.time(), tzinfo=now.tzinfo)
        # The counts cover only what had happened when the history was fetched.
        end = min(now, history_fetched_at) if history_fetched_at else now
        span_h = hours_between(max(start, created_at), end)
        return Velocity(
            source="star_history",
            stars_gained=gained,
            span_hours=_round(span_h, 1),
            stars_per_day=_round(gained / max(span_h, floor_h) * 24),
            lifetime_stars_per_day=lifetime,
        )

    return Velocity(
        source="lifetime_average",
        stars_gained=latest.stars,
        span_hours=_round(age_h, 1),
        stars_per_day=lifetime,
        lifetime_stars_per_day=lifetime,
    )


def activity(latest: Snapshot) -> Activity:
    """Four 0..1 signals of real activity (forks, issues and PRs, authors, commits)."""
    issues = (latest.open_issues or 0) + (latest.closed_issues or 0)
    prs = (latest.open_prs or 0) + (latest.merged_prs or 0) + (latest.closed_prs or 0)
    authors = latest.commit_authors_recent or 0
    signals = {
        "forks": min(1.0, latest.forks / max(1.0, 0.02 * latest.stars)),
        "issues_prs": min(1.0, (issues + prs) / 3),
        "contributors": min(1.0, max(authors - 1, 0) / 2),
        "commits": min(1.0, (latest.commits or 0) / 10),
    }
    return Activity(
        issues_total=issues,
        prs_total=prs,
        factor=_round(sum(signals.values()) / len(signals)),
        signals={k: _round(v) for k, v in signals.items()},
    )


def quality_multiplier(act: Activity, flags: list[RedFlag]) -> float:
    """0.2..1 from activity, times the penalty of each suspicious red flag."""
    penalties = math.prod(f.penalty for f in flags)
    # 0.2 keeps a new repo without activity yet in the ranking at a fifth of its velocity.
    return _round((0.2 + 0.8 * act.factor) * penalties, 3)


def score(velocity: Velocity, quality: float) -> float:
    """Stars per day weighted by quality."""
    return _round(velocity.stars_per_day * quality)
