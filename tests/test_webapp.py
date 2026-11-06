"""Fase 5: la web FastAPI (login, sesiones, páginas, permisos, CSRF, recetas, pareja, importar)."""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

import blend  # noqa: E402
import history  # noqa: E402
import smart  # noqa: E402
import tokens  # noqa: E402
import web_auth  # noqa: E402
from _db import fresh_db  # noqa: E402
from spotify_core import PlaylistResult  # noqa: E402
from webapp import forms  # noqa: E402

TOKEN = {"access_token": "a", "refresh_token": "r", "expires_at": 9999999999}


@unittest.skipIf(TestClient is None, "fastapi/httpx no instalados")
class WebAppTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        url = os.environ.get("TEST_DATABASE_URL", "").strip() or str(Path(tmp.name) / "web.db")
        env = {"SPOTIPY_CLIENT_ID": "cliente", "SPOTIPY_CLIENT_SECRET": "secreto", "TOKEN_ENCRYPTION_KEY": "",
               "SESSION_SECRET": "", "DATABASE_URL": url, "WEB_REDIRECT_URI": "http://testserver/callback",
               "ALLOWED_SPOTIFY_IDS": "", "SPOTIFY_CACHE": ""}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.db = fresh_db() if os.environ.get("TEST_DATABASE_URL") else history.connect(url)
        self.addCleanup(self.db.close)
        from webapp import main

        self.main = main
        self.addCleanup(main.close_all_connections)
        self.app = main.create_app()
        now = int(time.time())
        rows = [(now - 86400 * 5 * i, f"spotify:track:{i % 6}", f"Canción {i % 6}", f"Artista {i % 3}", "Al", 200000)
                for i in range(80)]
        rows += [(now - 86400 * 500 - 600 * i, "spotify:track:vieja", "Vieja", "Antes", "Al", 200000) for i in range(6)]
        history.add_plays(self.db, "pol", rows, "export")
        history.add_plays(self.db, "ana", [(r[0] + 7, *r[1:]) for r in rows[:40]], "export")

    def client(self, user="pol", name="Pol"):
        client = TestClient(self.app)
        if user:
            tokens.save_token(self.db, user, TOKEN)

            def fake_login(conn, code, state):
                tokens.save_profile(conn, user, name)
                return web_auth.LoginResult(user, name)

            with mock.patch.object(web_auth, "complete_login", side_effect=fake_login):
                r = client.get("/callback?code=c&state=s", follow_redirects=False)
            self.assertEqual(r.status_code, 303)
        return client

    # -------------------------------------------------------------- login y sesión

    def test_anonymous_sees_login_and_is_redirected(self):
        client = self.client(user=None)
        r = client.get("/")
        self.assertIn("Iniciar sesión con Spotify", r.text)
        for path in ("/stats", "/custom", "/blend", "/recipes", "/import", "/account"):
            r = client.get(path, follow_redirects=False)
            self.assertEqual((r.status_code, r.headers["location"]), (303, "/"), path)
        r = client.post("/api/import", json={"entries": []})
        self.assertEqual(r.status_code, 401)

    def test_login_redirects_to_spotify(self):
        r = self.client(user=None).get("/login", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("accounts.spotify.com/authorize", r.headers["location"])
        self.assertIn("redirect_uri=http%3A%2F%2Ftestserver%2Fcallback", r.headers["location"])

    def test_callback_errors_are_shown(self):
        client = self.client(user=None)
        r = client.get("/callback?error=access_denied")
        self.assertIn("Has cancelado", r.text)
        r = client.get("/callback?code=c&state=falso")
        self.assertIn("no es válido", r.text)

    def test_session_persists_and_logout(self):
        client = self.client()
        self.assertIn("Hola, Pol", client.get("/").text)
        self.assertIn("Hola, Pol", client.get("/").text)  # sigue dentro al recargar
        client.post("/logout")
        self.assertIn("Iniciar sesión con Spotify", client.get("/").text)

    def test_forget_requires_confirmation_and_ends_session(self):
        client = self.client()
        r = client.post("/account/forget", data={})
        self.assertIn("Marca la casilla", r.text)
        self.assertIsNotNone(tokens.load_token(self.db, "pol"))
        r = client.post("/account/forget", data={"confirm": "1"})
        self.assertIn("Cuenta olvidada", r.text)
        self.assertIsNone(tokens.load_token(self.db, "pol"))
        self.assertEqual(client.get("/stats", follow_redirects=False).status_code, 303)

    def test_removed_from_allowlist_is_kicked_out(self):
        client = self.client()
        with mock.patch.dict(os.environ, {"ALLOWED_SPOTIFY_IDS": "otra"}):
            self.assertEqual(client.get("/stats", follow_redirects=False).status_code, 303)

    def test_cross_origin_post_is_rejected(self):
        client = self.client()
        r = client.post("/logout", headers={"Origin": "https://malo.example"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("Hola", client.get("/").text)  # la sesión sigue

    def test_display_name_is_escaped(self):
        client = self.client(name="<script>alert(1)</script>")
        text = client.get("/").text
        self.assertNotIn("<script>alert(1)</script>", text)
        self.assertIn("&lt;script&gt;", text)

    # -------------------------------------------------------------- páginas

    def test_every_page_renders(self):
        client = self.client()
        pages = ["/", "/stats", "/stats?period=todo", "/stats?period=30d", "/stats?period=anio&year=2025",
                 "/custom", "/custom?period=mes&month=2&year=2025", "/custom?period=todo&d=6&f=noche&avoid=1",
                 "/custom?period=fechas&start=2025-01-01&end=2025-03-01", "/rediscover",
                 "/rediscover?epoch=anio&year=2024&forgotten=12", "/blend", "/recipes", "/top", "/import",
                 "/account", "/static/app.css", "/static/app.js"]
        for path in pages:
            r = client.get(path)
            self.assertEqual(r.status_code, 200, path)

    def test_stats_embeds_three_charts(self):
        r = self.client().get("/stats?period=todo")
        payload = r.text.split('id="chart-specs">')[1].split("</script>")[0]
        self.assertEqual(sorted(json.loads(payload)), ["artists", "heatmap", "period"])

    def test_rediscover_finds_forgotten_song(self):
        r = self.client().get("/rediscover?min_plays=5")
        self.assertIn("Vieja", r.text)

    def test_bad_filters_show_message_not_500(self):
        client = self.client()
        r = client.get("/custom?period=fechas&start=2025-05-01&end=2025-01-01")
        self.assertEqual(r.status_code, 200)
        self.assertIn("anterior a la inicial", r.text)
        r = client.get("/custom?limit=abc&days=-5&d=99&m=xx")
        self.assertEqual(r.status_code, 200)

    # -------------------------------------------------------------- crear y recetas

    def test_create_playlist_and_keep_as_recipe(self):
        client = self.client()
        spec = smart.Spec(days=60, limit=10).to_json()
        written = []

        def fake_write(sp, conn, user_id, name, uris, description, index=None):
            written.append((user_id, name, len(uris), description))
            return PlaylistResult("pl1", True, len(uris), "https://open.spotify.com/playlist/pl1")

        with mock.patch.object(self.main, "spotify_for", return_value=mock.Mock()), \
                mock.patch("jobs.write_playlist", side_effect=fake_write):
            r = client.post("/playlists/create", data={"name": "Mi lista", "spec": spec, "keep": "1"},
                            headers={"Referer": "http://testserver/custom?days=60"})
        self.assertEqual(written[0][:2], ("pol", "Mi lista"))
        self.assertIn("se actualiza cada día", written[0][3])
        self.assertIn("guardada en Mis recetas", r.text)
        self.assertEqual([n for n, _ in history.list_recipes(self.db, "pol")], ["Mi lista"])

        with mock.patch.object(self.main, "spotify_for", return_value=mock.Mock()), \
                mock.patch("jobs.write_playlist", side_effect=fake_write):
            client.post("/recipes/run", data={"name": "Mi lista"})
            client.post("/recipes/run-all")
        self.assertEqual(len(written), 3)
        client.post("/recipes/delete", data={"name": "Mi lista"})
        self.assertEqual(history.list_recipes(self.db, "pol"), [])

    def test_create_rejects_bad_spec_and_foreign_redirects(self):
        client = self.client()
        r = client.post("/playlists/create", data={"name": "X", "spec": '{"kind": "raro"}'},
                        headers={"Referer": "https://malo.example/robar"})
        self.assertEqual(r.url.path, "/")  # nunca redirige fuera de la web
        self.assertIn("desconocido", r.text)

    def test_top_create(self):
        client = self.client()
        with mock.patch.object(self.main, "spotify_for", return_value=mock.Mock()), \
                mock.patch.object(self.main, "get_top_tracks", return_value=[{"id": "1", "uri": "spotify:track:1"}]), \
                mock.patch("jobs.write_playlist", return_value=PlaylistResult("p", True, 1, "u")) as write:
            r = client.post("/top/create", data={"time_range": "short_term", "name": "Top", "limit": "20"})
        write.assert_called_once()
        self.assertIn("Top: creada", r.text)

    # -------------------------------------------------------------- pareja

    def test_blend_invite_accept_preview_and_remove(self):
        pol = self.client()
        ana = self.client("ana", "Ana")
        pol.post("/blend/invite", data={"partner_id": "ana"})
        self.assertIn("Invitaciones recibidas", ana.get("/blend").text)
        self.assertFalse(blend.is_linked(self.db, "pol", "ana"))
        ana.post("/blend/accept", data={"from_id": "pol"})
        self.assertTrue(blend.is_linked(self.db, "pol", "ana"))
        page = pol.get("/blend?mode=comunes&days=todo").text
        self.assertIn("Mezcla con Ana", page)
        self.assertIn("Canción", page)
        pol.post("/blend/remove", data={"other_id": "ana"})
        self.assertFalse(blend.is_linked(self.db, "pol", "ana"))

    def test_blend_cannot_target_unlinked_user(self):
        tokens.save_token(self.db, "intruso", TOKEN)
        page = self.client().get("/blend?partner=ana").text
        self.assertNotIn("Crear / actualizar", page)  # sin enlace no hay vista previa
        spec = smart.Spec(kind="blend", partner="ana", days=365).to_json()
        with mock.patch.object(self.main, "spotify_for", return_value=mock.Mock()), \
                mock.patch("jobs.write_playlist") as write:
            self.client().post("/playlists/create", data={"name": "X", "spec": spec})
        write.assert_not_called()

    # -------------------------------------------------------------- importar

    def test_import_batches(self):
        client = self.client()
        entries = [{"ts": "2021-05-01T10:00:00Z", "ms_played": 200000, "spotify_track_uri": "spotify:track:imp",
                    "master_metadata_track_name": "Importada", "master_metadata_album_artist_name": "X",
                    "skipped": False},
                   {"ts": "2021-05-01T11:00:00Z", "spotify_track_uri": None},
                   {"trackName": "viejo", "endTime": "2021-01-01 10:00"}]
        r = client.post("/api/import", json={"entries": entries})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual((body["inserted"], body["no_uri"], body["legacy_format"]), (1, 1, 1))
        self.assertEqual(client.post("/api/import", json={"entries": entries}).json()["duplicates"], 1)
        self.assertEqual(client.post("/api/import", json={"nada": 1}).status_code, 400)
        big = {"entries": [{}] * (self.main.IMPORT_BATCH_MAX + 1)}
        self.assertEqual(client.post("/api/import", json=big).status_code, 413)


class FormsTests(unittest.TestCase):
    TODAY = date(2026, 3, 15)

    def test_custom_month_period(self):
        spec, name = forms.custom_spec({"period": "mes", "month": "2", "year": "2024", "m": ["6"]}, self.TODAY, 30)
        self.assertEqual((spec.range_type, spec.start, spec.end), ("fixed", "2024-02-01", "2024-02-29"))
        self.assertEqual(spec.months, [])  # el mes ya acota
        self.assertEqual(name, "Mi selección - Febrero 2024")

    def test_custom_values_are_clamped_and_cleaned(self):
        spec, _ = forms.custom_spec({"days": "99999", "limit": "3", "d": ["1", "9", "x"], "f": ["noche", "raro"],
                                     "inc": " a , ,b ", "avoid": "1"}, self.TODAY, 30)
        self.assertEqual((spec.days, spec.limit, spec.weekdays), (3650, 10, [1]))
        self.assertEqual(spec.hours, list(range(18, 24)))
        self.assertEqual(spec.include_artists, ["a", "b"])
        self.assertTrue(spec.avoid_skipped)

    def test_custom_invalid_dates(self):
        with self.assertRaises(ValueError):
            forms.custom_spec({"period": "fechas", "start": "nope", "end": "2024-01-01"}, self.TODAY, 30)

    def test_rediscover_year(self):
        spec, name = forms.rediscover_spec({"epoch": "anio", "year": "2022", "forgotten": "12"}, 30)
        self.assertEqual((spec.start, spec.end, spec.forgotten_months), ("2022-01-01", "2022-12-31", 12))
        self.assertEqual(name, "Redescubrimientos 2022")

    def test_blend_partner_must_be_linked(self):
        self.assertIsNone(forms.blend_spec({}, [], {}, 30))
        spec, name = forms.blend_spec({"partner": "intruso", "mode": "mitad", "days": "todo"}, ["ana"],
                                      {"ana": "Ana"}, 30)
        self.assertEqual((spec.partner, spec.blend_mode, spec.range_type), ("ana", "mitad", "all"))
        self.assertEqual(name, "Mezcla con Ana")


if __name__ == "__main__":
    unittest.main()
