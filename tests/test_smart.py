"""Fase 3: recetas con filtros, redescubrimientos y estadísticas."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import history  # noqa: E402
import jobs  # noqa: E402
import smart  # noqa: E402
import stats  # noqa: E402
from _db import fresh_db  # noqa: E402
from history import PlayEvent  # noqa: E402
from spotify_core import PlaylistResult  # noqa: E402

MADRID = ZoneInfo("Europe/Madrid")
ME = "me"


def ts(iso):
    return history.parse_ts(iso)


def ev(iso, uri="spotify:track:a", artist="Artista", ms=200000, skipped=None, name=None):
    return PlayEvent(ts(iso), uri, name or uri[-1].upper(), artist, ms, skipped)


class SpecTests(unittest.TestCase):
    def test_json_roundtrip_ignores_unknown_keys(self):
        spec = smart.Spec(kind="rediscover", weekdays=[6], hours=[21, 22], limit=20)
        self.assertEqual(smart.Spec.from_json(spec.to_json()), spec)
        self.assertEqual(smart.Spec.from_json('{"limit": 5, "clave_futura": 1}').limit, 5)

    def test_validate(self):
        for bad in (smart.Spec(kind="raro"), smart.Spec(range_type="fixed"),
                    smart.Spec(range_type="fixed", start="2024-02-01", end="2024-01-01"),
                    smart.Spec(days=0), smart.Spec(limit=0)):
            with self.assertRaises(ValueError):
                bad.validate()
        smart.Spec(range_type="fixed", start="2024-01-01", end="2024-01-01").validate()

    def test_describe_in_spanish(self):
        spec = smart.Spec(days=90, weekdays=[6], hours=list(range(18, 24)), avoid_skipped=True,
                          exclude_artists=["X"])
        self.assertEqual(spec.describe(),
                         "Últimos 90 días · domingo · por la noche · sin X · sin las que sueles saltar")
        self.assertIn("Redescubrimientos", smart.Spec(kind="rediscover", range_type="all").describe())
        self.assertIn("horas 1h, 2h", smart.Spec(hours=[1, 2]).describe())


class AggregateTests(unittest.TestCase):
    def test_ranking_counts_ties_by_recency_and_limit(self):
        events = [ev("2024-01-01T10:00:00Z", "spotify:track:a"), ev("2024-01-02T10:00:00Z", "spotify:track:a"),
                  ev("2024-01-03T10:00:00Z", "spotify:track:b"), ev("2024-01-04T10:00:00Z", "spotify:track:c")]
        top = smart.aggregate(events, smart.Spec(limit=2), MADRID)
        self.assertEqual([(t.uri, t.plays) for t in top], [("spotify:track:a", 2), ("spotify:track:c", 1)])

    def test_time_filters_use_local_timezone(self):
        # 2024-03-03 es domingo. 22:30 UTC = 23:30 en Madrid (noche) ; 23:30 UTC = 00:30 del LUNES en Madrid.
        events = [ev("2024-03-03T22:30:00Z", "spotify:track:a"), ev("2024-03-03T23:30:00Z", "spotify:track:b")]
        spec = smart.Spec(weekdays=[6], hours=list(smart.FRANJAS["noche"]))
        self.assertEqual([t.uri for t in smart.aggregate(events, spec, MADRID)], ["spotify:track:a"])
        self.assertEqual({t.uri for t in smart.aggregate(events, smart.Spec(months=[3]), MADRID)},
                         {"spotify:track:a", "spotify:track:b"})
        self.assertEqual(smart.aggregate(events, smart.Spec(months=[7]), MADRID), [])

    def test_artist_filters_are_case_insensitive_substrings(self):
        events = [ev("2024-01-01T10:00:00Z", "spotify:track:a", artist="Bad Bunny"),
                  ev("2024-01-01T11:00:00Z", "spotify:track:b", artist="Rosalía")]
        only = smart.aggregate(events, smart.Spec(include_artists=["bad bun"]), MADRID)
        self.assertEqual([t.uri for t in only], ["spotify:track:a"])
        without = smart.aggregate(events, smart.Spec(exclude_artists=["ROSAL"]), MADRID)
        self.assertEqual([t.uri for t in without], ["spotify:track:a"])

    def test_short_plays_min_plays_and_skips(self):
        events = [
            ev("2024-01-01T10:00:00Z", "spotify:track:a"), ev("2024-01-02T10:00:00Z", "spotify:track:a"),
            ev("2024-01-03T10:00:00Z", "spotify:track:b", ms=5000),     # demasiado corta
            ev("2024-01-04T10:00:00Z", "spotify:track:c", ms=None),     # API: duración desconocida, cuenta
            # d: 3 escuchas válidas, pero la saltaste 3 de 4 veces
            *[ev(f"2024-01-0{i}T12:00:00Z", "spotify:track:d", skipped=1) for i in (5, 6, 7)],
            ev("2024-01-08T12:00:00Z", "spotify:track:d", skipped=0),
        ]
        plain = smart.aggregate(events, smart.Spec(), MADRID)
        self.assertNotIn("spotify:track:b", [t.uri for t in plain])
        self.assertIn("spotify:track:c", [t.uri for t in plain])
        twice = smart.aggregate(events, smart.Spec(min_plays=2), MADRID)
        self.assertEqual({t.uri for t in twice}, {"spotify:track:a", "spotify:track:d"})
        no_skips = smart.aggregate(events, smart.Spec(avoid_skipped=True), MADRID)
        self.assertNotIn("spotify:track:d", [t.uri for t in no_skips])


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.addCleanup(self.conn.close)
        self.now = datetime(2025, 6, 1, 12, tzinfo=timezone.utc)

    def _add(self, when, uri, n=1):
        rows = [(int((when + timedelta(minutes=10 * i)).timestamp()), uri, uri[-1], "Art", "Al", 200000)
                for i in range(n)]
        history.add_plays(self.conn, ME, rows, "export")

    def test_top_last_days(self):
        self._add(self.now - timedelta(days=5), "spotify:track:a", 2)
        self._add(self.now - timedelta(days=50), "spotify:track:b", 5)
        top = smart.build_tracks(self.conn, ME, smart.Spec(days=30), MADRID, now=self.now)
        self.assertEqual([t.uri for t in top], ["spotify:track:a"])

    def test_rediscover_only_forgotten_favourites(self):
        old = self.now - timedelta(days=400)
        self._add(old, "spotify:track:a", 6)                        # olvidada
        self._add(old, "spotify:track:b", 6)                        # ...pero la volviste a escuchar
        self._add(self.now - timedelta(days=10), "spotify:track:b")
        self._add(old, "spotify:track:c", 2)                        # pocas escuchas entonces
        spec = smart.Spec(kind="rediscover", range_type="all", forgotten_months=6, min_plays=5)
        found = smart.build_tracks(self.conn, ME, spec, MADRID, now=self.now)
        self.assertEqual([(t.uri, t.plays) for t in found], [("spotify:track:a", 6)])
        last = smart.last_played(self.conn, ME, ["spotify:track:a"])
        self.assertLess(last["spotify:track:a"], int(self.now.timestamp()))

    def test_rediscover_specific_year(self):
        self._add(datetime(2023, 5, 1, tzinfo=timezone.utc), "spotify:track:a", 5)
        self._add(datetime(2022, 5, 1, tzinfo=timezone.utc), "spotify:track:b", 5)
        spec = smart.Spec(kind="rediscover", range_type="fixed", start="2023-01-01", end="2023-12-31",
                          min_plays=5)
        found = smart.build_tracks(self.conn, ME, spec, MADRID, now=self.now)
        self.assertEqual([t.uri for t in found], ["spotify:track:a"])

    def test_rediscover_range_entirely_inside_forgotten_window_is_empty(self):
        self._add(self.now - timedelta(days=20), "spotify:track:a", 5)
        spec = smart.Spec(kind="rediscover", range_type="last_days", days=30, forgotten_months=6)
        self.assertEqual(smart.build_tracks(self.conn, ME, spec, MADRID, now=self.now), [])


class RecipeJobTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.addCleanup(self.conn.close)

    def test_update_recipes_writes_each_and_survives_bad_one(self):
        now = datetime.now(timezone.utc)
        history.add_plays(self.conn, ME, [(int(now.timestamp()) - 3600, "spotify:track:a", "A", "X", "Al", 60000)],
                          "api")
        history.save_recipe(self.conn, ME, "Buena", smart.Spec(days=7).to_json())
        history.save_recipe(self.conn, ME, "Rota", '{"kind": "nada"}')
        history.save_recipe(self.conn, ME, "Vacía", smart.Spec(include_artists=["nadie"]).to_json())
        written = []

        def fake_write(sp, conn, user_id, name, uris, description, index=None):
            written.append((name, uris, description))
            return PlaylistResult("id", True, len(uris), "u")

        with mock.patch.object(jobs, "write_playlist", side_effect=fake_write):
            outcomes = {o.name: o for o in jobs.update_recipes(object(), self.conn, ME, MADRID, index=object())}
        self.assertEqual([w[0] for w in written], ["Buena"])
        self.assertIn("se actualiza cada día", written[0][2])
        self.assertTrue(outcomes["Buena"].ok)
        self.assertFalse(outcomes["Rota"].ok)
        self.assertIn("no válida", outcomes["Rota"].note)
        self.assertFalse(outcomes["Vacía"].ok)  # sin canciones: la playlist no se toca


class StatsTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(stats.compute([], MADRID).plays, 0)

    def test_overview_and_estimated_hours(self):
        events = [ev("2024-01-01T10:00:00Z", "spotify:track:a", artist="X", ms=3_600_000),
                  ev("2024-01-02T10:00:00Z", "spotify:track:b", artist="Y", ms=None),   # API: se estima
                  ev("2024-01-03T10:00:00Z", "spotify:track:a", artist="X", ms=1000)]   # salto: no cuenta
        result = stats.compute(events, MADRID)
        self.assertEqual((result.plays, result.tracks, result.artists), (2, 2, 2))
        self.assertAlmostEqual(result.hours, 2.0)  # 1 h conocida + 1 estimada con la media (1 h)
        self.assertTrue(result.hours_estimated)
        self.assertEqual(result.top_artists[0], ("X", 1))

    def test_short_period_by_day_with_gaps(self):
        events = [ev("2024-01-01T10:00:00Z"), ev("2024-01-03T10:00:00Z"), ev("2024-01-03T11:00:00Z")]
        result = stats.compute(events, MADRID)
        self.assertEqual(result.period_unit, "day")
        self.assertEqual(result.by_period, [("2024-01-01", 1), ("2024-01-02", 0), ("2024-01-03", 2)])

    def test_long_period_by_month_with_gaps_and_heatmap_in_local_time(self):
        events = [ev("2023-11-30T23:30:00Z"),   # en Madrid ya es viernes 1 de diciembre, 00:30
                  ev("2024-02-10T10:00:00Z")]
        result = stats.compute(events, MADRID)
        self.assertEqual(result.period_unit, "month")
        self.assertEqual(result.by_period, [("2023-12", 1), ("2024-01", 0), ("2024-02", 1)])
        cells = {(d, h): n for d, h, n in result.heatmap}
        self.assertEqual(len(result.heatmap), 7 * 24)
        self.assertEqual(cells[(4, 0)], 1)   # viernes, 00h


if __name__ == "__main__":
    unittest.main()
