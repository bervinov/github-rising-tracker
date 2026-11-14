"""Collection jobs: discover, backfill and snapshot."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..config import Settings
from ..domain.timeutil import utcnow
from ..github.client import GitHubClient, GitHubError
from ..github.search import RepoSearch, build_queries
from ..github.snapshots import fetch_snapshots
from ..storage import Store

log = logging.getLogger(__name__)


@dataclass
class Collector:
    """Runs the collection jobs; each run is recorded in the `runs` table."""

    store: Store
    gh: GitHubClient
    settings: Settings

    def _run(self, kind: str, fn: Callable[[int], dict[str, Any]]) -> dict[str, Any]:
        run_id = self.store.start_run(kind)
        before = dict(self.gh.stats)
        try:
            details = fn(run_id)
        except Exception as exc:
            self.store.finish_run(run_id, "failed", {"error": repr(exc)})
            log.exception("%s run %d failed", kind, run_id)
            raise
        details["api"] = {k: v - before.get(k, 0) for k, v in self.gh.stats.items()}
        self.store.finish_run(run_id, "ok", details)
        log.info("%s run %d ok: %s", kind, run_id, details)
        return {"run_id": run_id, **details}

    def discover(self) -> dict[str, Any]:
        """Find repos created in the window that match the configured filters."""

        def job(_: int) -> dict[str, Any]:
            cfg = self.settings.discovery
            now = utcnow()
            start = now - timedelta(days=cfg.window_days)
            search = RepoSearch(self.gh, timedelta(minutes=cfg.min_split_window_minutes))
            seen: set[int] = set()
            new = invalid = 0
            queries = [(f, q) for f in cfg.filters for q in build_queries(f)]
            for f, q in queries:
                if len(seen) >= cfg.max_repos_per_run:
                    log.warning("max_repos_per_run reached; skipping remaining queries")
                    break
                log.info("discover [%s]: %s", f.name, q)
                try:
                    for repo in search.created_between(q, start, now):
                        if repo.id not in seen and len(seen) >= cfg.max_repos_per_run:
                            break
                        new += self.store.upsert_repo(repo, f.name, now)
                        seen.add(repo.id)
                except GitHubError as exc:
                    if exc.status != 422:  # 422: GitHub rejected this query, e.g. a bad qualifier
                        raise
                    log.warning("discover [%s]: query rejected, skipped: %s", f.name, exc)
                    invalid += 1
            return {
                "repos_seen": len(seen),
                "repos_new": new,
                "invalid_queries": invalid,
                **search.stats,
            }

        return self._run("discover", job)

    def backfill(self, refresh: timedelta = timedelta(hours=12)) -> dict[str, Any]:
        """Fetch daily star history for tracked repos."""
        if not self.settings.tracking.backfill_star_history:
            return {"skipped": "tracking.backfill_star_history is false"}

        def job(_: int) -> dict[str, Any]:
            now = utcnow()
            fetched = unavailable = skipped = failed = streak = 0
            for repo in self.store.tracked(now - timedelta(days=self.settings.tracking.track_days)):
                recent = (
                    repo.star_history_checked_at and now - repo.star_history_checked_at < refresh
                )
                if recent:
                    skipped += 1
                    continue
                try:
                    days = self.gh.star_history(repo.full_name)
                except GitHubError as exc:  # one failing repo is retried on the next run
                    log.warning("star history for %s failed: %s", repo.full_name, exc)
                    days, failed = None, failed + 1
                else:
                    if days is None:
                        unavailable += 1
                        self.store.set_star_history_status(repo.id, "unavailable", now)
                if days is None:
                    streak += 1
                    if streak >= 3:  # several repos in a row: the endpoint or GitHub is down
                        log.warning("star history unavailable; stopping backfill for this run")
                        break
                    continue
                streak = 0
                # The endpoint returns whole weeks: keep only days since the repo was created.
                days = [d for d in days if repo.created_at.date() <= d.day <= now.date()]
                self.store.append_star_history(repo.id, days, now)
                self.store.set_star_history_status(repo.id, "ok", now)
                fetched += 1
            return {
                "fetched": fetched,
                "unavailable": unavailable,
                "failed": failed,
                "skipped": skipped,
            }

        return self._run("backfill", job)

    def snapshot(self) -> dict[str, Any]:
        """Measure every tracked repository once."""

        def job(run_id: int) -> dict[str, Any]:
            now = utcnow()
            repos = self.store.tracked(now - timedelta(days=self.settings.tracking.track_days))
            size = self.settings.github.graphql_batch_size
            stats: dict[str, int] = {"batches": 0, "splits": 0}
            inserted = missing = 0
            for i in range(0, len(repos), size):
                snaps, gone = fetch_snapshots(
                    self.gh, [r.node_id for r in repos[i : i + size]], stats
                )
                inserted += self.store.append_snapshots(snaps, run_id)
                missing += gone
            if repos and not inserted and missing == len(repos):
                raise RuntimeError(f"no repository could be measured ({missing} failed)")
            return {
                "tracked": len(repos),
                "snapshots_inserted": inserted,
                "missing": missing,
                **stats,
            }

        return self._run("snapshot", job)

    def run_steps(self, *steps: Callable[[], dict[str, Any]]) -> list[dict[str, Any]]:
        """Run every step even if one fails; then raise the first failure, if any."""
        results, errors = [], []
        for step in steps:
            try:
                results.append(step())
            except Exception as exc:  # noqa: BLE001 - the step already logged and recorded it
                errors.append(exc)
        if errors:
            raise errors[0]
        return results

    def collect(self) -> list[dict[str, Any]]:
        """One full tick: discover -> backfill -> snapshot."""
        return self.run_steps(self.discover, self.backfill, self.snapshot)
