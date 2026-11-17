"""Géneros: clasificación de artistas (Spotify + Last.fm), filtro en recetas y cron."""
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from spotipy.exceptions import SpotifyException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cron  # noqa: E402
import genres  # noqa: E402
import history  # noqa: E402
import smart  # noqa: E402
from _db import fresh_db  # noqa: E402
from webapp import forms  # noqa: E402

TZ = ZoneInfo("Europe/Madrid")


def add(conn, rows, user="me"):
    """rows: (ts, uri, artista)"""
    history.add_plays(conn, user, [(t, uri, f"Canción {uri[-2:]}", artist, "Al", 200000)
                                   for t, uri, artist in rows], "export")


class FakeSp:
    """Spotify de mentira: canción -> artista -> géneros."""

    def __init__(self, artist_genres, fail_on=None):
        self.artist_genres = artist_genres   # nombre -> géneros (None = la canción no existe)
        self.fail_on = fail_on               # nombre de artista que provoca un 429
        self.calls = 0

    def track(self, track_id):
        self.calls += 1
        name = track_id.split("-")[0]
        if name == self.fail_on:
            raise SpotifyException(429, -1, "Too many requests")
        if self.artist_genres.get(name) is None:
            raise SpotifyException(404, -1, "Not found")
        return {"artists": [{"id": f"id-{name}", "name": name.capitalize()}]}

    def artist(self, artist_id):
        self.calls += 1
        return {"genres": self.artist_genres[artist_id[3:]]}


class TextTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(genres.normalize("  Hip-Hop  Español "), "hip hop español")

    def test_matches_by_whole_words(self):
        self.assertTrue(genres.matches(["indie rock", "pop"], ["rock"]))
        self.assertTrue(genres.matches(["rock en español"], ["Rock"]))
        self.assertTrue(genres.matches(["hip hop"], ["hip-hop"]))
        self.assertFalse(genres.matches(["rockabilly"], ["rock"]))
        self.assertFalse(genres.matches([], ["rock"]))
        self.assertFalse(genres.matches(None, ["rock"]))


class LastfmTests(unittest.TestCase):
    def opener(self, payload):
        def fake(url, timeout=10):
            self.url = url
            resp = mock.MagicMock()
            resp.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            return resp
        return fake

    def test_keeps_real_genres_only(self):
        payload = {"toptags": {"tag": [
            {"name": "Indie Rock", "count": 100}, {"name": "seen live", "count": 90},
            {"name": "Mi Grupo", "count": 80}, {"name": "post-punk", "count": 40},
            {"name": "raro", "count": 3},
        ]}}
        values = genres.lastfm_genres("Mi Grupo", "clave", opener=self.opener(payload))
        self.assertEqual(values, ["indie rock", "post punk"])
        self.assertIn("artist=Mi+Grupo", self.url)

    def test_unknown_artist_gives_nothing_but_bad_key_or_network_raise(self):
        self.assertEqual(genres.lastfm_genres("X", "c", opener=self.opener({"error": 6, "message": "no"})), [])
        with self.assertRaises(genres.LastfmError):
            genres.lastfm_genres("X", "c", opener=self.opener({"error": 10, "message": "Invalid API key"}))

        def broken(url, timeout=10):
            raise genres.urllib.error.URLError("sin red")
        with self.assertRaises(genres.LastfmError):
            genres.lastfm_genres("X", "c", opener=broken)


class TagTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.addCleanup(self.conn.close)
        patcher = mock.patch.object(genres, "PAUSE_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        # rockeros: 3 escuchas; poperos: 2; nadie: 1 (Spotify sin géneros); borrado: canción retirada
        add(self.conn, [(1000 + i, "spotify:track:rockeros-01", "Rockeros") for i in range(3)]
            + [(2000 + i, "spotify:track:poperos-02", "Poperos") for i in range(2)]
            + [(3000, "spotify:track:nadie-03", "Nadie"), (4000, "spotify:track:borrado-04", "Borrado")])
        self.sp = FakeSp({"rockeros": ["Indie Rock", "rock"], "poperos": ["pop"], "nadie": [], "borrado": None})

    def test_pending_most_listened_first(self):
        names = [p.name for p in genres.pending_artists(self.conn, "me", 10, with_lastfm=False)]
        self.assertEqual(names[:2], ["Rockeros", "Poperos"])

    def test_tags_whole_history_and_falls_back_to_lastfm(self):
        with mock.patch.object(genres, "lastfm_genres", side_effect=lambda name, key: ["flamenco"]
                               if name == "Borrado" else []) as lastfm:
            result = genres.tag_artists(self.sp, self.conn, "me", lastfm_key="clave")
        self.assertEqual((result.processed, result.with_genre), (4, 3))
        found = genres.genres_for(self.conn, ["Rockeros", "rockeros", "Nadie", "Borrado"])
        self.assertEqual(found["Rockeros"], ["indie rock", "rock"])
        self.assertEqual(found["rockeros"], ["indie rock", "rock"])  # mismo artista, otra grafía
        self.assertEqual(found["Borrado"], ["flamenco"])
        self.assertEqual(found["Nadie"], [])
        # Last.fm solo para los que Spotify no clasificó.
        self.assertEqual(sorted(c.args[0] for c in lastfm.call_args_list), ["Borrado", "Nadie"])
        # Nada pendiente: la siguiente tanda no llama a nadie.
        calls = self.sp.calls
        self.assertEqual(genres.tag_artists(self.sp, self.conn, "me", lastfm_key="clave").processed, 0)
        self.assertEqual(self.sp.calls, calls)

    def test_without_lastfm_key_retries_later_only_with_lastfm(self):
        genres.tag_artists(self.sp, self.conn, "me", lastfm_key="")
        calls = self.sp.calls
        with mock.patch.object(genres, "lastfm_genres", return_value=["copla"]):
            result = genres.tag_artists(self.sp, self.conn, "me", lastfm_key="clave")
        self.assertEqual(result.processed, 2)          # Nadie y Borrado, ahora con Last.fm
        self.assertEqual(self.sp.calls, calls)         # sin volver a preguntar a Spotify
        self.assertEqual(genres.genres_for(self.conn, ["Nadie"])["Nadie"], ["copla"])

    def test_lastfm_failure_never_marks_artist_without_genre(self):
        with mock.patch.object(genres, "lastfm_genres", side_effect=genres.LastfmError("caído")):
            result = genres.tag_artists(self.sp, self.conn, "me", lastfm_key="clave")
        self.assertIn("Last.fm", result.stopped)
        with mock.patch.object(genres, "lastfm_genres", return_value=["copla"]):
            genres.tag_artists(self.sp, self.conn, "me", lastfm_key="clave")
        found = genres.genres_for(self.conn, ["Nadie", "Borrado"])
        self.assertEqual(found, {"Nadie": ["copla"], "Borrado": ["copla"]})

    def test_rate_limit_stops_and_keeps_progress(self):
        self.sp.fail_on = "poperos"
        result = genres.tag_artists(self.sp, self.conn, "me", lastfm_key="")
        self.assertEqual(result.processed, 1)
        self.assertIn("esperar", result.stopped)
        self.assertEqual(genres.coverage(self.conn, "me").pending, 3)

    def test_batch_limit(self):
        self.assertEqual(genres.tag_artists(self.sp, self.conn, "me", max_artists=2, lastfm_key="").processed, 2)

    def test_coverage_and_top_genres(self):
        genres.tag_artists(self.sp, self.conn, "me", lastfm_key="")
        cov = genres.coverage(self.conn, "me")
        self.assertEqual((cov.artists, cov.tagged, cov.with_genre, cov.pending), (4, 4, 2, 0))
        self.assertEqual(cov.percent_plays, round(100 * 5 / 7))
        self.assertEqual(genres.top_genres(self.conn, "me"), [("indie rock", 3), ("rock", 3), ("pop", 2)])


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.addCleanup(self.conn.close)
        add(self.conn, [(1000 + i, "spotify:track:r1", "Rockeros") for i in range(3)]
            + [(2000 + i, "spotify:track:p1", "Poperos") for i in range(2)]
            + [(3000, "spotify:track:x1", "Sin clasificar")])
        genres._save(self.conn, "rockeros", "Rockeros", None, ["indie rock"], genres.SOURCE_SPOTIFY)
        genres._save(self.conn, "poperos", "Poperos", None, ["pop", "dance pop"], genres.SOURCE_SPOTIFY)

    def build(self, **kw):
        spec = smart.Spec(range_type="all", min_seconds=0, **kw)
        return [t.uri for t in smart.build_tracks(self.conn, "me", spec, TZ)]

    def test_include_and_exclude_genres(self):
        self.assertEqual(self.build(include_genres=["rock"]), ["spotify:track:r1"])
        self.assertEqual(self.build(include_genres=["Pop", "rock"]), ["spotify:track:r1", "spotify:track:p1"])
        # Un artista sin género conocido no entra en «solo» y sí en «sin».
        self.assertEqual(self.build(exclude_genres=["pop"]), ["spotify:track:r1", "spotify:track:x1"])

    def test_describe_and_old_recipes(self):
        spec = smart.Spec(include_genres=["rock"], exclude_genres=["pop"])
        self.assertIn("género: rock", spec.describe())
        self.assertIn("sin el género pop", spec.describe())
        old = smart.Spec.from_json('{"kind": "top", "days": 30}')  # receta guardada antes de los géneros
        self.assertEqual(old.include_genres, [])

    def test_form_merges_pills_and_text(self):
        spec, _ = forms.custom_spec({"g": ["indie rock", "pop"], "gen": "pop, flamenco", "xgen": "trap"},
                                    date(2026, 3, 1), 30)
        self.assertEqual(spec.include_genres, ["indie rock", "pop", "flamenco"])
        self.assertEqual(spec.exclude_genres, ["trap"])


class CronGenresTests(unittest.TestCase):
    def run_cron(self, **kw):
        conn = fresh_db()
        self.addCleanup(conn.raw.close)
        conn.close = mock.Mock()
        out = io.StringIO()
        with mock.patch.object(cron, "use_db_tokens", return_value=False), \
                mock.patch.object(cron.history, "connect", return_value=conn), \
                mock.patch.object(cron, "get_spotify_client") as client, \
                mock.patch.object(cron.jobs, "record_recent_plays", return_value=(0, 0)), redirect_stdout(out):
            client.return_value.current_user.return_value = {"id": "me"}
            code = cron.run(**kw)
        return code, out.getvalue()

    def test_hourly_record_tags_a_small_batch(self):
        with mock.patch.object(cron.genres, "tag_artists", return_value=genres.TagResult(5, 4)) as tag:
            code, out = self.run_cron(record_only=True)
        self.assertEqual(code, 0)
        self.assertEqual(tag.call_args.kwargs["max_artists"], cron.GENRES_SMALL[0])
        self.assertIn("Géneros: 5 artistas", out)

    def test_genre_failure_never_breaks_the_record(self):
        with mock.patch.object(cron.genres, "tag_artists", side_effect=RuntimeError("Last.fm caído")):
            code, out = self.run_cron(record_only=True)
        self.assertEqual(code, 0)
        self.assertIn("no se pudieron clasificar", out)

    def test_big_batch_mode(self):
        with mock.patch.object(cron.genres, "tag_artists", return_value=genres.TagResult()) as tag:
            code, _ = self.run_cron(genres_only=True)
        self.assertEqual(code, 0)
        self.assertEqual(tag.call_args.kwargs["max_artists"], cron.GENRES_BIG[0])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cron.run(genres_only=True, months=True), 1)


if __name__ == "__main__":
    unittest.main()
