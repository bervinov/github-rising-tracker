# Architecture

Container view ([C4](https://c4model.com/) level 2): what runs, where the data lives and what
flows between them. One Docker image runs as three Compose services that share one SQLite file.

```mermaid
flowchart TB
    subgraph GitHub["GitHub API"]
        search[REST Search]
        history[REST star history]
        graphql[GraphQL]
        readme[REST README]
    end

    subgraph scheduler["scheduler service: rising schedule"]
        discover["discover<br/>every 6 h"]
        backfill["backfill<br/>every 6 h"]
        snapshot["snapshot<br/>every hour"]
    end

    db[("SQLite data/rising.db<br/>repos, snapshots, star_history, runs<br/>snapshots are append-only")]

    api["api service: rising serve<br/>FastAPI, read-only"]
    cli["cli service: rising rank / repo / runs / digest<br/>one-off commands"]

    clients([Other programs])
    openai[(OpenAI API)]
    files[/"data/digests<br/>digest.md + evidence.json"/]

    search --> discover
    history --> backfill
    graphql --> snapshot
    discover --> db
    backfill --> db
    snapshot --> db
    db --> api
    db --> cli
    api -- "HTTP :8000" --> clients
    readme -- "digest only" --> cli
    cli -- "digest only" --> openai
    cli --> files
```
