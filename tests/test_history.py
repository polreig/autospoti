import json
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dateranges  # noqa: E402
import history  # noqa: E402
from _db import fresh_db  # noqa: E402
import history_import  # noqa: E402

MADRID = ZoneInfo("Europe/Madrid")


def entry(ts, uri="spotify:track:aaa", ms=200000, name="Song", artist="Artist"):
    return {
        "ts": ts,
        "ms_played": ms,
        "spotify_track_uri": uri,
        "master_metadata_track_name": name,
        "master_metadata_album_artist_name": artist,
        "master_metadata_album_album_name": "Album",
    }


ME = "me"


def load(conn, data, user_id=ME):
    return history_import.import_export_bytes(conn, user_id, json.dumps(data).encode())


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.addCleanup(self.conn.close)

    def test_import_filters_and_dedups(self):
        data = [
            entry("2022-03-05T18:00:00Z"),
            entry("2022-03-05T18:00:00Z"),  # repetida exacta
            entry("2022-03-06T10:00:00Z", uri=None),  # podcast/local
            entry("2022-03-06T10:00:00Z", uri="spotify:episode:xyz"),
            {"trackName": "viejo", "endTime": "2022-03-01 10:00"},  # formato antiguo
            entry("fecha-mala"),
        ]
        stats = load(self.conn, data)
        self.assertEqual(stats.inserted, 1)
        self.assertEqual(stats.duplicates, 1)
        self.assertEqual(stats.no_uri, 1)
        self.assertEqual(stats.not_track, 1)
        self.assertEqual(stats.legacy_format, 1)
        self.assertEqual(stats.invalid, 1)

    def test_import_twice_is_idempotent(self):
        load(self.conn, [entry("2022-03-05T18:00:00Z")])
        stats = load(self.conn, [entry("2022-03-05T18:00:00Z")])
        self.assertEqual(stats.inserted, 0)
        self.assertEqual(history.coverage(self.conn, ME).total, 1)

    def test_invalid_json_raises(self):
        with self.assertRaises(ValueError):
            history_import.import_export_bytes(self.conn, ME, b"no es json")
        with self.assertRaises(ValueError):
            history_import.import_export_bytes(self.conn, ME, b'{"a": 1}')

    def test_api_and_export_overlap_not_double_counted(self):
        load(self.conn, [entry("2024-01-01T10:00:30Z")])
        rows = [(history.parse_ts("2024-01-01T10:00:35Z"), "spotify:track:aaa", "S", "A", "Al", None)]
        self.assertEqual(history.add_plays(self.conn, ME, rows, "api"), (0, 1))

    def test_same_track_far_apart_counts_twice(self):
        load(self.conn, [entry("2024-01-01T10:00:00Z")])
        rows = [(history.parse_ts("2024-01-01T12:00:00Z"), "spotify:track:aaa", "S", "A", "Al", None)]
        self.assertEqual(history.add_plays(self.conn, ME, rows, "api"), (1, 0))

    def test_users_are_isolated(self):
        load(self.conn, [entry("2024-01-01T10:00:00Z")], user_id="ana")
        load(self.conn, [entry("2024-01-01T10:00:00Z"), entry("2024-01-02T10:00:00Z")], user_id="ME")
        self.assertEqual(history.coverage(self.conn, "ana").total, 1)
        self.assertEqual(history.coverage(self.conn, "ME").total, 2)
        # La misma escucha en otra cuenta no es "duplicada" ni cruza el filtro de solapamiento.
        rows = [(history.parse_ts("2024-01-01T10:00:30Z"), "spotify:track:aaa", "S", "A", "Al", None)]
        self.assertEqual(history.add_plays(self.conn, "nadie", rows, "api"), (1, 0))
        start, end = dateranges.range_for_month(2024, 1, MADRID)
        self.assertEqual(history.top_tracks(self.conn, "ana", start, end, 10)[0].plays, 1)

    def test_empty_user_id_rejected(self):
        with self.assertRaises(ValueError):
            history.add_plays(self.conn, "", [], "api")

    def test_export_extra_fields_are_stored(self):
        data = [dict(entry("2024-01-01T10:00:00Z"), skipped=True, reason_end="fwdbtn",
                     shuffle=False, platform="android"),
                dict(entry("2024-01-01T11:00:00Z"), skipped=None)]
        load(self.conn, data)
        rows = self.conn.execute(
            "SELECT skipped, reason_end, shuffle, platform FROM plays ORDER BY ts").fetchall()
        self.assertEqual(rows, [(1, "fwdbtn", 0, "android"), (None, None, None, None)])

    def test_bulk_import_is_fast_and_dedups_against_api(self):
        import time

        base = history.parse_ts("2020-01-01T00:00:00Z")
        api_rows = [(base + 600 * i + 30, f"spotify:track:{i % 500}", "S", "A", "Al", None)
                    for i in range(0, 20000, 2)]
        history.add_plays(self.conn, ME, api_rows, "api")
        export_rows = [(base + 600 * i, f"spotify:track:{i % 500}", "S", "A", "Al", 60000)
                       for i in range(20000)]
        started = time.perf_counter()
        inserted, dup = history.add_plays(self.conn, ME, export_rows, "export")
        self.assertLess(time.perf_counter() - started, 5)
        # La mitad ya estaba (registrada por API a 30 s): no se cuenta dos veces.
        self.assertEqual((inserted, dup), (10000, 10000))

    def test_top_tracks_range_and_min_ms(self):
        data = [
            entry("2022-03-02T10:00:00Z", uri="spotify:track:a", name="A"),
            entry("2022-03-03T10:00:00Z", uri="spotify:track:a", name="A"),
            entry("2022-03-04T10:00:00Z", uri="spotify:track:b", name="B"),
            entry("2022-03-05T10:00:00Z", uri="spotify:track:c", name="C", ms=5000),  # escucha corta
            entry("2022-04-01T10:00:00Z", uri="spotify:track:b", name="B"),  # fuera de rango
            entry("2022-02-28T10:00:00Z", uri="spotify:track:d", name="D"),  # fuera de rango
        ]
        load(self.conn, data)
        start, end = dateranges.range_for_month(2022, 3, MADRID)
        top = history.top_tracks(self.conn, ME, start, end, 10, min_ms=30000)
        self.assertEqual([(t.uri, t.plays) for t in top],
                         [("spotify:track:a", 2), ("spotify:track:b", 1)])

    def test_recently_played_rows_skip_episodes(self):
        items = [
            {"played_at": "2024-05-01T10:00:00.123Z",
             "track": {"uri": "spotify:track:a", "name": "A",
                       "artists": [{"name": "X"}, {"name": "Y"}], "album": {"name": "Al"}}},
            {"played_at": "2024-05-01T11:00:00Z", "track": {"uri": "spotify:episode:e", "name": "Pod"}},
            {"played_at": "2024-05-01T12:00:00Z", "track": {"uri": None, "name": "Local"}},
        ]
        rows = history.rows_from_recently_played(items)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], "X, Y")

    def test_managed_playlist_ids_roundtrip(self):
        self.assertIsNone(history.get_playlist_id(self.conn, ME, "L"))
        history.save_playlist_id(self.conn, ME, "L", "p1")
        history.save_playlist_id(self.conn, ME, "L", "p2")
        self.assertEqual(history.get_playlist_id(self.conn, ME, "L"), "p2")
        self.assertIsNone(history.get_playlist_id(self.conn, "otro", "L"))


