"""Fase 4: dos cuentas, enlace de pareja con consentimiento y mezclas (blend)."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import blend  # noqa: E402
import history  # noqa: E402
import smart  # noqa: E402
import tokens  # noqa: E402
from _db import fresh_db  # noqa: E402
from history import TopTrack  # noqa: E402

MADRID = ZoneInfo("Europe/Madrid")


def t(uri, plays):
    return TopTrack(f"spotify:track:{uri}", uri.upper(), "Art", plays)


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.db = fresh_db()
        self.addCleanup(self.db.close)

    def test_invite_then_accept(self):
        self.assertEqual(blend.invite(self.db, "pol", "ana"), "pending")
        self.assertFalse(blend.is_linked(self.db, "pol", "ana"))
        self.assertEqual(blend.incoming(self.db, "ana"), ["pol"])
        self.assertEqual(blend.outgoing(self.db, "pol"), ["ana"])
        self.assertTrue(blend.accept(self.db, "ana", "pol"))
        self.assertTrue(blend.is_linked(self.db, "ana", "pol"))
        self.assertEqual(blend.partners(self.db, "pol"), ["ana"])
        self.assertEqual(blend.partners(self.db, "ana"), ["pol"])
        self.assertEqual(blend.incoming(self.db, "ana"), [])

    def test_only_the_invited_person_can_accept(self):
        blend.invite(self.db, "pol", "ana")
        self.assertFalse(blend.accept(self.db, "pol", "ana"))   # el que invita no se autoacepta
        self.assertFalse(blend.accept(self.db, "intruso", "pol"))
        self.assertFalse(blend.is_linked(self.db, "pol", "ana"))

    def test_crossed_invitations_link_directly(self):
        blend.invite(self.db, "ana", "pol")
        self.assertEqual(blend.invite(self.db, "pol", "ana"), "accepted")
        self.assertTrue(blend.is_linked(self.db, "pol", "ana"))

    def test_remove_from_either_side(self):
        blend.invite(self.db, "pol", "ana")
        blend.accept(self.db, "ana", "pol")
        blend.remove(self.db, "ana", "pol")
        self.assertFalse(blend.is_linked(self.db, "pol", "ana"))
        self.assertEqual(blend.partners(self.db, "pol"), [])

    def test_cannot_invite_yourself_or_nobody(self):
        for bad in ("pol", "", "   "):
            with self.assertRaises(ValueError):
                blend.invite(self.db, "pol", bad)

    def test_display_names(self):
        tokens.save_profile(self.db, "ana", "Ana")
        self.assertEqual(tokens.display_names(self.db, ["ana", "pol"]), {"ana": "Ana", "pol": "pol"})


class CombineTests(unittest.TestCase):
    def test_common_prefers_songs_both_love(self):
        mine = [t("a", 10), t("b", 6), t("c", 3)]
        theirs = [t("b", 5), t("a", 1), t("d", 9)]
        out = blend.combine(mine, theirs, "comunes", 10)
        self.assertEqual([(x.uri[-1], x.plays) for x in out], [("b", 11), ("a", 11)])

    def test_half_and_half_alternates_without_duplicates(self):
        mine = [t("a", 9), t("b", 8), t("c", 7)]
        theirs = [t("b", 9), t("d", 8)]
        out = blend.combine(mine, theirs, "mitad", 4)
        self.assertEqual([x.uri[-1] for x in out], ["a", "b", "d", "c"])

    def test_unknown_mode(self):
        with self.assertRaises(ValueError):
            blend.combine([], [], "raro", 5)


class BlendRecipeTests(unittest.TestCase):
    def setUp(self):
        self.db = fresh_db()
        self.addCleanup(self.db.close)
        now = datetime.now(timezone.utc)
        base = int((now - timedelta(days=3)).timestamp())
        history.add_plays(self.db, "pol", [(base + i, "spotify:track:a", "A", "X", "Al", 60000) for i in range(3)]
                          + [(base + 10, "spotify:track:solo_pol", "P", "X", "Al", 60000)], "export")
        history.add_plays(self.db, "ana", [(base + 100 + i, "spotify:track:a", "A", "X", "Al", 60000)
                                           for i in range(2)]
                          + [(base + 200, "spotify:track:solo_ana", "Q", "Y", "Al", 60000)], "export")
        self.spec = smart.Spec(kind="blend", partner="ana", days=30, blend_mode="comunes")

    def test_no_link_no_data(self):
        self.assertEqual(smart.build_tracks(self.db, "pol", self.spec, MADRID), [])

    def test_linked_common_and_half(self):
        blend.invite(self.db, "pol", "ana")
        blend.accept(self.db, "ana", "pol")
        common = smart.build_tracks(self.db, "pol", self.spec, MADRID)
        self.assertEqual([(x.uri, x.plays) for x in common], [("spotify:track:a", 5)])
        half = smart.build_tracks(self.db, "pol", smart.Spec(kind="blend", partner="ana", days=30,
                                                             blend_mode="mitad"), MADRID)
        self.assertEqual({x.uri for x in half},
                         {"spotify:track:a", "spotify:track:solo_pol", "spotify:track:solo_ana"})

    def test_unlinking_stops_the_recipe(self):
        blend.invite(self.db, "pol", "ana")
        blend.accept(self.db, "ana", "pol")
        blend.remove(self.db, "ana", "pol")
        self.assertEqual(smart.build_tracks(self.db, "pol", self.spec, MADRID), [])

    def test_spec_validation_and_description(self):
        with self.assertRaises(ValueError):
            smart.Spec(kind="blend").validate()
        with self.assertRaises(ValueError):
            smart.Spec(kind="blend", partner="ana", blend_mode="raro").validate()
        self.assertIn("Mezcla con ana (en común)", self.spec.describe())


class ProfileOnLoginTests(unittest.TestCase):
    @mock.patch.dict(os.environ, {"SPOTIPY_CLIENT_ID": "c", "SPOTIPY_CLIENT_SECRET": "s",
                                  "TOKEN_ENCRYPTION_KEY": "", "ALLOWED_SPOTIFY_IDS": ""})
    def test_login_saves_display_name(self):
        import web_auth

        db = fresh_db()
        self.addCleanup(db.close)
        token = {"access_token": "a", "refresh_token": "r", "expires_at": 9999999999}

        def factory(cache):
            oauth = mock.Mock()
            oauth.get_access_token.side_effect = lambda **kw: cache.save_token_to_cache(token)
            return oauth

        sp = mock.Mock()
        sp.current_user.return_value = {"id": "ana", "display_name": "Ana"}
        with mock.patch.object(web_auth, "_oauth", side_effect=factory), \
                mock.patch.object(web_auth.spotipy, "Spotify", return_value=sp):
            web_auth.complete_login(db, "code", web_auth.make_state())
        self.assertEqual(tokens.display_names(db, ["ana"]), {"ana": "Ana"})


if __name__ == "__main__":
    unittest.main()
