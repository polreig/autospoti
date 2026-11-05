"""BD limpia para tests: SQLite en memoria, o Postgres si se define TEST_DATABASE_URL.

    set TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres
    python -m unittest discover -s tests
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import history  # noqa: E402

TABLES = ("plays", "managed_playlists", "spotify_tokens", "recipes")


def fresh_db():
    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not url:
        return history.connect(":memory:")
    db = history.connect(url)
    for table in TABLES:
        db.execute(f"DELETE FROM {table}")
    db.commit()
    return db
