"""Playlists automáticas (longitudes, Always, versiones) y playlists mensuales."""
import io
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cron  # noqa: E402
import history  # noqa: E402
import jobs  # noqa: E402
import monthly  # noqa: E402
from _db import fresh_db  # noqa: E402
from spotify_core import PlaylistIndex, PlaylistResult  # noqa: E402

TZ = ZoneInfo("Europe/Madrid")
NOW = datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc)  # tarea diaria del 1 de noviembre


class FakeSp:
    """Lo mínimo de spotipy para PlaylistIndex: tus playlists actuales."""

    def __init__(self, names=()):
        self.names = list(names)

    def current_user_playlists(self, limit=50):
        return {"items": [{"id": f"pl{i}", "name": n, "owner": {"id": "me"}} for i, n in enumerate(self.names)],
                "next": None}


def ts(year, month, day, hour=12):
    return int(datetime(year, month, day, hour, tzinfo=TZ).timestamp())


def add(conn, rows, source="export"):
    history.add_plays(conn, "me", [(t, uri, name, artist, "Al", 200000) for t, uri, name, artist in rows], source)


def month_rows(year, month, songs, day=10):
    """``songs`` canciones distintas en ese mes (la canción i, i+1 veces)."""
    rows = []
    for i in range(songs):
        for k in range(i + 1):
            rows.append((ts(year, month, day, 0) + 60 * (i * 40 + k), f"spotify:track:{year}{month}-{i}",
                         f"Canción {i}", f"Artista {year}{month}"))
    return rows


class FakeWriter:
    def __init__(self):
        self.calls = []

    def __call__(self, sp, user_id, name, uris, description="", **kwargs):
        self.calls.append((name, list(uris), description))
        return PlaylistResult(f"id-{name}", True, len(uris), "u")

    @property
    def names(self):
        return [c[0] for c in self.calls]


class NamesTests(unittest.TestCase):
    def test_monthly_name_format(self):
        self.assertEqual(monthly.monthly_name(2025, 9), "September 25'")
        self.assertEqual(monthly.monthly_name(2022, 10), "October 22'")
        self.assertEqual(monthly.monthly_name(2009, 1), "January 09'")

    def test_parse_accepts_variants(self):
        for name in ("September 25'", "september 25", "September 25’", "Sept 25", "Sep. 2025",
                     "Septiembre 25'", "SEPTEMBER '25", "  September   2025 "):
            self.assertEqual(monthly.parse_monthly_name(name), (2025, 9), name)

    def test_parse_rejects_other_playlists(self):
        for name in ("Always", "Más escuchado (7 días)", "Septembers", "Verano 25", "September 2025 mix", ""):
            self.assertIsNone(monthly.parse_monthly_name(name), name)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.addCleanup(self.conn.close)
        add(self.conn, month_rows(2026, 7, 12) + month_rows(2026, 8, 3) + month_rows(2026, 9, 15)
            + month_rows(2026, 10, 120) + month_rows(2026, 11, 5, day=1))

    def plan(self, names=()):
        return monthly.plan(self.conn, "me", TZ, list(names), now=NOW)

    def test_complete_months_only_with_status(self):
        plans = self.plan(["July 26'"])
        self.assertEqual([p.name for p in plans], ["July 26'", "August 26'", "September 26'", "October 26'"])
        self.assertEqual([p.status for p in plans],
                         [monthly.EXISTS, monthly.FEW, monthly.TO_CREATE, monthly.TO_CREATE])
        october = plans[-1]
        self.assertEqual(len(october.tracks), monthly.MONTHLY_LIMIT)  # 120 canciones -> top 100
        self.assertEqual(october.tracks[0].name, "Canción 119")       # la más escuchada primero

    def test_month_uses_local_timezone(self):
        # 30 de septiembre a las 23:30 en Madrid = 21:30 UTC: cuenta para septiembre.
        add(self.conn, [(ts(2026, 9, 30, 23) + 1800, "spotify:track:noche", "Noche", "X")])
        plans = {p.name: p for p in self.plan()}
        self.assertIn("spotify:track:noche", [t.uri for t in plans["September 26'"].tracks])
        self.assertNotIn("spotify:track:noche", [t.uri for t in plans["October 26'"].tracks])

    def test_managed_month_is_not_recreated(self):
        history.save_playlist_id(self.conn, "me", "September 26'", "borrada")
        self.assertEqual(self.plan()[2].status, monthly.MANAGED)


class CreateMonthlyTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.addCleanup(self.conn.close)
        add(self.conn, month_rows(2026, 8, 12) + month_rows(2026, 9, 12) + month_rows(2026, 10, 12))
        self.writer = FakeWriter()

    def create(self, sp, **kw):
        with mock.patch.object(jobs, "create_or_update_playlist", side_effect=self.writer):
            return jobs.create_monthly_playlists(sp, self.conn, "me", TZ, PlaylistIndex(sp, "me"), now=NOW, **kw)

    def test_daily_run_only_creates_previous_month(self):
        outcomes = self.create(FakeSp(), only_previous_month=True)
        self.assertEqual(self.writer.names, ["October 26'"])
        self.assertTrue(outcomes[0].ok)
        self.assertIn("octubre de 2026", self.writer.calls[0][2])
        # Al día siguiente ya está (recordada en la BD): no se repite.
        self.assertEqual(self.create(FakeSp(), only_previous_month=True), [])

    def test_backfill_creates_missing_oldest_first_and_skips_existing(self):
        self.create(FakeSp(["Sept 26"]))
        self.assertEqual(self.writer.names, ["August 26'", "October 26'"])

    def test_max_count_batches(self):
        self.create(FakeSp(), max_count=2)
        self.assertEqual(self.writer.names, ["August 26'", "September 26'"])
        self.create(FakeSp(), max_count=2)
        self.assertEqual(self.writer.names[2:], ["October 26'"])

    def test_previous_month_already_there_does_nothing(self):
        self.assertEqual(self.create(FakeSp(["October 26'"]), only_previous_month=True), [])
        self.assertEqual(self.writer.calls, [])


class AutoPlaylistsTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.addCleanup(self.conn.close)
        self.writer = FakeWriter()

    def run_auto(self, top=()):
        with mock.patch.object(jobs, "get_recently_played", return_value=[]), \
                mock.patch.object(jobs, "get_top_tracks", return_value=list(top)) as get_top, \
                mock.patch.object(jobs, "create_or_update_playlist", side_effect=self.writer):
            outcomes = jobs.update_auto_playlists(FakeSp(), self.conn, "me")
        return outcomes, get_top

    def written(self, name):
        return dict((c[0], c[1]) for c in self.writer.calls).get(name)

    def test_lengths_with_full_history(self):
        now = datetime.now(timezone.utc)
        rows = []
        # 300 canciones distintas en los últimos 3 días (cubre 7 días, 4 semanas...) y una
        # escucha de hace 2 años para que el registro cubra todos los periodos.
        for i in range(300):
            rows.append((int((now - timedelta(days=3)).timestamp()) + 60 * i, f"spotify:track:{i}", f"S{i}", "A"))
        rows.append((int((now - timedelta(days=730)).timestamp()), "spotify:track:vieja", "Vieja", "B"))
        add(self.conn, rows)
        outcomes, get_top = self.run_auto()
        self.assertEqual(len(self.written(jobs.NAME_7D)), 50)
        self.assertEqual(len(self.written(jobs.NAME_4W)), 100)
        self.assertEqual(len(self.written(jobs.NAME_6M)), 100)
        self.assertEqual(len(self.written(jobs.NAME_YEAR)), 100)
        self.assertEqual(len(self.written(jobs.NAME_ALWAYS)), 250)
        get_top.assert_not_called()  # todo sale del registro propio
        self.assertEqual(len(outcomes), 6)

    def test_seven_days_excludes_older_plays_and_ranks_by_plays(self):
        now = datetime.now(timezone.utc)
        recent = int((now - timedelta(days=2)).timestamp())
        old = int((now - timedelta(days=8)).timestamp())
        add(self.conn, [(recent + i, "spotify:track:fav", "Fav", "A") for i in range(0, 1500, 300)]
            + [(recent + 5000, "spotify:track:una", "Una", "A")]
            + [(old + i, "spotify:track:vieja", "Vieja", "A") for i in range(0, 3000, 300)])
        self.run_auto()
        self.assertEqual(self.written(jobs.NAME_7D), ["spotify:track:fav", "spotify:track:una"])

    def test_without_coverage_falls_back_to_spotify_with_100(self):
        add(self.conn, [(int(datetime.now(timezone.utc).timestamp()) - 3600, "spotify:track:1", "S", "A")])
        _, get_top = self.run_auto(top=[{"id": "2", "uri": "spotify:track:2"}])
        limits = {c.args[1]: c.args[2] for c in get_top.call_args_list}
        self.assertEqual(limits, {"short_term": 100, "medium_term": 100, "long_term": 100})
        self.assertEqual(self.written(jobs.NAME_ALWAYS), ["spotify:track:1"])


class VersionsTests(unittest.TestCase):
    def test_same_song_with_two_uris_counts_once(self):
        conn = fresh_db()
        self.addCleanup(conn.close)
        add(conn, [(1000, "spotify:track:single", "Hit", "Grupo"), (2000, "spotify:track:album", "hit ", "grupo"),
                   (3000, "spotify:track:album", "Hit", "Grupo"), (4000, "spotify:track:otra", "Otra", "Grupo"),
                   (5000, "spotify:track:otra", "Otra", "Grupo")])
        top = history.top_tracks(conn, "me", 0, 10_000, 10)
        self.assertEqual([(t.uri, t.plays) for t in top], [("spotify:track:album", 3), ("spotify:track:otra", 2)])


class CronMonthsTests(unittest.TestCase):
    def test_flags_are_validated(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cron.run(record_only=True, months=True), 1)
            self.assertEqual(cron.run(preview=True), 1)

    def test_preview_does_not_write(self):
        conn = fresh_db()
        self.addCleanup(conn.raw.close)
        add(conn, month_rows(2026, 9, 12))
        conn.close = mock.Mock()
        out = io.StringIO()
        with mock.patch.object(cron, "use_db_tokens", return_value=False), \
                mock.patch.object(cron.history, "connect", return_value=conn), \
                mock.patch.object(cron, "get_spotify_client") as client, \
                mock.patch.object(jobs, "create_or_update_playlist") as write, redirect_stdout(out):
            client.return_value = FakeSp()
            client.return_value.current_user = lambda: {"id": "me"}
            code = cron.run(months=True, preview=True)
        self.assertEqual(code, 0)
        write.assert_not_called()
        self.assertIn("September 26'", out.getvalue())
        self.assertIn("SE CREARÁ", out.getvalue())


if __name__ == "__main__":
    unittest.main()
