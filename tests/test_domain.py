from datetime import date, timedelta

from rising.domain.models import RepoHistory, StarDay
from rising.domain.ranking import rank
from rising.domain.scoring import star_velocity

from .conftest import NOW, repo, snap, tracked


def test_velocity_from_snapshots_and_activity(histories, settings):
    ranking = rank(histories, settings, NOW)
    good, hype = (
        next(m for m in ranking.repos if m.full_name == n) for n in ("good/agent", "hype/tool")
    )
    assert good.velocity.model_dump() == {
        "source": "snapshots",
        "stars_gained": 240,
        "span_hours": 24.0,
        "stars_per_day": 240.0,
        "lifetime_stars_per_day": 213.33,
    }
    assert hype.velocity.stars_per_day == 600.0
    assert good.activity.factor > 0.9
    assert hype.activity.factor < 0.2
    assert good.red_flags == []
    assert {f.code for f in hype.red_flags} >= {
        "stars_without_forks",
        "no_issues_or_prs",
        "single_author",
        "code_dump",
        "star_spike",
        "stale_while_growing",
    }
    assert [m.full_name for m in ranking.repos] == ["good/agent", "hype/tool"]
    assert ranking.meta.candidates == 2


def test_rates_are_not_extrapolated_and_clones_are_flagged(settings):
    created = NOW - timedelta(hours=2)
    hs = [
        RepoHistory(
            repo=tracked(repo(i, f"{o}/ai--manus", created, "AI Manus is an agent")),
            snapshots=[snap(i, NOW, 150)],
        )
        for i, o in enumerate("abc", start=10)
    ] + [
        RepoHistory(
            repo=tracked(repo(20, "solo/real", NOW - timedelta(days=2), "unique")),
            snapshots=[snap(20, NOW, 100)],
        )
    ]
    by_name = {m.full_name: m for m in rank(hs, settings, NOW).repos}
    spam = by_name["b/ai--manus"]
    assert spam.velocity.stars_per_day == 150.0
    flag = next(f for f in spam.red_flags if f.code == "clone_cluster")
    assert flag.evidence == {
        "repos_with_same_description": 3,
        "repos_with_similar_name_and_stars": 1,
    }
    assert flag.penalty == 0.1
    assert spam.quality_multiplier < by_name["solo/real"].quality_multiplier / 5


def test_the_original_whose_description_was_copied_is_not_penalised(settings):
    desc = "Local-first meeting notes with an MCP server"
    original = RepoHistory(
        repo=tracked(repo(50, "real/notes", NOW - timedelta(days=5), desc)),
        snapshots=[snap(50, NOW, 1400)],
    )
    copies = [
        RepoHistory(
            repo=tracked(repo(i, f"spam{i}/notes-ai", NOW - timedelta(days=1), desc)),
            snapshots=[snap(i, NOW, 40)],
        )
        for i in range(51, 54)
    ]
    flags = {
        m.full_name: {f.code for f in m.red_flags}
        for m in rank([original, *copies], settings, NOW).repos
    }
    assert "clone_cluster" not in flags["real/notes"]
    assert all("clone_cluster" in flags[c.repo.full_name] for c in copies)


def test_velocity_waits_for_a_full_day_of_snapshots(settings):
    snaps = [snap(1, NOW - timedelta(hours=1), 100, 5), snap(1, NOW, 105, 5)]
    history = [
        StarDay(day=date(2026, 11, 12), stars_added=60),
        StarDay(day=date(2026, 11, 13), stars_added=45),
    ]
    v = star_velocity(snaps, history, NOW - timedelta(days=2), NOW, settings.ranking)
    assert (v.source, v.stars_gained, v.stars_per_day) == ("star_history", 105, 70.0)


def test_star_history_velocity_ends_when_the_history_was_fetched(settings):
    snaps = [snap(1, NOW, 105, 5)]
    history = [StarDay(day=date(2026, 11, 12), stars_added=36)]
    fetched = NOW - timedelta(hours=24)  # 2026-11-12 12:00: that day is only half counted
    v = star_velocity(snaps, history, NOW - timedelta(days=5), NOW, settings.ranking, fetched)
    assert (v.source, v.span_hours, v.stars_per_day) == ("star_history", 12.0, 36.0)


def test_stale_star_history_falls_back_to_lifetime_average(settings):
    snaps = [snap(1, NOW, 120, 5)]
    history = [StarDay(day=date(2026, 11, 9), stars_added=120)]
    v = star_velocity(snaps, history, NOW - timedelta(days=6), NOW, settings.ranking)
    assert (v.source, v.stars_per_day) == ("lifetime_average", 20.0)


def test_a_common_name_or_short_description_is_not_a_clone_network(settings):
    hs = [
        RepoHistory(
            repo=tracked(repo(i, f"{o}/ai-agent", NOW - timedelta(days=2), "AI agent")),
            snapshots=[snap(i, NOW, 100)],
        )
        for i, o in enumerate("abc", start=30)
    ]
    for m in rank(hs, settings, NOW).repos:
        assert "clone_cluster" not in {f.code for f in m.red_flags}


def test_templated_names_with_the_same_star_count_are_a_clone_network(settings):
    def history(i, name, stars):
        r = repo(i, name, NOW - timedelta(days=2), f"unique description number {i} here")
        return RepoHistory(repo=tracked(r), snapshots=[snap(i, NOW, stars)])

    hs = [
        history(40, "a/blender-ai-copilot-flow", 270),
        history(41, "b/blender-ai-copilot-hub", 266),
        history(42, "c/blender-ai-copilot-desk", 275),
        history(43, "d/blender-ai-copilot-pro", 900),  # same template, very different stars
    ]
    flags = {m.full_name: {f.code for f in m.red_flags} for m in rank(hs, settings, NOW).repos}
    assert all(
        "clone_cluster" in flags[n]
        for n in ("a/blender-ai-copilot-flow", "b/blender-ai-copilot-hub")
    )
    assert "clone_cluster" not in flags["d/blender-ai-copilot-pro"]


def test_a_repo_that_stopped_being_measured_is_not_ranked(histories, settings):
    gone = RepoHistory(
        repo=tracked(repo(60, "dead/spam", NOW - timedelta(days=5), "deleted spam repo")),
        snapshots=[snap(60, NOW - timedelta(days=4), 100), snap(60, NOW - timedelta(days=3), 900)],
    )
    names = [m.full_name for m in rank([*histories, gone], settings, NOW).repos]
    assert "dead/spam" not in names and "good/agent" in names
