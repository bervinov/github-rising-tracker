"""Load the candidates from storage and rank them."""

from __future__ import annotations

from datetime import datetime, timedelta

from ..config import Settings
from ..domain.models import Ranking
from ..domain.ranking import rank
from ..domain.timeutil import utcnow
from ..storage import Store


def current_ranking(store: Store, settings: Settings, now: datetime | None = None) -> Ranking:
    """Ranking of repositories created within the discovery window."""
    now = now or utcnow()
    since = now - timedelta(days=settings.discovery.window_days)
    return rank(store.histories(since), settings, now)
