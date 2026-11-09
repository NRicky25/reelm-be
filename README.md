# Reelm: Backend

FastAPI + SQLAlchemy API for the Reelm streaming site (`../reelm-fe`).

## Run it

```bash
uv sync
cp .env.example .env                 # optional: defaults use a local SQLite file
uv run alembic upgrade head          # create / update tables
uv run python -m scripts.seed        # load the demo catalogue (add --reset to reload)
uv run uvicorn app.main:app --reload --port 8000
```

API docs: http://localhost:8000/docs

## Checks

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

## Layout

```
app/
├── main.py            FastAPI app, CORS, routes
├── config.py          settings from env / .env
├── db.py              engine, session, Base
├── models/            tables: titles, genres, seasons, episodes, videos
├── schemas/           API response shapes (match reelm-fe/src/data/types.ts)
├── routers/           endpoints
├── services/
│   └── video_url.py   the ONE place a stored video becomes a stream URL
└── seed.py            loads seed/catalogue.json
migrations/            Alembic
scripts/seed.py        seeding CLI
seed/catalogue.json    demo catalogue exported from reelm-fe
tests/                 pytest (in-memory SQLite)
```

## Database

Defaults to a `reelm.db` SQLite file in this folder. To use Supabase:

1. In Supabase: **Connect → Session pooler** (port 5432) and copy the connection string.
2. Put it in `.env` as `DATABASE_URL=...`. `postgres://` / `postgresql://` URLs are switched to
   the psycopg 3 driver automatically. (The transaction pooler on port 6543 also works.)
3. Run `uv run alembic upgrade head` and `uv run python -m scripts.seed`.

Lists use JSON on SQLite and JSONB on Postgres. SQLite runs with foreign keys ON so deletes behave
the same as on Postgres. Every episode / movie has its own `videos` row.

## Videos

`videos` stores the provider (`mux` or `external`) and its IDs, never a hard-coded Mux URL.
`app/services/video_url.py` builds the playback URL, so changing host only changes that file.
