"""Comprobación rápida: todos los módulos importan y autoplaylist.py compila.

Uso:  python tests/smoke_check.py
"""
import ast
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import auth, config, cron, dateranges, history, history_import, import_history, jobs, spotify_core  # noqa: E402,F401
import spotipy  # noqa: E402
from spotipy.oauth2 import SpotifyOAuth  # noqa: E402

lines = ["IMPORTS OK"]
lines.append(f"validate_token: {hasattr(SpotifyOAuth, 'validate_token')}")
lines.append(f"playlist_change_details: {hasattr(spotipy.Spotify, 'playlist_change_details')}")
lines.append(f"Spotify.__init__: {inspect.signature(spotipy.Spotify.__init__)}")
ast.parse((ROOT / "autoplaylist.py").read_text(encoding="utf-8"))
lines.append("autoplaylist.py sintaxis OK")
(ROOT / "smoke_result.txt").write_text("\n".join(lines), encoding="utf-8")
