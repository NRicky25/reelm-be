"""Copy images still hosted on Supabase Storage into the media folder.

Finds poster/backdrop/thumbnail URLs pointing at a Supabase public bucket
(.../storage/v1/object/public/<bucket>/<path>), downloads each file (public
bucket: no key needed), saves it as <MEDIA_ROOT>/images/<path> and rewrites the
URL to <MEDIA_PUBLIC_URL>/images/<path>. Safe to run again. The files on
Supabase are left alone; delete the bucket once the site looks right.

    uv run python -m scripts.move_images_from_supabase
"""

import re

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Episode, Title
from app.services.storage import MediaStorage, sniff_image_type

SUPABASE_PUBLIC = re.compile(
    r"^https://[^/]+\.supabase\.co/storage/v1/object/public/[^/]+/(?P<path>.+)$"
)
FIELDS = ((Title, ("poster_url", "backdrop_url")), (Episode, ("thumb_url",)))


def main() -> None:
    storage = MediaStorage.from_settings(get_settings())
    moved: dict[str, str] = {}
    changed = 0
    with SessionLocal() as db, httpx.Client(timeout=60, follow_redirects=True) as http:
        for model, attrs in FIELDS:
            for row in db.scalars(select(model)):
                for attr in attrs:
                    url = getattr(row, attr)
                    m = SUPABASE_PUBLIC.match(url or "")
                    if not m:
                        continue
                    if url not in moved:
                        r = http.get(url)
                        r.raise_for_status()
                        if sniff_image_type(r.content) is None:
                            raise SystemExit(f"Not an image: {url}")
                        path = httpx.URL(url).path.split("/", 6)[-1]  # unquoted
                        moved[url] = storage.save(path, r.content)
                        print(f"copied {path}")
                    setattr(row, attr, moved[url])
                    changed += 1
        db.commit()
    print(f"{len(moved)} images copied, {changed} catalogue URLs updated")


if __name__ == "__main__":
    main()
