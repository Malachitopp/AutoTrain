# AutoTrain backend

Python service behind the web app and the mobile app. One image, four entrypoints
(`api`, `ingestor`, `worker`, `scheduler`) — see [ARCHITECTURE.md](../ARCHITECTURE.md).

## Local setup

```bash
docker compose up -d --wait postgres   # from the repo root; binds host port 5433
cd backend
cp .env.example .env
uv sync                                # installs deps + dev group into .venv
uv run autotrain-migrate up
```

Postgres is on **5433**, not 5432, so it can't collide with a Postgres installed
natively on the host. On Windows both can bind 5432 (different address families)
and connections then land on whichever answers first — an unpleasant hour to debug.

## Everyday commands

```bash
uv run autotrain-migrate up        # apply pending migrations
uv run autotrain-migrate status    # what's applied, what's pending
uv run pytest                      # integration tests against real Postgres
uv run ruff check .                # lint, including the SQL-injection rule
uv run ruff format .
uv run lint-imports                # ARCHITECTURE §3 boundary contracts (.importlinter)
```

Without `uv` installed, the same commands work through the venv directly —
`.venv\Scripts\python.exe -m pytest`, `.venv\Scripts\autotrain-migrate.exe up`.
Install `uv` when convenient; CI uses it.

`pytest` drops and recreates the database named in `AUTOTRAIN_TEST_DATABASE_URL`
on every run. It never touches the development database.

## Run the API

```bash
uv run python -m autotrain.entrypoints.api   # http://127.0.0.1:8000 — docs at /docs
```

Host and port come from `AUTOTRAIN_API_HOST` / `AUTOTRAIN_API_PORT` (defaults
`127.0.0.1:8000`).

**Auth is a development stub.** Every request identifies itself with an
`X-User-Id` header carrying an existing user's UUID — the server trusts it
outright. Real authentication (magic-link + JWT) arrives with the identity
module and replaces the stub wholesale; do not build anything on that header.

```bash
curl -s http://127.0.0.1:8000/journeys -H "X-User-Id: <uuid>"
```

## Run the ingestor

```bash
uv run python -m autotrain.entrypoints.ingestor --once
```

Sweeps journeys past their scheduled arrival and records frozen delay
decisions (`delay_detections`). Refuses to start until
`AUTOTRAIN_ARRIVALS_SOURCE` names a real source.

The real source is `hsp` — National Rail's Historical Service Performance
API (actual arrival times, next-day data). Register a free account at
https://raildata.org.uk, subscribe to HSP, then set `AUTOTRAIN_HSP_EMAIL`
and `AUTOTRAIN_HSP_PASSWORD` in `.env`. The client lives in
`sources/hsp.py`; the sweep only ever sees the `ArrivalsSource` protocol.

## Writing a migration

Add `migrations/NNNN_snake_case.sql` with the next number. Never edit a migration
that has been applied anywhere — the runner stores a checksum and will refuse to
run if one changes. Fix a mistake with a new migration.

If the statement cannot run inside a transaction (`CREATE INDEX CONCURRENTLY`,
which is how you add an index to a hot table without blocking writes), make
`-- migrate:no-transaction` the first line and put a single statement in the file.
