"""Fase 1: capa de BD (SQLite/Postgres), tokens cifrados en la BD y cron multi-cuenta."""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import cron  # noqa: E402
import db  # noqa: E402
import jobs  # noqa: E402
import spotify_core  # noqa: E402
import tokens  # noqa: E402
from _db import fresh_db  # noqa: E402

SECRET_ENV = {"SPOTIPY_CLIENT_SECRET": "secreto-de-prueba", "TOKEN_ENCRYPTION_KEY": ""}
TOKEN = {"access_token": "a", "refresh_token": "r", "expires_at": 9999999999,
         "scope": spotify_core.SCOPE, "token_type": "Bearer"}


class DbLayerTests(unittest.TestCase):
    def test_postgres_url_detection(self):
        self.assertTrue(db.is_postgres_url("postgresql://u:p@h/d?sslmode=require"))
        self.assertTrue(db.is_postgres_url("postgres://u:p@h/d"))
        self.assertFalse(db.is_postgres_url("C:\\datos\\history.db"))

    def test_placeholders_translated_only_for_postgres(self):
        fake = db.Database(raw=None, dialect="postgres", target="postgresql://x")
        self.assertEqual(fake._sql("SELECT ? , ?"), "SELECT %s , %s")
        lite = db.Database(raw=None, dialect="sqlite", target=":memory:")
        self.assertEqual(lite._sql("SELECT ?"), "SELECT ?")

    def test_connect_uses_database_url_when_no_target(self):
        with mock.patch.dict(os.environ, {"DATABASE_URL": "postgresql://u@h/d"}), \
                mock.patch.object(db, "_connect_postgres") as pg, \
                mock.patch.object(db, "create_schema"):
            conn = db.connect()
        pg.assert_called_once_with("postgresql://u@h/d")
        self.assertTrue(conn.is_postgres)

    def test_history_always_enabled_with_database_url(self):
        with mock.patch.dict(os.environ, {"DATABASE_URL": "postgresql://u@h/d", "HISTORY_ENABLED": "false"}):
            self.assertTrue(config.history_enabled())
            self.assertTrue(config.use_db_tokens())
        with mock.patch.dict(os.environ, {"DATABASE_URL": ""}):
            self.assertFalse(config.use_db_tokens())

    def test_failed_statement_does_not_break_connection(self):
        conn = fresh_db()
        self.addCleanup(conn.close)
        with self.assertRaises(Exception):
            conn.execute("SELECT * FROM tabla_que_no_existe")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0], 0)


@mock.patch.dict(os.environ, SECRET_ENV)
class TokenTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.addCleanup(self.conn.close)

    def test_roundtrip_is_encrypted(self):
        tokens.save_token(self.conn, "me", TOKEN)
        stored = self.conn.execute("SELECT token FROM spotify_tokens").fetchone()[0]
        self.assertNotIn("refresh_token", stored)  # nunca en claro
        self.assertEqual(tokens.load_token(self.conn, "me"), TOKEN)
        self.assertEqual(tokens.list_accounts(self.conn), ["me"])

    def test_other_key_cannot_read(self):
        tokens.save_token(self.conn, "me", TOKEN)
        with mock.patch.dict(os.environ, {"SPOTIPY_CLIENT_SECRET": "otro"}):
            self.assertIsNone(tokens.load_token(self.conn, "me"))

    def test_explicit_key_and_invalid_key(self):
        from cryptography.fernet import Fernet

        with mock.patch.dict(os.environ, {"TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
            tokens.save_token(self.conn, "me", TOKEN)
            self.assertEqual(tokens.load_token(self.conn, "me"), TOKEN)
        with mock.patch.dict(os.environ, {"TOKEN_ENCRYPTION_KEY": "no-vale"}):
            with self.assertRaises(config.ConfigError):
                tokens.save_token(self.conn, "me", TOKEN)

    def test_cache_handler_saves_refreshed_token(self):
        handler = tokens.DbCacheHandler(self.conn, "me")
        self.assertIsNone(handler.get_cached_token())
        handler.save_token_to_cache(TOKEN)
        handler.save_token_to_cache(dict(TOKEN, access_token="nuevo"))
        self.assertEqual(handler.get_cached_token()["access_token"], "nuevo")
        self.assertEqual(tokens.list_accounts(self.conn), ["me"])

    def test_bootstrap_from_env_once(self):
        sp = mock.Mock()
        sp.current_user.return_value = {"id": "pol"}
        with mock.patch.dict(os.environ, {"SPOTIFY_CACHE": json.dumps(TOKEN)}), \
                mock.patch.object(spotify_core, "get_spotify_client", return_value=sp):
            self.assertEqual(tokens.bootstrap_from_env(self.conn), "pol")
            # Con cuentas ya guardadas no vuelve a importar (no pisa un token renovado).
            self.assertIsNone(tokens.bootstrap_from_env(self.conn))
        self.assertEqual(tokens.load_token(self.conn, "pol"), TOKEN)

    def test_bootstrap_rejects_bad_json(self):
        with mock.patch.dict(os.environ, {"SPOTIFY_CACHE": "{no es json"}):
            with self.assertRaises(config.ConfigError):
                tokens.bootstrap_from_env(self.conn)


@mock.patch.dict(os.environ, SECRET_ENV)
class CloudCronTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.addCleanup(self.conn.close)
        self.conn.close = mock.Mock()  # cron.run la cierra; la necesitamos para comprobar

    def _run(self, record_only=True, clients=None):
        def fake_client(cache_handler=None):
            result = clients[cache_handler.user_id]
            if isinstance(result, Exception):
                raise result
            return result

        with mock.patch.object(cron, "use_db_tokens", return_value=True), \
                mock.patch.object(cron.history, "connect", return_value=self.conn), \
                mock.patch.object(cron, "get_spotify_client", side_effect=fake_client), \
                mock.patch.object(cron.jobs, "record_recent_plays", return_value=(1, 0)) as rec:
            code = cron.run(record_only=record_only)
        return code, rec

    def test_processes_every_account_and_survives_one_failure(self):
        tokens.save_token(self.conn, "ana", TOKEN)
        tokens.save_token(self.conn, "pol", TOKEN)
        clients = {"ana": spotify_core.AuthRequiredError("token caducado"), "pol": mock.Mock()}
        code, rec = self._run(clients=clients)
        self.assertEqual(code, 1)  # hubo un error...
        self.assertEqual([c.args[2] for c in rec.call_args_list], ["pol"])  # ...pero pol se registró

    def test_all_ok_returns_zero(self):
        tokens.save_token(self.conn, "pol", TOKEN)
        code, rec = self._run(clients={"pol": mock.Mock()})
        self.assertEqual(code, 0)
        rec.assert_called_once()

    def test_no_accounts_yet_is_not_an_error(self):
        # Hasta el primer login en la web no hay cuentas: el cron horario no debe fallar.
        with mock.patch.dict(os.environ, {"SPOTIFY_CACHE": ""}):
            code, rec = self._run(clients={})
        self.assertEqual(code, 0)
        rec.assert_not_called()

    def test_full_mode_updates_playlists_per_account(self):
        tokens.save_token(self.conn, "pol", TOKEN)
        outcome = jobs.JobOutcome("n", spotify_core.PlaylistResult("id", False, 3, "u"))
        with mock.patch.object(cron.jobs, "update_auto_playlists", return_value=[outcome]) as upd:
            code, _ = self._run(record_only=False, clients={"pol": mock.Mock()})
        self.assertEqual(code, 0)
        self.assertEqual(upd.call_args.args[2], "pol")


if __name__ == "__main__":
    unittest.main()
