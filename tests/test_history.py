import json
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dateranges  # noqa: E402
import history  # noqa: E402
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


def load(conn, data):
    return history_import.import_export_bytes(conn, json.dumps(data).encode())


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = history.connect(":memory:")

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
        self.assertEqual(history.coverage(self.conn).total, 1)

    def test_invalid_json_raises(self):
        with self.assertRaises(ValueError):
            history_import.import_export_bytes(self.conn, b"no es json")
        with self.assertRaises(ValueError):
            history_import.import_export_bytes(self.conn, b'{"a": 1}')

    def test_api_and_export_overlap_not_double_counted(self):
        load(self.conn, [entry("2024-01-01T10:00:30Z")])
        rows = [(history.parse_ts("2024-01-01T10:00:35Z"), "spotify:track:aaa", "S", "A", "Al", None)]
        self.assertEqual(history.add_plays(self.conn, rows, "api"), (0, 1))

    def test_same_track_far_apart_counts_twice(self):
        load(self.conn, [entry("2024-01-01T10:00:00Z")])
        rows = [(history.parse_ts("2024-01-01T12:00:00Z"), "spotify:track:aaa", "S", "A", "Al", None)]
        self.assertEqual(history.add_plays(self.conn, rows, "api"), (1, 0))

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
        top = history.top_tracks(self.conn, start, end, 10, min_ms=30000)
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
