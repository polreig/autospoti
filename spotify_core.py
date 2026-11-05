"""Capa de acceso a Spotify, sin dependencias de Streamlit (la usan la app web y cron.py)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

import spotipy
from spotipy.cache_handler import CacheFileHandler
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth, SpotifyOauthError

from config import CACHE_PATH, required_env

DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"
# Un único SCOPE para app, cron y auth.py: si difiere del guardado en .cache,
# spotipy intenta reautorizar de forma interactiva y se cuelga en un cron.
SCOPE = (
    "playlist-modify-public playlist-modify-private playlist-read-private "
    "user-top-read user-read-recently-played"
)
PLAYLIST_BATCH = 100          # máximo de items por petición de playlist
MAX_PLAYLIST_TRACKS = 10_000  # límite de Spotify por playlist
MAX_DESCRIPTION = 300         # límite de Spotify para la descripción


class AuthRequiredError(RuntimeError):
    """No hay un token válido: hay que ejecutar ``python auth.py``."""


def _restore_cache_from_env() -> None:
    """Despliegues sin disco persistente: crea .cache desde SPOTIFY_CACHE solo si no existe.

    Antes se sobrescribía en cada ejecución, pisando un token ya refrescado con uno caducado.
    """
    cache_data = os.environ.get("SPOTIFY_CACHE", "").strip()
    if cache_data and not CACHE_PATH.exists():
        CACHE_PATH.write_text(cache_data, encoding="utf-8")


def build_auth_manager(open_browser: bool = False) -> SpotifyOAuth:
    _restore_cache_from_env()
    return SpotifyOAuth(
        client_id=required_env("SPOTIPY_CLIENT_ID"),
        client_secret=required_env("SPOTIPY_CLIENT_SECRET"),
        redirect_uri=os.environ.get("SPOTIPY_REDIRECT_URI", "").strip() or DEFAULT_REDIRECT_URI,
        scope=SCOPE,
        cache_handler=CacheFileHandler(cache_path=str(CACHE_PATH)),
        open_browser=open_browser,
    )


def get_spotify_client() -> spotipy.Spotify:
    """Cliente no interactivo. Lanza AuthRequiredError si falta autorización."""
    auth_manager = build_auth_manager(open_browser=False)
    token_info = auth_manager.cache_handler.get_cached_token()
    if not token_info:
        raise AuthRequiredError(
            "No hay sesión de Spotify guardada. Ejecuta una vez: python auth.py"
        )
    try:
        valid = auth_manager.validate_token(token_info)
    except SpotifyOauthError as exc:
        raise AuthRequiredError(
            f"No se pudo renovar el token de Spotify ({exc}). Ejecuta de nuevo: python auth.py"
        ) from exc
    if not valid:
        raise AuthRequiredError(
            "El token guardado no es válido o sus permisos han cambiado. "
            "Ejecuta de nuevo: python auth.py"
        )
    return spotipy.Spotify(auth_manager=auth_manager, requests_timeout=20, retries=3)


def get_clean_track_uris(tracks_items: Iterable[dict]) -> list[str]:
    """URIs de canciones sin duplicados, respetando el orden. Ignora locales y episodios."""
    seen_ids: set[str] = set()
    track_uris: list[str] = []
    for item in tracks_items:
        track = item["track"] if item.get("track") else item
        track_id = track.get("id")
        uri = track.get("uri")
        if track_id and uri and uri.startswith("spotify:track:") and track_id not in seen_ids:
            seen_ids.add(track_id)
            track_uris.append(uri)
    return track_uris


def get_top_tracks(sp: spotipy.Spotify, time_range: str, total_limit: int) -> list[dict]:
    """Top de canciones paginado. ``time_range``: short_term / medium_term / long_term."""
    items: list[dict] = []
    offset = 0
    while len(items) < total_limit:
        batch = min(50, total_limit - len(items))
        page = sp.current_user_top_tracks(time_range=time_range, limit=batch, offset=offset)
        page_items = page.get("items", [])
        if not page_items:
            break
        items.extend(page_items)
        offset += len(page_items)
        if len(page_items) < batch:
            break
    return items


def get_recently_played(sp: spotipy.Spotify) -> list[dict]:
    """Últimas reproducciones (la API devuelve como máximo 50)."""
    return sp.current_user_recently_played(limit=50).get("items", [])


@dataclass
class PlaylistResult:
    playlist_id: "str | None"
    created: bool
    track_count: int
    url: "str | None" = None


class PlaylistIndex:
    """Playlists propias del usuario, descargadas UNA vez (perezosamente) y reutilizadas.

    Antes cada playlist escrita volvía a paginar todas tus playlists; con 5 automáticas
    eran 5 recorridos completos. Comparte un índice entre escrituras de la misma ejecución.
    """

    def __init__(self, sp: spotipy.Spotify, user_id: str):
        self._sp = sp
        self._user_id = user_id
        self._names: "dict[str, str] | None" = None  # id -> nombre

    def _load(self) -> dict[str, str]:
        if self._names is None:
            names: dict[str, str] = {}
            page = self._sp.current_user_playlists(limit=50)
            while page:
                for pl in page.get("items", []):
                    if pl and (pl.get("owner") or {}).get("id") == self._user_id:
                        names[pl["id"]] = pl.get("name") or ""
                page = self._sp.next(page) if page.get("next") else None
            self._names = names
        return self._names

    def owns(self, playlist_id: str) -> bool:
        """True si la playlist sigue en tu biblioteca y es tuya (no borrada/dejada de seguir)."""
        return playlist_id in self._load()

    def find_by_name(self, name: str) -> "str | None":
        for playlist_id, pl_name in self._load().items():
            if pl_name == name:
                return playlist_id
        return None

    def remember(self, playlist_id: str, name: str) -> None:
        self._load()[playlist_id] = name


def _write_items(sp: spotipy.Spotify, playlist_id: str, uris: list[str]) -> None:
    """Sustituye el contenido: primer lote con replace, el resto se añade por lotes de 100."""
    sp.playlist_replace_items(playlist_id, uris[:PLAYLIST_BATCH])
    for start in range(PLAYLIST_BATCH, len(uris), PLAYLIST_BATCH):
        sp.playlist_add_items(playlist_id, uris[start:start + PLAYLIST_BATCH])


def create_or_update_playlist(
    sp: spotipy.Spotify,
    user_id: str,
    name: str,
    track_uris: list[str],
    description: str = "",
    public: bool = False,
    *,
    index: "PlaylistIndex | None" = None,
    playlist_id: "str | None" = None,
) -> PlaylistResult:
    """Crea o actualiza una playlist propia. Con lista vacía NO toca nada (evita vaciarla).

    ``playlist_id``: ID recordado de una ejecución anterior; se usa si la playlist sigue
    siendo tuya aunque le hayas cambiado el nombre. Si no, se busca por nombre exacto.
    ``index``: índice compartido para no volver a descargar tus playlists en cada llamada.
    """
    if not track_uris:
        return PlaylistResult(playlist_id=None, created=False, track_count=0)

    uris = list(track_uris[:MAX_PLAYLIST_TRACKS])
    description = description[:MAX_DESCRIPTION]
    index = index or PlaylistIndex(sp, user_id)
    if not (playlist_id and index.owns(playlist_id)):
        playlist_id = index.find_by_name(name)
    created = playlist_id is None
    if created:
        playlist = sp.current_user_playlist_create(name=name, public=public, description=description)
        playlist_id = playlist["id"]
        index.remember(playlist_id, name)
    else:
        sp.playlist_change_details(playlist_id, description=description)
    _write_items(sp, playlist_id, uris)
    return PlaylistResult(
        playlist_id=playlist_id,
        created=created,
        track_count=len(uris),
        url=f"https://open.spotify.com/playlist/{playlist_id}",
    )


def friendly_error(exc: Exception) -> str:
    """Mensaje claro para errores habituales de Spotify (sin ocultar la causa real)."""
    if isinstance(exc, SpotifyException):
        if exc.http_status == 403:
            return (
                "Spotify respondió 403 (prohibido). Con una app en Development Mode, el dueño de la "
                "app necesita Spotify Premium y tu usuario debe estar añadido en el dashboard."
            )
        if exc.http_status == 429:
            return "Spotify indica demasiadas peticiones (429). Espera unos minutos y reintenta."
        return f"Error de Spotify ({exc.http_status}): {exc.msg}"
    return f"{type(exc).__name__}: {exc}"
