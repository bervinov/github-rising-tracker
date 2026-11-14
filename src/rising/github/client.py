"""GitHub REST and GraphQL client that respects primary and secondary rate limits."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ..config import GitHubConfig
from ..domain.models import StarDay
from ..domain.timeutil import parse_iso

log = logging.getLogger(__name__)


class GitHubError(RuntimeError):
    """An error response from GitHub, with its HTTP status."""

    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


@dataclass
class _Bucket:
    """What GitHub last reported about one rate-limit resource."""

    remaining: int | None = None
    reset_at: float | None = None


@dataclass
class GitHubClient:
    """Serial GitHub client: paces requests, waits out limits, retries errors."""

    cfg: GitHubConfig
    token: str
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.time
    transport: httpx.BaseTransport | None = None
    stats: dict[str, float] = field(
        default_factory=lambda: {"requests": 0, "retries": 0, "slept_s": 0.0}
    )

    def __post_init__(self) -> None:
        self._http = httpx.Client(
            base_url=self.cfg.api_url,
            timeout=self.cfg.timeout_s,
            transport=self.transport,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": self.cfg.api_version,
                "User-Agent": "rising-tracker",
            },
        )
        self._buckets: dict[str, _Bucket] = {}
        self._last_call: dict[str, float] = {}

    def close(self) -> None:
        """Close the HTTP connection pool."""
        self._http.close()

    def _pause(self, seconds: float) -> None:
        if seconds > 0:
            self.stats["slept_s"] = round(self.stats["slept_s"] + seconds, 1)
            self.sleep(seconds)

    def _pace(self, resource: str) -> None:
        now = self.clock()
        # Wait for the longest of: the gap after any request, the slower gap between searches,
        # and the reset of a nearly empty rate-limit bucket.
        gaps = [self.cfg.min_interval_s - (now - max(self._last_call.values(), default=0))]
        if resource == "search":
            gaps.append(self.cfg.search_min_interval_s - (now - self._last_call.get("search", 0)))
        bucket = self._buckets.get(resource)
        if bucket and bucket.remaining is not None and bucket.remaining <= 1 and bucket.reset_at:
            wait = bucket.reset_at - now + 1
            if wait > 0:
                log.warning("%s rate limit exhausted, sleeping %.0fs until reset", resource, wait)
                gaps.append(wait)
        self._pause(max(gaps))
        self._last_call[resource] = self.clock()

    def _record(self, resp: httpx.Response) -> None:
        resource = resp.headers.get("x-ratelimit-resource")
        if resource and "x-ratelimit-remaining" in resp.headers:
            self._buckets[resource] = _Bucket(
                remaining=int(resp.headers["x-ratelimit-remaining"]),
                reset_at=float(resp.headers.get("x-ratelimit-reset", 0)),
            )

    def _retry_delay(self, resp: httpx.Response | None, attempt: int) -> float | None:
        """Seconds to wait before retrying, or None if the error is not retryable."""
        backoff = min(60 * 2**attempt, 900)
        if resp is None:
            return min(2**attempt, 60)
        if resp.status_code in (403, 429):
            # Rate limited: use GitHub's hint if there is one, otherwise back off.
            if "retry-after" in resp.headers:
                return self._retry_after(resp.headers["retry-after"], backoff)
            if resp.headers.get("x-ratelimit-remaining") == "0":
                return max(float(resp.headers.get("x-ratelimit-reset", 0)) - self.clock(), 0) + 1
            text = resp.text.lower()
            if "secondary rate limit" in text or "abuse" in text or resp.status_code == 429:
                return backoff
            return None  # a 403 without a rate-limit sign means no access: retrying will not help
        if resp.status_code >= 500:
            return min(2**attempt * 2, 120)
        return None

    def _retry_after(self, value: str, fallback: float) -> float:
        """Seconds from a Retry-After header, which is either a number or an HTTP date."""
        try:
            return float(value) + 1
        except ValueError:
            try:
                return max(parsedate_to_datetime(value).timestamp() - self.clock(), 0) + 1
            except (TypeError, ValueError):
                return fallback

    def _request(
        self,
        method: str,
        url: str,
        resource: str,
        server_error_retries: int | None = None,
        **kw: Any,
    ) -> httpx.Response:
        """Send a request, waiting out rate limits and retrying transient errors."""
        for attempt in range(self.cfg.max_retries + 1):
            self._pace(resource)
            resp: httpx.Response | None = None
            try:
                resp = self._http.request(method, url, **kw)
                self.stats["requests"] += 1
                self._record(resp)
                if resp.status_code < 400:
                    return resp
            except httpx.TransportError as exc:
                log.warning("network error on %s: %s", url, exc)
            delay = self._retry_delay(resp, attempt)
            limit = self.cfg.max_retries
            if server_error_retries is not None and (resp is None or resp.status_code >= 500):
                limit = min(limit, server_error_retries)
            if delay is None or attempt >= limit:
                if resp is None:
                    raise GitHubError(0, f"network error on {url}")
                raise GitHubError(resp.status_code, resp.text[:300])
            self.stats["retries"] += 1
            log.warning(
                "retrying %s %s in %.0fs (status %s)",
                method,
                url,
                delay,
                resp.status_code if resp is not None else "network",
            )
            self._pause(delay)
        raise AssertionError("unreachable")

    def search_repositories(self, q: str, page: int = 1, per_page: int = 100) -> dict[str, Any]:
        """One page of the repository Search API, sorted by stars."""
        resp = self._request(
            "GET",
            "/search/repositories",
            "search",
            params={"q": q, "sort": "stars", "order": "desc", "per_page": per_page, "page": page},
        )
        return resp.json()

    def star_history(self, full_name: str, max_pages: int = 2) -> list[StarDay] | None:
        """Daily star counts from GET /repos/{o}/{r}/stargazers/history.

        Returns [] when the repo cannot be read and None when the endpoint is hidden from the token.
        """
        days: dict[date, int] = {}
        url: str | None = f"/repos/{full_name}/stargazers/history"
        pages = 0
        while url and pages < max_pages:
            try:
                resp = self._request("GET", url, "core")
            except GitHubError as exc:
                if exc.status in (403, 404, 410, 451):
                    # The repo itself may be gone or blocked (no history for this repo), or it is
                    # readable and only the endpoint is hidden from this token.
                    return [] if self._is_gone(full_name) else None
                raise
            # Weeks come newest first, 30 per page: one page covers the whole tracking window.
            for week in resp.json():
                start = datetime.fromtimestamp(int(week["week"]), UTC).date()
                for offset, count in enumerate(week.get("days") or []):
                    days[start + timedelta(days=offset)] = int(count)
            url = resp.links.get("next", {}).get("url")
            pages += 1
        return [StarDay(day=d, stars_added=n) for d, n in sorted(days.items())]

    def _is_gone(self, full_name: str) -> bool:
        """Whether the repository itself can no longer be read (deleted, private or blocked)."""
        try:
            self._request("GET", f"/repos/{full_name}", "core")
        except GitHubError as exc:
            if exc.status in (403, 404, 410, 451):
                return True
            raise
        return False

    def readme(self, full_name: str, max_chars: int) -> str | None:
        """The README as plain text, or None when it cannot be read."""
        try:
            resp = self._request(
                "GET",
                f"/repos/{full_name}/readme",
                "core",
                headers={"Accept": "application/vnd.github.raw+json"},
            )
        except GitHubError as exc:
            log.warning("README of %s unavailable: %s", full_name, exc)
            return None
        return resp.text[:max_chars]

    def graphql(
        self, query: str, variables: dict[str, Any], server_error_retries: int | None = None
    ) -> dict[str, Any]:
        """POST a GraphQL query."""
        for attempt in range(self.cfg.max_retries + 1):
            resp = self._request(
                "POST",
                "/graphql",
                "graphql",
                server_error_retries=server_error_retries,
                json={"query": query, "variables": variables},
            )
            body = resp.json()
            errors = body.get("errors") or []
            # GraphQL reports rate limiting inside a 200 response.
            if any(e.get("type") == "RATE_LIMITED" for e in errors):
                delay = self._graphql_reset_delay(body) or min(60 * 2**attempt, 900)
                log.warning("GraphQL RATE_LIMITED, sleeping %.0fs", delay)
                self.stats["retries"] += 1
                self._pause(delay)
                continue
            self._respect_graphql_budget(body)
            return body
        raise GitHubError(403, "GraphQL rate limited too many times")

    def _graphql_reset_delay(self, body: dict[str, Any]) -> float | None:
        rl = (body.get("data") or {}).get("rateLimit")
        if rl and rl.get("resetAt"):
            return max(parse_iso(rl["resetAt"]).timestamp() - self.clock(), 0) + 1
        return None

    def _respect_graphql_budget(self, body: dict[str, Any]) -> None:
        rl = (body.get("data") or {}).get("rateLimit")
        if not rl:
            return
        self._buckets["graphql"] = _Bucket(
            remaining=int(rl["remaining"]), reset_at=parse_iso(rl["resetAt"]).timestamp()
        )
        if int(rl["remaining"]) < max(int(rl.get("cost", 1)) * 3, 10):
            delay = self._graphql_reset_delay(body) or 60
            log.warning("GraphQL budget low (%s left), sleeping %.0fs", rl["remaining"], delay)
            self._pause(delay)
