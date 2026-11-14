"""Builds the real objects (store, GitHub client, OpenAI client) from settings."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from ..config import MissingSecret, Settings, load_settings
from ..github.client import GitHubClient
from ..pipeline.jobs import Collector
from ..storage import Store

if TYPE_CHECKING:
    from openai import OpenAI


def setup_logging() -> None:
    """Log to stderr at INFO level."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def settings(path: str | None = None) -> Settings:
    """Load the configuration."""
    return load_settings(path)


def store(s: Settings) -> Store:
    """Open the database."""
    return Store(s.database_url)


def github(s: Settings) -> GitHubClient:
    """GitHub client with the token from the environment."""
    return GitHubClient(s.github, s.github.token)


def collector(s: Settings) -> Collector:
    """Collector wired to the database and GitHub."""
    gh = github(s)  # a missing token fails before the database file is created
    return Collector(store(s), gh, s)


def openai_client(s: Settings) -> OpenAI:
    """OpenAI client with the key from the environment."""
    from openai import OpenAI

    key = os.environ.get(s.llm.api_key_env, "").strip()
    if not key:
        raise MissingSecret(f"OpenAI key missing: set ${s.llm.api_key_env} (see .env.example)")
    return OpenAI(api_key=key)
