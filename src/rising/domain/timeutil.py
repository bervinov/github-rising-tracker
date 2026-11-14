"""UTC time helpers."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Current UTC time, whole seconds."""
    return datetime.now(UTC).replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    """Format as ISO-8601 UTC with a trailing Z."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    """Parse ISO-8601; a value without a zone is taken as UTC."""
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def hours_between(a: datetime, b: datetime) -> float:
    """Hours from `a` to `b`."""
    return (b - a).total_seconds() / 3600
