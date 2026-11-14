"""Repository discovery through the REST Search API."""

from __future__ import annotations

import itertools
import logging
import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..config import SearchFilter
from ..domain.models import Repo
from ..domain.timeutil import parse_iso, to_iso
from .client import GitHubClient

log = logging.getLogger(__name__)

SEARCH_CAP = 1000  # Search never returns more than 1000 results for one query
PER_PAGE = 100


def build_queries(f: SearchFilter) -> list[str]:
    """Expand one filter into Search queries (without the created: window)."""
    queries = []
    for kw, topic, lang in itertools.product(
        f.keywords or [None], f.topics or [None], f.languages or [None]
    ):
        parts = []
        if kw:
            term = f'"{kw}"' if " " in kw else kw
            parts.append(f"{term} in:name,description,topics")
        if topic:
            parts.append(f"topic:{topic}")
        if lang:
            parts.append(f'language:"{lang}"')
        if f.min_stars:
            parts.append(f"stars:>={f.min_stars}")
        parts.append("fork:true" if f.include_forks else "fork:false")
        if f.extra_qualifiers:
            parts.append(f.extra_qualifiers.strip())
        queries.append(" ".join(parts))
    return queries


def to_repo(item: dict[str, Any]) -> Repo:
    """Convert a Search API item into a Repo."""
    return Repo(
        id=item["id"],
        node_id=item["node_id"],
        full_name=item["full_name"],
        html_url=item["html_url"],
        description=item.get("description"),
        homepage=item.get("homepage") or None,
        language=item.get("language"),
        topics=item.get("topics") or [],
        license=(item.get("license") or {}).get("spdx_id"),
        owner_login=item["owner"]["login"],
        owner_type=item["owner"].get("type"),
        is_fork=bool(item.get("fork")),
        created_at=parse_iso(item["created_at"]),
    )


@dataclass
class RepoSearch:
    """Yields every repo matching a query in a creation-time window."""

    gh: GitHubClient
    min_window: timedelta
    stats: dict[str, int] = field(
        default_factory=lambda: {"queries": 0, "truncated_windows": 0, "incomplete_results": 0}
    )

    def created_between(self, base_q: str, start: datetime, end: datetime) -> Iterator[Repo]:
        """Yield every matching repo created between `start` and `end`."""
        q = f"{base_q} created:{to_iso(start)}..{to_iso(end)}"
        first = self._page(q, 1)
        total = int(first.get("total_count", 0))
        # Too many matches: split the created: window in half until each part fits the cap.
        if total > SEARCH_CAP and end - start > self.min_window:
            mid = start + (end - start) / 2
            log.info("split %s..%s (%d results)", to_iso(start), to_iso(end), total)
            yield from self.created_between(base_q, start, mid)
            yield from self.created_between(base_q, mid, end)
            return
        # Cannot split further: keep the top results by stars and count the loss.
        if total > SEARCH_CAP:
            self.stats["truncated_windows"] += 1
            log.warning(
                "window %s..%s has %d results; keeping the top %d by stars",
                to_iso(start),
                to_iso(end),
                total,
                SEARCH_CAP,
            )
        pages = math.ceil(min(total, SEARCH_CAP) / PER_PAGE)
        data = first
        for page in range(1, pages + 1):
            if page > 1:
                data = self._page(q, page)
            items = data.get("items", [])
            if not items:
                break
            for item in items:
                repo = to_repo(item)
                if start <= repo.created_at <= end:  # windows share their boundary
                    yield repo

    def _page(self, q: str, page: int) -> dict[str, Any]:
        data = self.gh.search_repositories(q, page=page, per_page=PER_PAGE)
        self.stats["queries"] += 1
        if data.get("incomplete_results"):
            self.stats["incomplete_results"] += 1
        return data
