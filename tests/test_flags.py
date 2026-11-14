from dataclasses import replace
from datetime import date, timedelta

import pytest

from rising.config import RedFlagConfig
from rising.domain import flags
from rising.domain.models import Activity, StarDay, Velocity
from rising.domain.ranking import rank

from .conftest import GOOD, NOW, snap, tracked

CFG = RedFlagConfig()


def ctx(
    stars=100,
    forks=10,
    commits=20,
    authors=3,
    issues=0,
    prs=0,
    history=(),
    pushed_days_ago=0.0,
    velocity=10.0,
    same_description=1,
    archived=False,
    cfg=CFG,
):
    latest = snap(
        1,
        NOW,
        stars,
        forks,
        commits=commits,
        authors=authors,
        pushed_at=NOW - timedelta(days=pushed_days_ago),
    )
    latest = latest.model_copy(update={"is_archived": archived})
    return flags.FlagContext(
        repo=tracked(GOOD),
        latest=latest,
        velocity=Velocity(
            source="snapshots",
            stars_gained=1,
            span_hours=24,
            stars_per_day=velocity,
            lifetime_stars_per_day=velocity,
        ),
        activity=Activity(issues_total=issues, prs_total=prs, factor=0.5, signals={}),
        history=list(history),
        cfg=cfg,
        now=NOW,
        same_description=same_description,
    )


def codes(c) -> set[str]:
    return {f.code for f in flags.detect(c, flags.RULES)}


@pytest.mark.parametrize(
    ("rule", "fires", "quiet", "evidence"),
    [
        (
            flags.StarsWithoutForks(),
            ctx(stars=500, forks=2, issues=1),
            ctx(stars=500, forks=50),
            {"stars": 500, "forks": 2, "fork_to_star_pct": 0.4},
        ),
        (
            flags.NoIssuesOrPrs(),
            ctx(issues=0, prs=0),
            ctx(issues=1),
            {"stars": 100, "issues_total": 0, "prs_total": 0},
        ),
        (flags.SingleAuthor(), ctx(authors=1), ctx(authors=2), {"commit_authors_recent": 1}),
        (flags.CodeDump(), ctx(commits=2), ctx(commits=4), {"commits": 2}),
        (
            flags.StaleWhileGrowing(),
            ctx(pushed_days_ago=4),
            ctx(pushed_days_ago=1),
            {"days_since_push": 4.0, "pushed_at": "2026-11-09T12:00:00Z"},
        ),
        (
            flags.StarSpike(),
            ctx(
                history=[
                    StarDay(day=date(2026, 11, 10), stars_added=5),
                    StarDay(day=date(2026, 11, 11), stars_added=90),
                    StarDay(day=date(2026, 11, 12), stars_added=5),
                ]
            ),
            ctx(
                history=[
                    StarDay(day=date(2026, 11, 10), stars_added=40),
                    StarDay(day=date(2026, 11, 11), stars_added=30),
                    StarDay(day=date(2026, 11, 12), stars_added=30),
                ]
            ),
            {
                "peak_day": "2026-11-11",
                "peak_day_stars": 90,
                "history_stars": 100,
                "peak_share_pct": 90.0,
            },
        ),
        (
            flags.CloneCluster(),
            ctx(same_description=4),
            ctx(same_description=2),
            {"repos_with_same_description": 4, "repos_with_similar_name_and_stars": 1},
        ),
        (flags.Archived(), ctx(archived=True), ctx(), {}),
    ],
    ids=lambda v: v.code if isinstance(v, flags.Rule) else "",
)
def test_rule_fires_with_evidence_and_stays_quiet(rule, fires, quiet, evidence):
    flag = rule.check(fires)
    assert flag is not None and flag.code == rule.code
    assert flag.evidence == evidence
    assert flag.suspicious is rule.suspicious
    assert rule.check(quiet) is None


def test_attention_gate_applies_only_to_attention_rules():
    small = ctx(stars=10, forks=0, commits=1, authors=1, same_description=5)
    assert codes(small) == {"clone_cluster"}


def test_only_spike_and_clone_cluster_are_suspicious():
    assert {r.code for r in flags.RULES if r.suspicious} == {"star_spike", "clone_cluster"}


def test_registry_is_complete_and_unique():
    from typing import get_args

    from rising.domain.models import FlagCode

    assert set(flags.REGISTRY) == set(get_args(FlagCode))
    assert len(flags.REGISTRY) == len(flags.RULES)


def test_disabled_rules_are_skipped_and_typos_fail_fast(histories, settings):
    def hype(ranking):
        return next(m for m in ranking.repos if m.full_name == "hype/tool")

    assert "star_spike" in {f.code for f in hype(rank(histories, settings, NOW)).red_flags}

    off = settings.model_copy(update={"red_flags": RedFlagConfig(disabled=["star_spike"])})
    ranked = rank(histories, off, NOW)
    assert "star_spike" not in {f.code for f in hype(ranked).red_flags}

    typo = settings.model_copy(update={"red_flags": RedFlagConfig(disabled=["star_spik"])})
    with pytest.raises(ValueError, match="unknown codes"):
        rank(histories, typo, NOW)


def test_star_spike_waits_until_a_repo_is_a_few_days_old():
    # Created late on day 1: three history entries, but only 46 hours old.
    history = [
        StarDay(day=date(2026, 11, 11), stars_added=0),
        StarDay(day=date(2026, 11, 12), stars_added=248),
        StarDay(day=date(2026, 11, 13), stars_added=141),
    ]

    def aged(hours):
        c = ctx(history=history)
        repo = c.repo.model_copy(update={"created_at": NOW - timedelta(hours=hours)})
        return replace(c, repo=repo)

    assert flags.StarSpike().check(aged(46)) is None
    assert flags.StarSpike().check(aged(80)) is not None


def test_single_author_needs_a_known_author_count():
    assert flags.SingleAuthor().check(ctx(authors=None)) is None


def test_only_bot_commits_are_not_a_single_author():
    assert flags.SingleAuthor().check(ctx(authors=0)) is None
