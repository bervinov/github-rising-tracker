"""Generate a digest end to end and save it with its evidence."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import Settings
from ..domain.timeutil import to_iso, utcnow
from ..pipeline.current_ranking import current_ranking
from ..storage import Store
from .agent import DigestAgent, DigestResult
from .tools import DigestTools, ReadmeSource

if TYPE_CHECKING:
    from openai import OpenAI


def footer(result: DigestResult, model: str, now: datetime) -> str:
    """Footer with the grounding check result."""
    r = result.report
    status = (
        "passed"
        if r.ok
        else (
            f"FAILED ({len(r.violations)} unverified values, {len(r.missing_repos)} missing repos, "
            f"{len(r.missing_facts)} missing required values)"
        )
    )
    lines = [
        "",
        "---",
        (
            f"_Generated {to_iso(now)} by `{model}`. Grounding check: {status}; {r.checked} "
            f"numbers/dates checked against retrieved data in {result.attempts} attempt(s). "
            "Evidence: see the matching `.evidence.json`._"
        ),
    ]
    if not r.ok:
        lines += [
            "",
            "> **Unverified values (do not trust):**",
            *[f"> - {v}" for v in r.violations],
        ]
        if r.missing_repos:
            lines.append(f"> - missing: {', '.join(r.missing_repos)}")
        lines += [f"> - not stated: {f}" for f in r.missing_facts]
    return "\n".join(lines) + "\n"


def write_digest(result: DigestResult, out_dir: Path, model: str, now: datetime) -> Path:
    """Save the Markdown digest and its `.evidence.json`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"digest-{now.strftime('%Y%m%dT%H%M%SZ')}.md"
    path.write_text(result.markdown.rstrip() + "\n" + footer(result, model, now))
    evidence: dict[str, Any] = {
        "generated_at": to_iso(now),
        "model": model,
        "grounding": {
            "ok": result.report.ok,
            "checked": result.report.checked,
            "violations": [str(v) for v in result.report.violations],
            "missing_repos": result.report.missing_repos,
            "missing_facts": result.report.missing_facts,
            "attempts": result.attempts,
        },
        "tool_calls": result.tool_calls,
    }
    path.with_suffix(".evidence.json").write_text(json.dumps(evidence, indent=2))
    return path


def generate_digest(
    store: Store,
    settings: Settings,
    client: OpenAI,
    readme: ReadmeSource | None,
    n: int | None = None,
    now: datetime | None = None,
) -> tuple[DigestResult, Path]:
    """Rank, run the agent, save the files and record the run."""
    now = now or utcnow()
    n = n or settings.llm.top_n
    run_id = store.start_run("digest")
    try:
        tools = DigestTools(
            store, current_ranking(store, settings, now), readme, settings.llm.readme_chars
        )
        result = DigestAgent(client, tools, settings.llm).run(n)
        path = write_digest(result, settings.digest_dir, settings.llm.model, now)
    except Exception as exc:
        store.finish_run(run_id, "failed", {"error": repr(exc)})
        raise
    store.finish_run(
        run_id,
        "ok" if result.report.ok else "failed",
        {
            "path": str(path),
            "checked": result.report.checked,
            "violations": len(result.report.violations),
            "attempts": result.attempts,
        },
    )
    return result, path
