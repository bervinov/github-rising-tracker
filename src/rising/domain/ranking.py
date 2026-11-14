"""Turn repository histories into a ranking."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta

from ..config import Settings
from . import flags as flag_rules
from . import scoring
from .models import RankedRepo, Ranking, RankingMeta, RepoHistory
from .timeutil import hours_between

# Shorter descriptions ("AI agent", "MCP server") are shared by unrelated projects by chance.
MIN_CLONE_DESCRIPTION_CHARS = 20


def _desc_key(h: RepoHistory) -> str:
    """Normalised description, or "" when it is too short to identify a clone."""
    text = " ".join((h.repo.description or "").lower().split())
    return text if len(text) >= MIN_CLONE_DESCRIPTION_CHARS else ""


def _stars(h: RepoHistory) -> int:
    return h.snapshots[-1].stars if h.snapshots else 0


def _name_words(h: RepoHistory) -> frozenset[str]:
    return frozenset(w for w in re.split(r"[^a-z0-9]+", h.repo.name.lower()) if w)


def _similar_name_counts(histories: list[RepoHistory], overlap: float, ratio: float) -> list[int]:
    """How many repos (itself included) share most of each repo's name words and its star count."""
    # Spam networks reuse a name template ("blender-ai-copilot-flow", "-hub", "-lab") and buy
    # about the same number of stars for every copy; sorting by stars keeps the scan short.
    # Names of one or two words ("ai-agent") are too generic to tell a template from chance.
    words = [_name_words(h) for h in histories]
    stars = [_stars(h) for h in histories]
    order = sorted(range(len(histories)), key=lambda i: stars[i])
    counts = [1] * len(histories)
    for pos, i in enumerate(order):
        for j in order[pos + 1 :]:
            if stars[j] > max(stars[i], 1) * ratio:
                break
            if len(words[i]) < 3 or len(words[j]) < 3:
                continue
            if len(words[i] & words[j]) / len(words[i] | words[j]) >= overlap:
                counts[i] += 1
                counts[j] += 1
    return counts


def evaluate(
    h: RepoHistory,
    settings: Settings,
    now: datetime,
    rules: tuple[flag_rules.Rule, ...],
    same_description: int = 1,
    similar_name: int = 1,
) -> RankedRepo:
    """Score one repository: velocity, activity, red flags and the final score."""
    repo, snaps, history = h.repo, h.snapshots, h.star_history
    latest = snaps[-1]
    velocity = scoring.star_velocity(
        snaps, history, repo.created_at, now, settings.ranking, h.star_history_fetched_at
    )
    act = scoring.activity(latest)
    ctx = flag_rules.FlagContext(
        repo=repo,
        latest=latest,
        velocity=velocity,
        activity=act,
        history=history,
        cfg=settings.red_flags,
        now=now,
        same_description=same_description,
        similar_name=similar_name,
    )
    found = flag_rules.detect(ctx, rules)
    quality = scoring.quality_multiplier(act, found)
    return RankedRepo(
        rank=0,
        full_name=repo.full_name,
        url=repo.html_url,
        description=repo.description,
        language=repo.language,
        topics=repo.topics,
        license=repo.license,
        owner_type=repo.owner_type,
        created_at=repo.created_at,
        age_hours=round(hours_between(repo.created_at, now), 1),
        matched_filters=repo.matched_filters,
        snapshot_count=len(snaps),
        latest=latest,
        velocity=velocity,
        activity=act,
        quality_multiplier=quality,
        score=scoring.score(velocity, quality),
        red_flags=found,
    )


def rank(histories: list[RepoHistory], settings: Settings, now: datetime) -> Ranking:
    """Rank all candidates."""
    rules = flag_rules.active_rules(settings.red_flags)
    # Clone networks are counted over every candidate in the window, not only the ranked ones.
    by_description: dict[str, list[RepoHistory]] = defaultdict(list)
    for h in histories:
        if key := _desc_key(h):
            by_description[key].append(h)
    # Spam may copy the description of a project that is already trending. The original is the
    # repo with clearly more stars than every copy; copies bought in bulk all look alike.
    cfg = settings.red_flags
    originals = set()
    for group in by_description.values():
        ordered = sorted(group, key=_stars, reverse=True)
        if len(ordered) > 1 and _stars(ordered[0]) > _stars(ordered[1]) * cfg.clone_star_ratio:
            originals.add(ordered[0].repo.id)
    # A repo that stopped returning measurements (deleted, made private) must not keep ranking on
    # old data: only repos measured in the last few snapshot rounds are ranked.
    newest = max((h.snapshots[-1].captured_at for h in histories if h.snapshots), default=None)
    fresh_after = (
        newest - timedelta(minutes=3 * settings.tracking.snapshot_interval_minutes)
        if newest
        else None
    )
    similar = _similar_name_counts(histories, cfg.clone_name_overlap, cfg.clone_star_ratio)
    scored = [
        evaluate(
            h,
            settings,
            now,
            rules,
            1 if h.repo.id in originals else len(by_description.get(_desc_key(h), [h])),
            similar[i],
        )
        for i, h in enumerate(histories)
        if h.snapshots
        and h.snapshots[-1].stars >= settings.ranking.min_stars
        and h.snapshots[-1].captured_at >= fresh_after
    ]
    scored.sort(key=lambda m: (m.score, m.latest.stars), reverse=True)
    ranked = [m.model_copy(update={"rank": i}) for i, m in enumerate(scored, 1)]
    meta = RankingMeta(
        generated_at=now,
        window_days=settings.discovery.window_days,
        velocity_window_hours=settings.ranking.velocity_window_hours,
        min_stars=settings.ranking.min_stars,
        candidates=len(ranked),
        data_as_of=max((m.latest.captured_at for m in ranked), default=None),
        score_formula=scoring.SCORE_FORMULA,
    )
    return Ranking(meta=meta, repos=ranked)
