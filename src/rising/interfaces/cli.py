"""Command line interface: `rising --help`."""

from __future__ import annotations

import json
import os
from typing import Annotated

import typer

from ..config import MAX_TOP_N, Settings
from ..pipeline.current_ranking import current_ranking
from . import bootstrap
from .scheduler import Job, Scheduler

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Track newly created GitHub repositories and write a grounded digest.",
)

ConfigOpt = Annotated[str | None, typer.Option("--config", "-c", help="Path to config.yaml")]


def _settings(config: str | None) -> Settings:
    bootstrap.setup_logging()
    return bootstrap.settings(config)


def _print(obj) -> None:
    typer.echo(json.dumps(obj, indent=2, default=str))


@app.command()
def discover(config: ConfigOpt = None) -> None:
    """Find repos created in the window that match the filters, then backfill star history."""
    c = bootstrap.collector(_settings(config))
    _print(c.run_steps(c.discover, c.backfill))


@app.command()
def snapshot(config: ConfigOpt = None) -> None:
    """Append one timestamped measurement for every tracked repository."""
    _print(bootstrap.collector(_settings(config)).snapshot())


@app.command()
def collect(config: ConfigOpt = None) -> None:
    """discover + star-history backfill + snapshot (one cron tick)."""
    _print(bootstrap.collector(_settings(config)).collect())


@app.command("rank")
def rank_cmd(
    n: Annotated[int, typer.Option(min=1)] = 10,
    config: ConfigOpt = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the deterministic ranking (no LLM)."""
    s = _settings(config)
    ranking = current_ranking(bootstrap.store(s), s)
    if not ranking.repos:
        typer.echo("No ranked repositories yet: run `rising collect` first", err=True)
    if as_json:
        typer.echo(
            ranking.model_copy(update={"repos": ranking.repos[:n]}).model_dump_json(indent=2)
        )
        return
    for m in ranking.repos[:n]:
        flags = ",".join(f.code for f in m.red_flags) or "-"
        typer.echo(
            f"{m.rank:>3}. {m.full_name:<45} score={m.score:<8} = "
            f"{m.velocity.stars_per_day}/day ({m.velocity.source}) x {m.quality_multiplier}  "
            f"stars={m.latest.stars} forks={m.latest.forks} issues+prs="
            f"{m.activity.issues_total + m.activity.prs_total} commits={m.latest.commits} "
            f"releases={m.latest.releases} flags={flags}"
        )


@app.command()
def repo(full_name: str, config: ConfigOpt = None) -> None:
    """One repository with its full snapshot series and star history, as JSON."""
    store = bootstrap.store(_settings(config))
    r = store.repo(full_name)
    if not r:
        raise typer.BadParameter(f"{full_name} is not tracked")
    _print(
        {
            "repo": r.model_dump(mode="json"),
            "snapshots": [s.model_dump(mode="json") for s in store.snapshots(r.id)],
            "star_history": [d.model_dump(mode="json") for d in store.star_history(r.id)],
        }
    )


@app.command()
def runs(n: Annotated[int, typer.Option(min=1)] = 20, config: ConfigOpt = None) -> None:
    """Show the latest collection and digest runs (the audit log) as JSON."""
    s = _settings(config)
    _print([r.model_dump(mode="json") for r in bootstrap.store(s).runs(n)])


@app.command()
def digest(
    n: Annotated[
        int | None, typer.Option(min=1, max=MAX_TOP_N, help="Top N (default: llm.top_n)")
    ] = None,
    config: ConfigOpt = None,
    strict: Annotated[bool, typer.Option(help="Exit 1 if grounding fails")] = True,
) -> None:
    """Run the LLM agent and write a grounded Markdown digest to digest_dir."""
    from ..digest.service import generate_digest

    s = _settings(config)
    readme = bootstrap.github(s) if s.llm.readme_chars else None
    result, path = generate_digest(bootstrap.store(s), s, bootstrap.openai_client(s), readme, n)
    typer.echo(path.read_text())
    typer.echo(f"saved: {path}", err=True)
    if strict and not result.report.ok:
        raise typer.Exit(1)


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8000, config: ConfigOpt = None) -> None:
    """Start the read-only HTTP API (OpenAPI at /docs)."""
    import uvicorn

    _settings(config)  # fail fast on a bad config
    if config:
        os.environ["RISING_CONFIG"] = config  # the API process loads settings from here

    uvicorn.run("rising.interfaces.api:app", host=host, port=port)


@app.command()
def schedule(config: ConfigOpt = None) -> None:
    """Long-running loop: discovery+backfill and snapshots at the configured intervals."""
    s = _settings(config)
    c = bootstrap.collector(s)
    # Discovery first, so the first snapshot at start-up already measures the new repos.
    jobs = [
        Job("discover", s.tracking.discovery_interval_minutes * 60, c.discover),
        Job("backfill", s.tracking.discovery_interval_minutes * 60, c.backfill),
        Job("snapshot", s.tracking.snapshot_interval_minutes * 60, c.snapshot),
    ]
    Scheduler(jobs).run_forever()


def main() -> None:
    """Entry point of `rising`: configuration and OpenAI errors print one line, not a traceback."""
    from openai import APIError

    from ..config import UserError

    try:
        app()
    except (UserError, FileNotFoundError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise SystemExit(2) from None
    except APIError as exc:
        typer.echo(f"OpenAI error: {exc}", err=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
