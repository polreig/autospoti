import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import cron  # noqa: E402
import history  # noqa: E402
from _db import fresh_db  # noqa: E402
import jobs  # noqa: E402
from spotify_core import PlaylistResult  # noqa: E402


def fake_result(name="x"):
    return PlaylistResult(playlist_id="id", created=False, track_count=3, url="u")


class ApiOnlyTests(unittest.TestCase):
    def test_api_only_updates_only_three_playlists_without_db(self):
        written = []

        def fake_create(sp, user_id, name, uris, description="", **kwargs):
            written.append(name)
            return fake_result(name)

        recent = [{"track": {"id": "1", "uri": "spotify:track:1"}}]
        top = [{"id": "2", "uri": "spotify:track:2"}]
        with mock.patch.object(jobs, "get_recently_played", return_value=recent), \
                mock.patch.object(jobs, "get_top_tracks", return_value=top), \
                mock.patch.object(jobs, "create_or_update_playlist", side_effect=fake_create):
            outcomes = jobs.update_auto_playlists(object(), None, "me", api_only=True)

        self.assertEqual(written, [jobs.NAME_RECENT, jobs.NAME_4W, jobs.NAME_6M])
        self.assertTrue(all(o.ok for o in outcomes))
        # Las que dependen del registro propio NO se tocan (no se pisan con datos incompletos).
        self.assertNotIn(jobs.NAME_7D, written)
        self.assertNotIn(jobs.NAME_YEAR, written)

    def test_full_mode_without_db_raises(self):
        with self.assertRaises(ValueError):
            jobs.update_auto_playlists(object(), None, "me", api_only=False)

    def test_full_mode_updates_five_playlists(self):
        written = []
        conn = fresh_db()
        self.addCleanup(conn.close)

        def fake_create(sp, user_id, name, uris, description="", **kwargs):
            written.append(name)
            return fake_result(name)

        recent = [{"track": {"id": "1", "uri": "spotify:track:1"}, "played_at": "2024-01-01T10:00:00Z"}]
        top = [{"id": "2", "uri": "spotify:track:2"}]
        with mock.patch.object(jobs, "get_recently_played", return_value=recent), \
                mock.patch.object(jobs, "get_top_tracks", return_value=top), \
                mock.patch.object(jobs, "create_or_update_playlist", side_effect=fake_create):
            outcomes = jobs.update_auto_playlists(object(), conn, "me")

        # Sin historial suficiente, 7 días queda sin actualizar pero no vacía nada.
        self.assertIn(jobs.NAME_RECENT, written)
        self.assertIn(jobs.NAME_4W, written)
        self.assertIn(jobs.NAME_6M, written)
        self.assertIn(jobs.NAME_YEAR, written)  # cae al ranking long_term de Spotify
        self.assertEqual(len(outcomes), 5)
        seven = [o for o in outcomes if o.name == jobs.NAME_7D][0]
        self.assertFalse(seven.ok)

    def test_last_year_uses_history_only_when_it_covers_the_year(self):
        from datetime import datetime, timedelta, timezone

        written = []
        conn = fresh_db()
        self.addCleanup(conn.close)
        now = datetime.now(timezone.utc)
        recent_ts = int((now - timedelta(days=10)).timestamp())
        old_ts = int((now - timedelta(days=400)).timestamp())
        conn.executemany(
            "INSERT INTO plays (user_id, ts, track_uri, track_name, artist, album, ms_played, source) "
            "VALUES ('me', ?, ?, 'S', 'A', 'Al', 60000, 'export')",
            [(old_ts, "spotify:track:old"), (recent_ts, "spotify:track:new")],
        )
        conn.commit()

        def fake_create(sp, user_id, name, uris, description="", **kwargs):
            written.append((name, list(uris)))
            return fake_result(name)

        with mock.patch.object(jobs, "get_recently_played", return_value=[]), \
                mock.patch.object(jobs, "get_top_tracks", return_value=[]), \
                mock.patch.object(jobs, "create_or_update_playlist", side_effect=fake_create):
            jobs.update_auto_playlists(object(), conn, "me")

        year = dict(written).get(jobs.NAME_YEAR)
        # Cubre el año (dato de hace 400 días) y hay escuchas dentro: sale del registro,
        # y solo incluye la canción reciente, no la antigua.
        self.assertEqual(year, ["spotify:track:new"])

    def test_one_index_per_run_and_ids_remembered(self):
        conn = fresh_db()
        self.addCleanup(conn.close)
        indexes = []

        def fake_create(sp, user_id, name, uris, description="", **kwargs):
            indexes.append(kwargs["index"])
            return PlaylistResult(playlist_id=f"id-{name}", created=True, track_count=1, url="u")

        recent = [{"track": {"id": "1", "uri": "spotify:track:1"}, "played_at": "2024-01-01T10:00:00Z"}]
        top = [{"id": "2", "uri": "spotify:track:2"}]
        with mock.patch.object(jobs, "get_recently_played", return_value=recent), \
                mock.patch.object(jobs, "get_top_tracks", return_value=top), \
                mock.patch.object(jobs, "create_or_update_playlist", side_effect=fake_create):
            jobs.update_auto_playlists(object(), conn, "me")

        self.assertGreaterEqual(len(indexes), 4)
        self.assertTrue(all(i is indexes[0] for i in indexes))
        self.assertEqual(history.get_playlist_id(conn, "me", jobs.NAME_4W), f"id-{jobs.NAME_4W}")
        # La escucha reciente se registró a nombre del usuario.
        self.assertEqual(history.coverage(conn, "me").total, 1)


