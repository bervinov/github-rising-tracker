# Rising Repos Tracker

Finds GitHub repositories created in the last 7 days that match your filters, measures them
repeatedly (popularity and activity), keeps the full time series, and asks an LLM agent for a short
Markdown digest of the top N. **Every number in the digest is checked against the data the agent
retrieved before the digest is saved.**

Architecture diagram: [`docs/architecture.md`](docs/architecture.md).

## Quick start

Docker (Compose v2.24+):

```bash
git clone https://github.com/bervinov/github-rising-tracker.git && cd github-rising-tracker
cp .env.example .env              # then set GITHUB_TOKEN and OPENAI_API_KEY in .env
docker compose up -d --build      # starts the collector (scheduler) and the API on :8000
docker compose logs -f scheduler  # wait for the first "snapshot run … ok" (about 7 min)
docker compose run --rm cli rank --n 10    # ranking with the score breakdown, no LLM
docker compose run --rm cli digest --n 10  # the digest; also saved to data/digests/
```

The collector discovers new repos every 6 h and snapshots every tracked repo every hour; leave it
running for a day so velocity comes from its own snapshots. API docs: <http://localhost:8000/docs>.
A fine-grained token with *Public repositories (read-only)* access is enough.

Local (Python 3.12+, [uv](https://docs.astral.sh/uv/)): `uv sync`, export both tokens, then
`uv run rising collect`, `uv run rising rank`, `uv run rising digest`; `uv run pytest` needs no
network. Cron can replace `rising schedule` (cron reads neither `.env` nor your `PATH`; `flock`
keeps the jobs from talking to GitHub at the same time):

```cron
GITHUB_TOKEN=...
OPENAI_API_KEY=...
0 * * * *    cd /path/to/github-rising-tracker && flock /tmp/rising.lock /usr/local/bin/uv run rising snapshot
15 */6 * * * cd /path/to/github-rising-tracker && flock /tmp/rising.lock /usr/local/bin/uv run rising discover
0 8 * * 1    cd /path/to/github-rising-tracker && flock /tmp/rising.lock /usr/local/bin/uv run rising digest
```

## Configuration

`config.yaml` is the example config (`-c path` or `RISING_CONFIG` for another); secrets stay in
environment variables. In `discovery.filters`, values in one list are OR-ed, fields are AND-ed,
and profiles are OR-ed:

```yaml
discovery:
  window_days: 7
  filters:
    - name: ai-agents
      topics: [llm, ai-agents, mcp]    # topic:llm OR topic:ai-agents OR topic:mcp
      min_stars: 20                    # AND stars:>=20
```

The ranking covers the 7-day window; repos keep being measured until `tracking.track_days` (30)
after creation, so their later growth stays in the time series the API serves. The model
(`llm.model`), ranking weights and red-flag thresholds are set in the same file.

**Digest intent** (`llm.intent`): a weekly brief for a tech lead scouting new AI-agent and LLM
tooling: which projects deserve a look, and which look like hype without substance.

## Digest

Example from a live run: [`examples/digest-2026-11-14.md`](examples/digest-2026-11-14.md) with its
`.evidence.json` (README text removed); a test re-runs the grounding check on this pair.

An intro (intent, date of the data, how the list is ranked), then per repo **What it is**, **Why it
ranks here** (score, quality multiplier, velocity, stars, forks, commits, issues, PRs, releases,
why it sits above or below its neighbours) and **Red flags**, then **Takeaways**. Each digest is
saved to `data/digests/` with an `.evidence.json` of every tool call and the data returned.

## Access for other programs

| CLI | HTTP API (read-only, schema at `/openapi.json`) |
|---|---|
| `rising rank [--n 10] [--json]` | `GET /rising?n=10`: ranking + `meta` (formula, window, `data_as_of`) |
| `rising repo owner/name` (JSON) | `GET /repos`, `GET /repos/{owner}/{name}` |
| | `GET /repos/{owner}/{name}/snapshots?since=`, `.../star-history` |
| `rising runs` (audit log, JSON) | `GET /health` |
| `rising discover` / `snapshot` / `collect` / `schedule` | |
| `rising digest [--n 10]` (exit 1 if the check fails), `rising serve` | |

## Data layout

SQLite `data/rising.db` (WAL), UTC ISO-8601 timestamps. Tables are SQLAlchemy models in
`src/rising/storage/tables.py`, created on first start with the append-only triggers.

| Table | Content | Mutability |
|---|---|---|
| `repos` | one row per repo keyed by GitHub `databaseId`: name, description, language, topics, license, owner, `created_at`, matched filters | metadata, refreshed |
| `snapshots` | per repo per run: `captured_at`, stars, forks, watchers, issues, PRs, releases, commits, recent authors, last push, size, archived | **append-only**: unique `(repo_id, captured_at)`, triggers abort `UPDATE`/`DELETE` |
| `star_history` | stars added per day from GitHub's star-history endpoint, one set per fetch | **append-only**, same triggers |
| `runs` | every job: status, counts, API requests, retries, seconds waited | audit log |

Read the data through the CLI or API while the stack runs: on macOS and Windows, opening
`data/rising.db` from the host corrupts it (SQLite's WAL lock does not work across Docker Desktop's
file sharing); `docker compose stop` first.

## Architecture

One package and one Docker image, run as the collector and the API plus one-off CLI commands,
sharing one SQLite file, as drawn in [`docs/architecture.md`](docs/architecture.md). Dependencies
point inward; `domain` has no I/O.

```
src/rising/
├── domain/       scoring, red flags, ranking (pure functions)
├── github/       rate-limited client, search window splitting, GraphQL snapshots
├── storage/      SQLAlchemy tables + Store (the only SQL)
├── pipeline/     discover / backfill / snapshot jobs, current ranking
├── digest/       agent tools, Responses loop, grounding check, output files
└── interfaces/   CLI, API, scheduler, wiring
```

| Language / framework | Why |
|---|---|
| Python 3.12+, OpenAI SDK (Responses API) | the spec's stack; function calling built in |
| httpx | one client for REST and GraphQL; tests swap its transport |
| SQLite + SQLAlchemy 2.0 | one writer, small data, no server; typed models, `ON CONFLICT` |
| Pydantic, PyYAML | config validation and domain models (also the API's JSON Schema) |
| FastAPI, Typer | API with OpenAPI; CLI from type hints |
| uv, pytest, ruff | locked installs (`uv.lock`), offline tests, lint |

## Design decisions

| Decision | Why, and what was rejected |
|---|---|
| Ranking and red flags in code; the LLM only explains | an LLM ranking is not reproducible |
| Velocity from our own hourly snapshots, never extrapolated from under a day | GitHub cannot say when stars arrived; the official [`/stargazers/history`][star-history] endpoint (GA 2026-09-04) is used only as a backfill until a repo has a day of snapshots; without it everything still works (lifetime average, then snapshots) |
| Search with `created:` windows halved until under 1000 results | a broad query had about 88 000 matches |
| GraphQL batches of 25, halved on 502 | REST per repo costs about 5× the quota; a 40-repo batch got 502 |
| Append-only enforced by database triggers | a convention in code can be broken; plain files cannot enforce it |
| Serial in-process scheduler | parallel workers would break GitHub's "no concurrent requests" rule |
| SQLite, not Postgres | no server to run at this size; all SQL is in `storage/` |

**Rate limits.** Every GitHub request goes through `github/client.py`, one at a time: at least
0.25 s between requests and 2.1 s between searches; `X-RateLimit-*` headers and GraphQL's own
`rateLimit` are read, and the client waits for the reset rather than fail; 403/429 follow
`Retry-After` or back off exponentially. Each job logs requests, retries and waits in `runs`.

**Score.** `score = stars_per_day × (0.2 + 0.8 × activity_factor) × penalties`, for repos measured
in the last three snapshot rounds (a deleted repo does not keep ranking on old data). `activity_factor`
is the mean of four capped signals: forks (2% of stars), issues+PRs (3), extra commit authors (2),
commits (10). Two red flags penalise: `clone_cluster` ×0.1 (3+ repos share a description or a
templated name with near-equal stars; an original with clearly more stars than its copies is
spared) and `star_spike` ×0.5 (half of all stars on one day, for repos at least 3 days old). The others (`single_author`,
`no_issues_or_prs`, `code_dump`, ...) only explain. Live data showed why: ranked by raw velocity, a
star-farming network (`*/ai--manus`) took the whole top 12. The weights are hand-picked and live in
`config.yaml`; fitting them on labelled data is the next step.

**Grounding.** The agent sees our data only through three tools. `digest/grounding.py` checks every
number and date in a repo's section against that repo's tool results, at the precision written
(including `25M`, `3rd`, `999/day` and numbers in code). Numbers in a README or a description are
the author's claims and don't count; numbers written as words are rejected (except "one", too
common in prose); the intro and takeaways may use only the ranking metadata. A number followed by
a field word (`stars`, `forks`, `commits`, `issues`, `PRs`, `releases`) must equal a field of that
name, so "75 forks" fails if 75 is the commit count, and a percentage must match a share or ratio.
The same holds for `authors`, `days`, `hours`, multipliers (`2x`) and labels (`Stars: 75`). On
failure the model rewrites with the offending values listed; if it still fails, the digest is saved
marked "unverified" and the command exits 1. The footer is written by code after the check. Not
proved: a number without a field word or unit ("score 216.81", "ranked 3") only has to occur in
that repo's data.

## Limitations

- One ranking mixes velocity sources (snapshots, star history, lifetime average) until every repo
  has a day of snapshots; `velocity.source` says which.
- Activity signals are cumulative, so a repo with imported history saturates them.
- A spam network with unrelated names and varied star counts would pass the clone rule.
- Commits cover the default branch; "authors" are names among the last 50 commits, bots excluded.
- `digest` reads READMEs from GitHub while the scheduler may also be making requests.

[star-history]: https://github.blog/changelog/2026-09-04-new-api-endpoint-provides-privacy-safe-star-history-data/
