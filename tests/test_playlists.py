import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import spotify_core  # noqa: E402


class FakeSpotify:
    """Simula las páginas de current_user_playlists y registra las llamadas de escritura."""

    def __init__(self, pages):
        self._pages = pages
        self.calls = []

    def _page(self, i):
        has_next = i + 1 < len(self._pages)
        return {"items": self._pages[i], "next": "url" if has_next else None, "_i": i}

    def current_user_playlists(self, limit=50):
        return self._page(0)

    def next(self, page):
        return self._page(page["_i"] + 1)

    def current_user_playlist_create(self, name, public=True, collaborative=False, description=""):
        self.calls.append(("create", name, public, description))
        return {"id": "NEW"}

    def playlist_change_details(self, pid, **kw):
        self.calls.append(("details", pid, kw))

    def playlist_replace_items(self, pid, uris):
        self.calls.append(("replace", pid, list(uris)))


class PlaylistTests(unittest.TestCase):
    def test_clean_uris_dedup_and_filter(self):
        items = [
            {"track": {"id": "1", "uri": "spotify:track:1"}},
            {"track": {"id": "1", "uri": "spotify:track:1"}},
            {"id": "2", "uri": "spotify:track:2"},
            {"track": {"id": None, "uri": "spotify:local:x"}},
            {"track": {"id": "3", "uri": "spotify:episode:3"}},
        ]
        self.assertEqual(spotify_core.get_clean_track_uris(items), ["spotify:track:1", "spotify:track:2"])

    def test_finds_playlist_on_second_page_and_updates(self):
        pages = [
            [{"id": f"p{i}", "name": f"n{i}", "owner": {"id": "me"}} for i in range(50)],
            [{"id": "target", "name": "Mi lista", "owner": {"id": "me"}}],
        ]
        sp = FakeSpotify(pages)
        res = spotify_core.create_or_update_playlist(sp, "me", "Mi lista", ["spotify:track:1"], "desc")
        self.assertFalse(res.created)
        self.assertEqual(res.playlist_id, "target")
        self.assertIn(("replace", "target", ["spotify:track:1"]), sp.calls)
        self.assertEqual(sp.calls[0][0], "details")

    def test_ignores_same_name_owned_by_others(self):
        sp = FakeSpotify([[{"id": "x", "name": "Lista", "owner": {"id": "otro"}}]])
        res = spotify_core.create_or_update_playlist(sp, "me", "Lista", ["spotify:track:1"], "d")
        self.assertTrue(res.created)
        self.assertEqual(res.playlist_id, "NEW")

    def test_empty_list_does_not_touch_playlist(self):
        sp = FakeSpotify([[{"id": "x", "name": "Lista", "owner": {"id": "me"}}]])
        res = spotify_core.create_or_update_playlist(sp, "me", "Lista", [], "d")
        self.assertIsNone(res.playlist_id)
        self.assertEqual(sp.calls, [])

    def test_description_truncated_and_tracks_capped(self):
        sp = FakeSpotify([[]])
        uris = [f"spotify:track:{i}" for i in range(150)]
        spotify_core.create_or_update_playlist(sp, "me", "L", uris, "x" * 500)
        create = [c for c in sp.calls if c[0] == "create"][0]
        replace = [c for c in sp.calls if c[0] == "replace"][0]
        self.assertEqual(len(create[3]), 300)
        self.assertEqual(len(replace[2]), 100)


class SecretsTests(unittest.TestCase):
    def test_no_hardcoded_secrets_in_source(self):
        # Un Client ID/Secret de Spotify son 32 caracteres hexadecimales.
        pattern = re.compile(r"\b[0-9a-f]{32}\b", re.IGNORECASE)
        root = Path(__file__).resolve().parent.parent
        for path in list(root.glob("*.py")) + list(root.glob("tests/*.py")) + list(root.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(pattern.search(text), f"Posible secreto en {path.name}")


if __name__ == "__main__":
    unittest.main()
