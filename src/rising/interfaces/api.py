"""Read-only HTTP API; response schemas are published at /openapi.json."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from importlib.metadata import version
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query

from ..config import Settings, load_settings
from ..domain.models import Ranking, Snapshot, StarDay, TrackedRepo
from ..pipeline.current_ranking import current_ranking
from ..storage import Store


@lru_cache
def get_settings() -> Settings:
    """Settings, loaded once per process."""
    return load_settings()


@lru_cache
def _store_for(url: str) -> Store:
    """One Store (engine + connection pool) per process."""
    return Store(url)


def get_store(settings: Annotated[Settings, Depends(get_settings)]) -> Store:
    """The Store for the configured database (FastAPI dependency)."""
    return _store_for(settings.database_url)


StoreDep = Annotated[Store, Depends(get_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

app = FastAPI(
    title="Rising Repos Tracker",
    version=version("rising-tracker"),
    description="Time series of popularity and activity for newly created GitHub repositories.",
)


def _tracked(store: Store, owner: str, name: str) -> TrackedRepo:
    repo = store.repo(f"{owner}/{name}")
    if not repo:
        raise HTTPException(404, "repository not tracked")
    return repo


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check."""
    return {"status": "ok"}


@app.get("/rising")
def rising(store: StoreDep, settings: SettingsDep, n: int = Query(10, ge=1, le=100)) -> Ranking:
    """Current ranking of rising repositories."""
    ranking = current_ranking(store, settings)
    return ranking.model_copy(update={"repos": ranking.repos[:n]})


@app.get("/repos")
def list_repos(
    store: StoreDep,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    language: str | None = None,
) -> list[TrackedRepo]:
    """Tracked repositories, newest first."""
    return store.repos(limit, offset, language)


@app.get("/repos/{owner}/{name}")
def get_repo(owner: str, name: str, store: StoreDep) -> TrackedRepo:
    """One tracked repository."""
    return _tracked(store, owner, name)


@app.get("/repos/{owner}/{name}/snapshots")
def get_snapshots(
    owner: str, name: str, store: StoreDep, since: datetime | None = None
) -> list[Snapshot]:
    """A repository's measurements over time (`since` without a zone is UTC)."""
    if since and since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return store.snapshots(_tracked(store, owner, name).id, since)


@app.get("/repos/{owner}/{name}/star-history")
def get_star_history(owner: str, name: str, store: StoreDep) -> list[StarDay]:
    """Stars added per day, from GitHub's star-history endpoint."""
    return store.star_history(_tracked(store, owner, name).id)
