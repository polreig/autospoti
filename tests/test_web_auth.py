"""Fase 2: login con Spotify en la web (OAuth) y la app Streamlit en modo nube."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import spotify_core  # noqa: E402
import tokens  # noqa: E402
import web_auth  # noqa: E402
from _db import fresh_db  # noqa: E402

ENV = {"SPOTIPY_CLIENT_ID": "cliente", "SPOTIPY_CLIENT_SECRET": "secreto-de-prueba",
       "TOKEN_ENCRYPTION_KEY": "", "ALLOWED_SPOTIFY_IDS": "", "WEB_REDIRECT_URI": "https://app.example/"}
TOKEN = {"access_token": "a", "refresh_token": "r", "expires_at": 9999999999,
         "scope": spotify_core.SCOPE, "token_type": "Bearer"}


def fake_oauth_factory(token=TOKEN):
    """Sustituye a SpotifyOAuth: al canjear el código deja el token en la caché recibida."""
    def factory(cache):
        oauth = mock.Mock()
        oauth.get_access_token.side_effect = lambda **kw: cache.save_token_to_cache(token)
        return oauth
    return factory


def fake_spotify(user_id="pol", name="Pol"):
    sp = mock.Mock()
    sp.current_user.return_value = {"id": user_id, "display_name": name}
    return sp


@mock.patch.dict(os.environ, ENV)
class StateTests(unittest.TestCase):
    def test_valid_state(self):
        self.assertTrue(web_auth.verify_state(web_auth.make_state()))

    def test_tampered_or_garbage_state(self):
        state = web_auth.make_state()
        issued, nonce, sig = state.split(".")
        self.assertFalse(web_auth.verify_state(f"{int(issued) + 1}.{nonce}.{sig}"))
        for bad in ("", "abc", "a.b.c", "1.2.3.4"):
            self.assertFalse(web_auth.verify_state(bad), bad)

    def test_expired_state(self):
        state = web_auth.make_state(now=1_000_000)
        self.assertTrue(web_auth.verify_state(state, now=1_000_000 + 60))
        self.assertFalse(web_auth.verify_state(state, now=1_000_000 + web_auth.STATE_MAX_AGE + 1))

    def test_state_signed_with_client_secret(self):
        state = web_auth.make_state()
        with mock.patch.dict(os.environ, {"SPOTIPY_CLIENT_SECRET": "otro"}):
            self.assertFalse(web_auth.verify_state(state))

    def test_authorize_url_uses_web_redirect(self):
        url = web_auth.authorize_url()
        self.assertIn("redirect_uri=https%3A%2F%2Fapp.example%2F", url)
        self.assertIn("state=", url)


@mock.patch.dict(os.environ, ENV)
class AllowListTests(unittest.TestCase):
    def test_empty_list_allows_everyone(self):
        self.assertIsNone(web_auth.allowed_ids())
        self.assertTrue(web_auth.is_allowed("cualquiera"))

    def test_list_is_enforced(self):
        with mock.patch.dict(os.environ, {"ALLOWED_SPOTIFY_IDS": " pol , ana ,"}):
            self.assertEqual(web_auth.allowed_ids(), {"pol", "ana"})
            self.assertTrue(web_auth.is_allowed("ana"))
            self.assertFalse(web_auth.is_allowed("intruso"))


@mock.patch.dict(os.environ, ENV)
class CompleteLoginTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.addCleanup(self.conn.close)

    def _login(self, user_id="pol", state=None):
        with mock.patch.object(web_auth, "_oauth", side_effect=fake_oauth_factory()), \
                mock.patch.object(web_auth.spotipy, "Spotify", return_value=fake_spotify(user_id)):
            return web_auth.complete_login(self.conn, "codigo", state or web_auth.make_state())

    def test_success_saves_encrypted_token(self):
        result = self._login()
        self.assertEqual((result.user_id, result.display_name), ("pol", "Pol"))
        self.assertEqual(tokens.load_token(self.conn, "pol"), TOKEN)

    def test_bad_state_rejected_before_calling_spotify(self):
        with mock.patch.object(web_auth, "_oauth") as oauth:
            with self.assertRaises(web_auth.LoginError):
                web_auth.complete_login(self.conn, "codigo", "falso")
        oauth.assert_not_called()

    def test_not_allowed_user_gets_no_token(self):
        with mock.patch.dict(os.environ, {"ALLOWED_SPOTIFY_IDS": "ana"}):
            with self.assertRaises(web_auth.LoginError):
                self._login("pol")
        self.assertEqual(tokens.list_accounts(self.conn), [])

    def test_spotify_failure_becomes_login_error(self):
        def broken(cache):
            oauth = mock.Mock()
            oauth.get_access_token.side_effect = spotify_core.SpotifyOauthError("invalid_grant")
            return oauth

        with mock.patch.object(web_auth, "_oauth", side_effect=broken):
            with self.assertRaises(web_auth.LoginError):
                web_auth.complete_login(self.conn, "codigo", web_auth.make_state())

    def test_delete_token(self):
        self._login()
        tokens.delete_token(self.conn, "pol")
        self.assertEqual(tokens.list_accounts(self.conn), [])


class AppTests(unittest.TestCase):
    """La app real, sin navegador (streamlit.testing), con BD SQLite temporal en modo nube."""

    def setUp(self):
        try:
            from streamlit.testing.v1 import AppTest  # noqa: F401
        except ImportError:  # pragma: no cover
            self.skipTest("streamlit no instalado")
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.apps = []
        self.addCleanup(self._close_app_connections)
        env = dict(ENV, DATABASE_URL=str(Path(tmp.name) / "app.db"), SPOTIFY_CACHE="", APP_PASSWORD="")
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _app(self):
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(str(ROOT / "autoplaylist.py"), default_timeout=30)
        self.apps.append(at)
        return at

    def _close_app_connections(self):
        # La app guarda su conexión en la sesión; en Windows bloquea el fichero temporal.
        for at in self.apps:
            if "db_conn" in at.session_state:
                at.session_state["db_conn"].close()

    def test_shows_login_when_not_signed_in(self):
        at = self._app().run()
        self.assertFalse(at.exception)
        self.assertFalse(at.error)
        self.assertNotIn("user_id", at.session_state)
        buttons = at.get("link_button")
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].proto.label, "Iniciar sesión con Spotify")
        self.assertIn("accounts.spotify.com/authorize", buttons[0].proto.url)

    def test_callback_signs_in_and_shows_home(self):
        at = self._app()
        at.query_params["code"] = "codigo"
        at.query_params["state"] = "estado"
        result = web_auth.LoginResult("pol", "Pol")
        with mock.patch.object(web_auth, "complete_login", return_value=result) as login, \
                mock.patch.object(spotify_core, "get_spotify_client", return_value=fake_spotify()):
            at.run()
        self.assertFalse(at.exception)
        login.assert_called_once()
        self.assertEqual(at.session_state["user_id"], "pol")
        self.assertIn("Hola, Pol", [t.value for t in at.title])

    def test_bad_callback_shows_error_and_login(self):
        at = self._app()
        at.query_params["code"] = "codigo"
        at.query_params["state"] = "falso"
        at.run()
        self.assertFalse(at.exception)
        self.assertTrue(any("no es válido" in e.value for e in at.error))
        self.assertNotIn("user_id", at.session_state)

    def test_every_page_renders(self):
        from streamlit.testing.v1 import AppTest

        def render(page_name):
            import os
            from unittest import mock
            from zoneinfo import ZoneInfo

            import autoplaylist
            import history

            import smart

            conn = history.connect(os.environ["DATABASE_URL"])
            import time

            now = int(time.time())
            # Escuchas de los últimos ~14 meses (estadísticas) + una favorita olvidada (redescubrimientos).
            rows = [(now - 86400 * 7 * i, f"spotify:track:{i % 5}", f"T{i % 5}", f"Art{i % 3}", "Al", 60000)
                    for i in range(60)]
            rows += [(now - 86400 * 400 - 600 * i, "spotify:track:old", "Vieja", "ArtV", "Al", 60000)
                     for i in range(6)]
            history.add_plays(conn, "pol", rows, "export")
            history.save_recipe(conn, "pol", "Receta", smart.Spec(days=30).to_json())
            ctx = autoplaylist.Ctx(sp=mock.Mock(), conn=conn, user_id="pol", display_name="Pol",
                                   tz=ZoneInfo("Europe/Madrid"))
            try:
                getattr(autoplaylist, page_name)(ctx)
            finally:
                conn.close()

        for page_name in ("page_home", "page_stats", "page_custom", "page_rediscover", "page_recipes",
                          "page_import", "page_spotify_top", "page_account"):
            at = AppTest.from_function(render, args=(page_name,), default_timeout=30).run()
            self.assertFalse(at.exception, f"{page_name}: {[e.value for e in at.exception]}")
            self.assertTrue(at.title, page_name)
            if page_name == "page_stats":
                self.assertEqual(len(at.get("vega_lite_chart")), 3, "deben pintarse los 3 gráficos")
                self.assertEqual(len(at.metric), 4)
            if page_name == "page_rediscover":
                self.assertIn("Vieja", at.dataframe[0].value["Canción"].tolist())

    def test_local_mode_uses_cache_file_client(self):
        with mock.patch.dict(os.environ, {"DATABASE_URL": "", "HISTORY_DB_PATH": os.environ["DATABASE_URL"]}), \
                mock.patch.object(spotify_core, "get_spotify_client", return_value=fake_spotify()):
            at = self._app().run()
        self.assertFalse(at.exception)
        self.assertIn("Hola, Pol", [t.value for t in at.title])


if __name__ == "__main__":
    unittest.main()