class MigrationTests(unittest.TestCase):
    OLD_SCHEMA = """
        CREATE TABLE plays (ts INTEGER NOT NULL, track_uri TEXT NOT NULL, track_name TEXT,
            artist TEXT, album TEXT, ms_played INTEGER, source TEXT NOT NULL,
            PRIMARY KEY (ts, track_uri));
        CREATE INDEX idx_plays_uri_ts ON plays (track_uri, ts);
        INSERT INTO plays VALUES (1700000000, 'spotify:track:a', 'A', 'X', 'Al', 60000, 'export');
        INSERT INTO plays VALUES (1700000500, 'spotify:track:b', 'B', 'Y', 'Al', NULL, 'api');
    """

    def setUp(self):
        import sqlite3
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "history.db"
        old = sqlite3.connect(self.path)
        old.executescript(self.OLD_SCHEMA)
        old.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_old_database_is_migrated_with_backup(self):
        conn = history.connect(self.path)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], history.SCHEMA_VERSION)
            self.assertEqual(history.coverage(conn, history.ORPHAN_USER).total, 2)
            self.assertTrue(self.path.with_suffix(".v1-backup.db").exists())
            self.assertEqual(history.claim_orphan_plays(conn, "me"), 2)
            self.assertEqual(history.coverage(conn, "me").total, 2)
            self.assertEqual(history.coverage(conn, history.ORPHAN_USER).total, 0)
        finally:
            conn.close()
        # Volver a abrir no migra de nuevo ni pierde datos.
        conn = history.connect(self.path)
        try:
            self.assertEqual(history.coverage(conn, "me").total, 2)
        finally:
            conn.close()

    def test_orphans_not_claimed_when_db_has_other_users(self):
        conn = history.connect(self.path)
        try:
            history.add_plays(conn, "ana", [(1, "spotify:track:z", "Z", "Z", "Z", None)], "api")
            self.assertEqual(history.claim_orphan_plays(conn, "me"), 0)
            self.assertEqual(history.coverage(conn, history.ORPHAN_USER).total, 2)
        finally:
            conn.close()


class DateRangeTests(unittest.TestCase):
    def test_month_boundaries_respect_timezone(self):
        start, end = dateranges.range_for_month(2022, 3, MADRID)
        self.assertEqual(datetime.fromtimestamp(start, timezone.utc).isoformat(), "2022-02-28T23:00:00+00:00")
        # El 27/03/2022 hubo cambio a horario de verano: el mes acaba el 31/03 a las 22:00 UTC
        self.assertEqual(datetime.fromtimestamp(end, timezone.utc).isoformat(), "2022-03-31T22:00:00+00:00")

    def test_february_leap_year(self):
        start, end = dateranges.range_for_month(2024, 2, timezone.utc)
        self.assertEqual((end - start) // 86400, 29)

    def test_end_before_start_raises(self):
        with self.assertRaises(ValueError):
            dateranges.range_for_dates(date(2022, 3, 5), date(2022, 3, 1), MADRID)

    def test_single_day_range(self):
        start, end = dateranges.range_for_dates(date(2022, 3, 5), date(2022, 3, 5), timezone.utc)
        self.assertEqual(end - start, 86400)


if __name__ == "__main__":
    unittest.main()