class WritePlaylistTests(unittest.TestCase):
    def test_passes_remembered_id(self):
        conn = fresh_db()
        self.addCleanup(conn.close)
        history.save_playlist_id(conn, "me", "Mi lista", "p1")
        with mock.patch.object(jobs, "create_or_update_playlist",
                               return_value=PlaylistResult("p1", False, 1, "u")) as create:
            jobs.write_playlist(object(), conn, "me", "Mi lista", ["spotify:track:1"], "d")
        self.assertEqual(create.call_args.kwargs["playlist_id"], "p1")

    def test_without_db_works_by_name(self):
        with mock.patch.object(jobs, "create_or_update_playlist",
                               return_value=PlaylistResult("p9", True, 1, "u")) as create:
            res = jobs.write_playlist(object(), None, "me", "L", ["spotify:track:1"], "d")
        self.assertIsNone(create.call_args.kwargs["playlist_id"])
        self.assertEqual(res.playlist_id, "p9")

    def test_empty_result_not_saved(self):
        conn = fresh_db()
        self.addCleanup(conn.close)
        with mock.patch.object(jobs, "create_or_update_playlist",
                               return_value=PlaylistResult(None, False, 0)):
            jobs.write_playlist(object(), conn, "me", "L", [], "d")
        self.assertIsNone(history.get_playlist_id(conn, "me", "L"))


class CronTests(unittest.TestCase):
    def test_record_and_api_only_are_exclusive(self):
        self.assertEqual(cron.run(record_only=True, api_only=True), 1)

    def test_api_only_does_not_open_database(self):
        outcome = jobs.JobOutcome("n", fake_result())
        with mock.patch.object(cron, "get_spotify_client") as client, \
                mock.patch.object(cron.history, "connect") as connect, \
                mock.patch.object(cron.jobs, "update_auto_playlists", return_value=[outcome]) as upd:
            client.return_value.current_user.return_value = {"id": "me"}
            code = cron.run(api_only=True)
        self.assertEqual(code, 0)
        connect.assert_not_called()
        self.assertTrue(upd.call_args.kwargs["api_only"])

    def test_failed_playlist_gives_exit_code_2(self):
        outcome = jobs.JobOutcome("n", None, "sin datos")
        with mock.patch.object(cron, "get_spotify_client") as client, \
                mock.patch.object(cron.jobs, "update_auto_playlists", return_value=[outcome]):
            client.return_value.current_user.return_value = {"id": "me"}
            self.assertEqual(cron.run(api_only=True), 2)


class ConfigTests(unittest.TestCase):
    def test_history_enabled_flag(self):
        for value, expected in (("", True), ("true", True), ("false", False), ("0", False),
                                ("No", False), ("OFF", False)):
            with mock.patch.dict(os.environ, {"HISTORY_ENABLED": value}):
                self.assertEqual(config.history_enabled(), expected, value)


if __name__ == "__main__":
    unittest.main()
