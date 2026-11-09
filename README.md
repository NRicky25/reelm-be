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

## Admin panel

`http://localhost:8000/admin`. It's **off until `ADMIN_PASSWORD` is set** in `.env` (at least 12
characters; username `admin` by default). Manage titles, seasons, episodes, genres and videos there.

- After 10 wrong passwords from one address, logins from it are refused for 15 minutes.
- Changing `ADMIN_PASSWORD` logs out existing sessions.
- Deleting a title / season / episode also deletes its videos; a video in use can't be deleted.

- Genres, languages, cast and placeholder colours are typed as comma-separated text.
- Leave a title's slug empty to generate it from the name.
- An episode needs a **video** to be playable; without one it shows as "coming soon".
- The frontend caches API responses for up to 60 s, so edits can take a minute to appear.
- Before deploying: set `ADMIN_SECRET_KEY`, use a strong password and `ADMIN_COOKIE_SECURE=true`.

## Database

Defaults to a `reelm.db` SQLite file in this folder. To use Supabase:

1. In Supabase: **Connect → Session pooler** (port 5432) and copy the connection string.
2. Put it in `.env` as `DATABASE_URL=...`. `postgres://` / `postgresql://` URLs are switched to
   the psycopg 3 driver automatically. (The transaction pooler on port 6543 also works.)
3. Run `uv run alembic upgrade head` and `uv run python -m scripts.seed`.

Lists use JSON on SQLite and JSONB on Postgres. SQLite runs with foreign keys ON so deletes behave
the same as on Postgres. Every episode / movie has its own `videos` row.

## Uploading videos (Mux)

1. Put `MUX_TOKEN_ID` and `MUX_TOKEN_SECRET` in `.env` (Mux → Settings → Access Tokens, *Mux Video: Read + Write*).
2. Admin → **Media → Upload video**: pick a show/episode (or a movie) and a file. The file goes straight from
   your browser to Mux; the page then checks with Mux until it's ready (usually under a minute for short clips).
3. Uploading replaces the episode's old video (its Mux asset is deleted too). Deleting a title / episode / video
   in the admin also deletes the Mux asset, which matters on the free plan (10 videos).

**Webhooks (for production):** in Mux → Settings → Webhooks, add `https://<your-api>/webhooks/mux` and put the
signing secret in `MUX_WEBHOOK_SECRET`. Requests are rejected unless the `Mux-Signature` is valid and recent.
Locally you don't need webhooks: the upload page polls Mux instead.

## Videos

`videos` stores the provider (`mux` or `external`) and its IDs, never a hard-coded Mux URL.
`app/services/video_url.py` builds the playback URL, so changing host only changes that file.
