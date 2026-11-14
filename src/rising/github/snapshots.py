"""Batched repository measurements through GraphQL."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..domain.models import Snapshot
from ..domain.timeutil import parse_iso, utcnow
from .client import GitHubClient, GitHubError

log = logging.getLogger(__name__)

HISTORY_DEPTH = 50  # recent commits fetched to count distinct authors

SNAPSHOT_QUERY = """
query Snapshot($ids: [ID!]!, $depth: Int!) {
  rateLimit { cost remaining resetAt }
  nodes(ids: $ids) {
    ... on Repository {
      databaseId
      stargazerCount
      forkCount
      pushedAt
      diskUsage
      isArchived
      watchers { totalCount }
      openIssues: issues(states: OPEN) { totalCount }
      closedIssues: issues(states: CLOSED) { totalCount }
      openPRs: pullRequests(states: OPEN) { totalCount }
      mergedPRs: pullRequests(states: MERGED) { totalCount }
      closedPRs: pullRequests(states: CLOSED) { totalCount }
      releases { totalCount }
      latestRelease { tagName publishedAt }
      defaultBranchRef {
        target {
          ... on Commit {
            history(first: $depth) {
              totalCount
              nodes { committedDate author { name user { login } } }
            }
          }
        }
      }
    }
  }
}
"""


def _dt(value: str | None) -> datetime | None:
    return parse_iso(value) if value else None


def parse_node(node: dict[str, Any], captured_at: datetime) -> Snapshot:
    """GraphQL Repository node -> Snapshot."""
    history = ((node.get("defaultBranchRef") or {}).get("target") or {}).get("history") or {}
    commits = history.get("nodes") or []
    # Count authors (GitHub login, or commit name when unlinked); identities are not stored.
    authors = {
        ((c.get("author") or {}).get("user") or {}).get("login")
        or ((c.get("author") or {}).get("name") or "").lower()
        for c in commits
    }
    authors = {a for a in authors if a and not a.endswith("[bot]")}
    release = node.get("latestRelease") or {}
    return Snapshot(
        repo_id=node["databaseId"],
        captured_at=captured_at,
        stars=node["stargazerCount"],
        forks=node["forkCount"],
        watchers=node["watchers"]["totalCount"],
        open_issues=node["openIssues"]["totalCount"],
        closed_issues=node["closedIssues"]["totalCount"],
        open_prs=node["openPRs"]["totalCount"],
        merged_prs=node["mergedPRs"]["totalCount"],
        closed_prs=node["closedPRs"]["totalCount"],
        releases=node["releases"]["totalCount"],
        commits=history.get("totalCount"),
        commit_authors_recent=len(authors) if history else None,
        last_commit_at=_dt(commits[0]["committedDate"]) if commits else None,
        latest_release_tag=release.get("tagName"),
        latest_release_at=_dt(release.get("publishedAt")),
        pushed_at=_dt(node.get("pushedAt")),
        disk_kb=node.get("diskUsage"),
        is_archived=bool(node.get("isArchived")),
    )


def fetch_snapshots(
    gh: GitHubClient, node_ids: list[str], stats: dict[str, int]
) -> tuple[list[Snapshot], int]:
    """Measure a batch of repositories; returns the snapshots and how many repos returned nothing.

    A batch GitHub cannot finish (502/503/504 or a timeout) is split in half and retried; a single
    repository that still fails is skipped for this round. Split counts are added to `stats`.
    """
    try:
        body = gh.graphql(
            SNAPSHOT_QUERY, {"ids": node_ids, "depth": HISTORY_DEPTH}, server_error_retries=1
        )
        nodes = (body.get("data") or {}).get("nodes")
        if nodes is None:
            raise GitHubError(502, f"GraphQL returned no data: {body.get('errors')}")
    except GitHubError as exc:
        if exc.status not in (0, 502, 503, 504):  # 0: network error or timeout
            raise
        if len(node_ids) == 1:
            log.warning("GraphQL %s for %s, skipping it this round", exc.status, node_ids[0])
            return [], 1
        stats["splits"] = stats.get("splits", 0) + 1
        log.warning("GraphQL %s on a batch of %d, splitting", exc.status, len(node_ids))
        mid = len(node_ids) // 2
        a, ma = fetch_snapshots(gh, node_ids[:mid], stats)
        b, mb = fetch_snapshots(gh, node_ids[mid:], stats)
        return a + b, ma + mb
    stats["batches"] = stats.get("batches", 0) + 1
    captured_at = utcnow()
    snaps = [parse_node(n, captured_at) for n in nodes if n]
    return snaps, sum(1 for n in nodes if not n)
