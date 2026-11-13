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
uv run pytest                                   # in-memory SQLite (seconds)
TEST_DATABASE_URL="postgresql://..." uv run pytest   # same tests on Postgres, in a throwaway reelm_test schema (session pooler, port 5432)
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

## Database (Supabase Postgres)

Defaults to a local `reelm.db` SQLite file. To use Supabase:

1. Supabase → **Connect** → Connection String → URI → **Session pooler**. Put it in `.env` as
   `DATABASE_URL=...` with your password in place of `[YOUR-PASSWORD]` (encode `@` as `%40`, `#` as `%23`).
2. `uv run alembic upgrade head` then `uv run python -m scripts.seed`.

- **Row Level Security** is enabled on every table (migration `f263661d9129`), and the public API roles
  (`anon`, `authenticated`) have no privileges on our tables, including tables created later (`a7c1d9e2b4f0`).
  Supabase's public REST/GraphQL API can't read, write or even list them; the backend connects as the owner
  and isn't affected. Still enable RLS on new tables (belt and braces).
- Lists use JSON on SQLite and JSONB on Postgres. SQLite runs with foreign keys ON (but OFF during migrations,
  because SQLite rebuilds tables to alter them). Every episode / movie has its own `videos` row.
- Latency: every page needs a few database round trips, so host the backend in the **same region** as the
  database (this project: `ap-south-1`).

## Media: images and videos (self-hosted)

Images and videos live in one folder on this PC, `MEDIA_ROOT` (e.g. `D:\reelm-media`), with
`images/` and `videos/` inside. The **media server** serves it read-only, and a Cloudflare
Tunnel publishes it as `MEDIA_PUBLIC_URL` (e.g. `https://media.reelm.site`):

```
uv run uvicorn app.media_server:app --host 127.0.0.1 --port 8090
```

It serves only `images/` and `videos/` (GET/HEAD, no folder listings, no hidden files), sends
CORS `*` and the right types for HLS (`.m3u8`, `.ts`), and hides a video folder while
reelm-encoder is still writing it (`.incomplete` marker).

**Images:** in the admin, titles have **Upload poster / Upload backdrop** and episodes
**Upload thumbnail**. JPEG, PNG or WebP up to 8 MB (checked by content, not file name),
saved as `images/<posters|backdrops|thumbnails>/<random>.jpg`. Replacing an image deletes the
old file unless another row still uses it.

- `uv run python -m scripts.upload_stills` copies the demo stills into `images/stills/` and
  points the catalogue at them. Run it again after `scripts.seed --reset`.
- `uv run python -m scripts.move_images_from_supabase` copies images still on Supabase
  Storage into the media folder and rewrites their URLs (safe to run again).

**Videos** are encoded on this PC with [reelm-encoder](https://github.com/NRicky25/reelm-encoder)
(1080p / 720p / 480p HLS) straight into `MEDIA_ROOT\videos` (set its `WORK_DIR`). A video in
Reelm is just a **link + duration**.

**Adding a video:**

1. Encode it with Reelm Encoder; you get a folder like `videos/episode-1-3f9a2c`.
2. Admin → **Media → Videos → New**: paste the link the encoder shows,
   `https://media.reelm.site/videos/<folder>/master.m3u8`, and the duration. **Subtitles**: the language codes of the `.vtt` files in that folder, e.g. `en` for
   `en.vtt` (or `en, es`); the player then offers them in its Subtitles menu.
3. Open the episode (or movie), choose that video, set its **Duration**, Save.

Deleting an episode / movie / video in the admin removes the row only: delete its folder
from `MEDIA_ROOT\videos` too.

`app/services/video_url.py` builds the playback URL, so changing host later only changes
that file.

### Coming from Mux

The migration `c3f8a1d2e5b7` drops the Mux columns. It **refuses to run while a Mux video
still exists** (it would stop playing): first give those episodes a self-hosted video and delete the
Mux video in the admin, then `uv run alembic upgrade head`. Remove any `MUX_*` lines from
`.env` (they're ignored now) and the webhook in the Mux dashboard.
