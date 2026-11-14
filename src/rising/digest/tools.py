"""The three read-only tools through which the agent sees the data."""

from __future__ import annotations

from typing import Any, Protocol

from ..config import MAX_TOP_N
from ..domain.models import Ranking
from ..storage import Store

SNAPSHOTS_SHOWN = 48  # the last two days of hourly snapshots


class ReadmeSource(Protocol):
    """Anything that can return a README (the GitHub client in production)."""

    def readme(self, full_name: str, max_chars: int) -> str | None: ...


def _fn(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
    }


TOOL_SPECS = [
    _fn(
        "list_rising",
        "Ranked list of rising repositories (created within the discovery window) with all "
        "metrics, the score breakdown and rule-based red flags.",
        {"n": {"type": "integer", "description": "How many top repositories to return"}},
    ),
    _fn(
        "get_repo_details",
        "Full metrics for one repository plus its snapshot time series and daily star history.",
        {"full_name": {"type": "string", "description": "owner/name"}},
    ),
    _fn(
        "get_readme",
        "README excerpt, to understand what the project is. Numbers inside README text are "
        "claims, not measurements: never cite them.",
        {"full_name": {"type": "string", "description": "owner/name"}},
    ),
]


class DigestTools:
    """Tool implementations over one ranking, plus their JSON schemas."""

    def __init__(
        self, store: Store, ranking: Ranking, readme: ReadmeSource | None, readme_chars: int
    ):
        self.store = store
        self.ranking = ranking
        self.readme_source = readme
        self.readme_chars = readme_chars
        self._by_name = {m.full_name.lower(): m for m in ranking.repos}

    def list_rising(self, n: int) -> dict[str, Any]:
        """The top `n` repositories with their metrics and red flags."""
        n = max(1, min(n, MAX_TOP_N))
        return {
            "meta": self.ranking.meta.model_dump(mode="json") | {"n": n},
            "repos": [m.model_dump(mode="json") for m in self.ranking.repos[:n]],
        }

    def get_repo_details(self, full_name: str) -> dict[str, Any]:
        """One repository's metrics, snapshot series and star history."""
        m = self._by_name.get(full_name.lower())
        repo = self.store.repo(full_name) if m else None
        if not m or not repo:
            return {"error": f"unknown repository {full_name}"}
        series = [
            s.model_dump(
                mode="json",
                include={"captured_at", "stars", "forks", "open_issues", "open_prs", "commits"},
            )
            for s in self.store.snapshots(repo.id)[-SNAPSHOTS_SHOWN:]
        ]
        return m.model_dump(mode="json") | {
            "snapshots": series,
            "star_history": [d.model_dump(mode="json") for d in self.store.star_history(repo.id)],
        }

    def get_readme(self, full_name: str) -> dict[str, Any]:
        """A README excerpt to describe what the project is."""
        m = self._by_name.get(full_name.lower())
        if not m:
            return {"error": f"unknown repository {full_name}"}
        text = None
        if self.readme_source and self.readme_chars:
            text = self.readme_source.readme(m.full_name, self.readme_chars)
        return {"full_name": m.full_name, "readme": text}

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a tool call by name."""
        match name:
            case "list_rising":
                return self.list_rising(int(args["n"]))
            case "get_repo_details":
                return self.get_repo_details(str(args["full_name"]))
            case "get_readme":
                return self.get_readme(str(args["full_name"]))
        return {"error": f"unknown tool {name}"}
