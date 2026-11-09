"""Seed the database with the demo catalogue.

uv run python -m scripts.seed           # add titles that aren't there yet
uv run python -m scripts.seed --reset   # wipe the catalogue and reload it
"""

import argparse

from app.db import SessionLocal
from app.seed import load_catalogue, seed


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--reset", action="store_true", help="delete the existing catalogue first"
    )
    args = parser.parse_args()

    with SessionLocal() as db:
        counts = seed(db, load_catalogue(), reset=args.reset)
    print(
        f"Seeded {counts['titles']} titles, {counts['episodes']} episodes ({counts['skipped']} already existed)."
    )


if __name__ == "__main__":
    main()
