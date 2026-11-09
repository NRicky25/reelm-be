"""Move the demo artwork to Supabase Storage and point the catalogue at it.

Uploads reelm-fe/public/images/stills/*.jpg|png|webp to the bucket (folder `stills/`) and
rewrites poster/backdrop/thumbnail URLs that still use the local `/images/stills/`
paths. Safe to run again (re-uploads overwrite; already-moved URLs are skipped).

    uv run python -m scripts.upload_stills [path/to/stills]
"""

import sys
from pathlib import Path

from sqlalchemy import select

from app.config import PROJECT_ROOT, get_settings
from app.db import SessionLocal
from app.models import Episode, Title
from app.services.storage import SupabaseStorage

LOCAL_PREFIX = "/images/stills/"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
DEFAULT_DIR = PROJECT_ROOT.parent / "reelm-fe" / "public" / "images" / "stills"


def main() -> None:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DIR
    files = sorted(f for f in folder.iterdir() if f.suffix.lower() in IMAGE_SUFFIXES)
    if not files:
        raise SystemExit(f"No images in {folder}")

    storage = SupabaseStorage.from_settings(get_settings())
    urls: dict[str, str] = {}
    try:
        storage.ensure_bucket()
        for f in files:
            # fixed names can be re-uploaded with new content -> short cache
            urls[LOCAL_PREFIX + f.name] = storage.upload(
                "stills", f.read_bytes(), name=f.stem, max_age=3600
            )
            print(f"uploaded {f.name}")
    finally:
        storage.close()

    changed = 0
    with SessionLocal() as db:
        for t in db.scalars(select(Title)):
            for attr in ("poster_url", "backdrop_url"):
                if getattr(t, attr) in urls:
                    setattr(t, attr, urls[getattr(t, attr)])
                    changed += 1
        for ep in db.scalars(select(Episode)):
            if ep.thumb_url in urls:
                ep.thumb_url = urls[ep.thumb_url]
                changed += 1
        db.commit()
    print(f"{len(files)} images uploaded, {changed} catalogue URLs updated")


if __name__ == "__main__":
    main()
