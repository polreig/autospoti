"""Géneros musicales de los artistas de tu historial (para filtrar playlists por género).

El género es del ARTISTA, no de cada escucha: se busca una vez por artista y sirve para todo
el historial (también el importado). Se guarda en la tabla ``artists``, compartida entre
cuentas porque es información pública del artista (no es historial de nadie).

De dónde salen:
  1. Spotify: una canción del artista (``GET /tracks/{id}``) da su ID, y el artista
     (``GET /artists/{id}``) sus géneros. Desde febrero de 2026 no hay consultas por lotes,
     así que son 2 peticiones por artista: se va poco a poco y se para ante un 429.
  2. Last.fm (opcional, ``LASTFM_API_KEY``): si Spotify no tiene géneros para el artista,
     sus etiquetas más votadas (``artist.getTopTags``).

``tag_artists`` procesa una tanda (primero los artistas que más escuchas); el cron la llama
cada hora, así que todo el historial se completa solo en uno o dos días.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

import spotipy
from spotipy.exceptions import SpotifyException

from db import Database

SEPARATOR = "|"
LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
LASTFM_MIN_COUNT = 10      # Last.fm puntúa cada etiqueta de 0 a 100; menos de esto es ruido
LASTFM_MAX_TAGS = 6
PAUSE_SECONDS = 0.3        # entre peticiones a Spotify (evita el límite de peticiones)

# Estados de ``artists.source``
SOURCE_SPOTIFY = "spotify"
SOURCE_LASTFM = "lastfm"
SOURCE_NONE_SPOTIFY = "none-spotify"   # Spotify sin géneros y Last.fm aún no consultado
SOURCE_NONE = "none"                   # nadie tiene géneros para este artista

# Etiquetas de Last.fm que no son géneros.
_NOT_GENRES = {
    "seen live", "favorites", "favourites", "favorite", "favourite", "my favorites", "love",
    "awesome", "beautiful", "albums i own", "amazing", "cool", "good", "great", "best",
    "under 2000 listeners", "spotify", "all", "music", "male vocalists", "female vocalists",
    "male vocalist", "female vocalist", "singer songwriter", "favorite artists", "sexy",
}


class RateLimited(RuntimeError):
    """Spotify pidió esperar (429): se deja para la siguiente ejecución."""


class LastfmError(RuntimeError):
    """Last.fm no responde, la clave no vale o pide esperar: se reintenta más tarde
    (nunca se marca un artista como «sin género» por un fallo de Last.fm)."""


LASTFM_NOT_FOUND = 6   # código de error de Last.fm: el artista no existe (no es un fallo)


def normalize(text: str) -> str:
    """«Hip-Hop» -> «hip hop». Así coinciden Spotify, Last.fm y lo que se escribe a mano."""
    return " ".join((text or "").casefold().replace("-", " ").replace("_", " ").split())


def artist_key(name: str) -> str:
    return " ".join((name or "").casefold().split())


def matches(artist_genres: "list[str] | None", terms: list[str]) -> bool:
    """True si algún género del artista contiene alguno de los términos como palabras.

    «rock» encaja con «indie rock» y «rock en español», pero no con «rockabilly».
    """
    if not artist_genres:
        return False
    padded = [f" {g} " for g in artist_genres]
    return any(f" {normalize(t)} " in g for t in terms if normalize(t) for g in padded)


# ---------------------------------------------------------------- base de datos

def genres_for(conn: Database, names) -> dict[str, list[str]]:
    """{nombre tal cual -> géneros} de los artistas pedidos que ya estén clasificados."""
    by_key = {}
    for name in set(n for n in names if n):
        by_key.setdefault(artist_key(name), []).append(name)
    if not by_key:
        return {}
    found: dict[str, list[str]] = {}
    keys = list(by_key)
    for i in range(0, len(keys), 500):  # IN (...) por bloques
        chunk = keys[i:i + 500]
        marks = ", ".join("?" for _ in chunk)
        cur = conn.execute(f"SELECT artist_key, genres FROM artists WHERE artist_key IN ({marks})", chunk)
        for key, raw in cur.fetchall():
            values = [g for g in (raw or "").split(SEPARATOR) if g]
            for name in by_key[key]:
                found[name] = values
    return found


def _save(conn: Database, key: str, name: str, spotify_id: "str | None", values: list[str],
          source: str) -> None:
    conn.execute(
        "INSERT INTO artists (artist_key, name, spotify_id, genres, source, checked_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (artist_key) DO UPDATE SET name = excluded.name, "
        "spotify_id = COALESCE(excluded.spotify_id, artists.spotify_id), genres = excluded.genres, "
        "source = excluded.source, checked_at = excluded.checked_at",
        (key, name, spotify_id, SEPARATOR.join(values), source, int(datetime.now(timezone.utc).timestamp())),
    )
    conn.commit()


@dataclass
class Pending:
    key: str
    name: str
    track_uri: str
    retry_lastfm_only: bool = False   # Spotify ya se consultó: solo falta Last.fm


def pending_artists(conn: Database, user_id: str, limit: int, with_lastfm: bool) -> list[Pending]:
    """Artistas de tu historial aún sin clasificar, de los que más escuchas a los que menos."""
    cur = conn.execute(
        "SELECT artist, MIN(track_uri), COUNT(*) FROM plays WHERE user_id = ? AND artist IS NOT NULL "
        "GROUP BY artist ORDER BY COUNT(*) DESC",
        (user_id,),
    )
    rows = cur.fetchall()
    done = {key: source for key, source in conn.execute("SELECT artist_key, source FROM artists").fetchall()}
    out: list[Pending] = []
    seen: set[str] = set()
    for name, uri, _count in rows:
        key = artist_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        source = done.get(key)
        if source is None:
            out.append(Pending(key, name, uri))
        elif source == SOURCE_NONE_SPOTIFY and with_lastfm:
            out.append(Pending(key, name, uri, retry_lastfm_only=True))
        if len(out) >= limit:
            break
    return out


@dataclass
class Coverage:
    artists: int          # artistas distintos en tu historial
    tagged: int           # ya consultados
    with_genre: int       # con algún género
    plays: int            # escuchas totales
    plays_with_genre: int # escuchas de artistas con género

    @property
    def pending(self) -> int:
        return self.artists - self.tagged

    @property
    def percent_plays(self) -> int:
        return round(100 * self.plays_with_genre / self.plays) if self.plays else 0


def coverage(conn: Database, user_id: str) -> Coverage:
    rows = conn.execute(
        "SELECT artist, COUNT(*) FROM plays WHERE user_id = ? AND artist IS NOT NULL GROUP BY artist",
        (user_id,),
    ).fetchall()
    counts: dict[str, int] = {}
    names: dict[str, str] = {}
    for name, n in rows:
        key = artist_key(name)
        counts[key] = counts.get(key, 0) + n
        names.setdefault(key, name)
    known = genres_for(conn, names.values())
    known_keys = {artist_key(n): g for n, g in known.items()}
    return Coverage(
        artists=len(counts),
        tagged=len(known_keys),
        with_genre=sum(1 for g in known_keys.values() if g),
        plays=sum(counts.values()),
        plays_with_genre=sum(n for k, n in counts.items() if known_keys.get(k)),
    )


def top_genres(conn: Database, user_id: str, start_ts: int = 0, end_ts: "int | None" = None,
               limit: int = 30) -> list[tuple[str, int]]:
    """Tus géneros más escuchados (escuchas de sus artistas) en [start_ts, end_ts)."""
    end_ts = end_ts if end_ts is not None else int(datetime.now(timezone.utc).timestamp()) + 1
    rows = conn.execute(
        "SELECT artist, COUNT(*) FROM plays WHERE user_id = ? AND artist IS NOT NULL AND ts >= ? AND ts < ? "
        "GROUP BY artist",
        (user_id, start_ts, end_ts),
    ).fetchall()
    known = genres_for(conn, [r[0] for r in rows])
    totals: dict[str, int] = {}
    for name, n in rows:
        for genre in known.get(name, []):
            totals[genre] = totals.get(genre, 0) + n
    return sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


# ---------------------------------------------------------------- fuentes externas

def _careful_client(sp):
    """Cliente de Spotify sin reintentos: ante un 429 se para enseguida en vez de quedarse
    esperando (Spotify puede pedir horas de espera y el cron se colgaría)."""
    if isinstance(sp, spotipy.Spotify) and getattr(sp, "auth_manager", None) is not None:
        return spotipy.Spotify(auth_manager=sp.auth_manager, requests_timeout=10, retries=0,
                               status_retries=0)
    return sp


def spotify_genres(sp, track_uri: str, artist_name: str) -> "tuple[str | None, list[str]]":
    """(ID del artista, géneros) a partir de una de sus canciones. Lanza RateLimited."""
    track_id = track_uri.rsplit(":", 1)[-1]
    try:
        track = sp.track(track_id)
        artists = (track or {}).get("artists") or []
        wanted = artist_key(artist_name)
        chosen = next((a for a in artists if artist_key(a.get("name", "")) == wanted), None)
        chosen = chosen or (artists[0] if artists else None)
        if not chosen or not chosen.get("id"):
            return None, []
        time.sleep(PAUSE_SECONDS)
        artist = sp.artist(chosen["id"])
    except SpotifyException as exc:
        if exc.http_status == 429 or "max retries" in str(exc).lower():
            raise RateLimited(str(exc)) from exc
        if exc.http_status in (400, 404):
            return None, []  # canción retirada o ID raro: se prueba con Last.fm
        raise
    values = []
    for g in (artist or {}).get("genres") or []:
        if normalize(g) and normalize(g) not in values:
            values.append(normalize(g))
    return chosen["id"], values


def lastfm_genres(artist_name: str, api_key: str, opener=urllib.request.urlopen) -> list[str]:
    """Etiquetas más votadas del artista en Last.fm que parecen géneros.

    Lanza LastfmError si el fallo es de Last.fm (red, clave, límite); [] si el artista no
    existe o no tiene etiquetas que sean géneros.
    """
    query = urllib.parse.urlencode({"method": "artist.gettoptags", "artist": artist_name,
                                    "api_key": api_key, "format": "json", "autocorrect": 1})
    try:
        with opener(f"{LASTFM_URL}?{query}", timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # Last.fm manda los errores con un JSON
        try:
            data = json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            raise LastfmError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise LastfmError(str(exc)) from exc
    if isinstance(data, dict) and data.get("error"):
        if data.get("error") == LASTFM_NOT_FOUND:
            return []
        raise LastfmError(f"error {data.get('error')}: {data.get('message', '')}")
    tags = ((data or {}).get("toptags") or {}).get("tag") or []
    if isinstance(tags, dict):
        tags = [tags]
    own = normalize(artist_name)
    values: list[str] = []
    for tag in tags:
        name = normalize(tag.get("name", ""))
        try:
            count = int(tag.get("count", 0))
        except (TypeError, ValueError):
            count = 0
        if (count < LASTFM_MIN_COUNT or not name or name in _NOT_GENRES or name == own
                or name in values or len(name) > 40):
            continue
        values.append(name)
        if len(values) >= LASTFM_MAX_TAGS:
            break
    return values


def get_lastfm_key() -> str:
    return os.environ.get("LASTFM_API_KEY", "").strip()


# ---------------------------------------------------------------- tanda

@dataclass
class TagResult:
    processed: int = 0
    with_genre: int = 0
    stopped: str = ""       # motivo si se cortó antes (límite de Spotify, tiempo...)


def tag_artists(sp, conn: Database, user_id: str, max_artists: int = 100,
                max_seconds: float = 60.0, lastfm_key: "str | None" = None) -> TagResult:
    """Clasifica una tanda de artistas pendientes. Nunca lanza por un artista concreto."""
    lastfm_key = get_lastfm_key() if lastfm_key is None else lastfm_key
    client = _careful_client(sp)
    result = TagResult()
    deadline = time.monotonic() + max_seconds
    for item in pending_artists(conn, user_id, max_artists, with_lastfm=bool(lastfm_key)):
        if time.monotonic() > deadline:
            result.stopped = "tiempo de la tanda agotado"
            break
        spotify_id, values, source = None, [], SOURCE_NONE_SPOTIFY
        if not item.retry_lastfm_only:
            try:
                spotify_id, values = spotify_genres(client, item.track_uri, item.name)
            except RateLimited:
                result.stopped = "Spotify pidió esperar (límite de peticiones); se sigue en la próxima ejecución"
                break
            except Exception as exc:  # noqa: BLE001 - un artista raro no para la tanda
                result.stopped = f"error de Spotify: {exc}"
                break
            source = SOURCE_SPOTIFY if values else SOURCE_NONE_SPOTIFY
            time.sleep(PAUSE_SECONDS)
        if not values and lastfm_key:
            try:
                values = lastfm_genres(item.name, lastfm_key)
                source = SOURCE_LASTFM if values else SOURCE_NONE
            except LastfmError as exc:
                # Se guarda lo de Spotify (none-spotify): Last.fm se reintenta en otra tanda.
                if not item.retry_lastfm_only:
                    _save(conn, item.key, item.name, spotify_id, [], SOURCE_NONE_SPOTIFY)
                    result.processed += 1
                result.stopped = f"Last.fm no respondió bien ({exc}); se reintenta más tarde"
                break
        _save(conn, item.key, item.name, spotify_id, values, source)
        result.processed += 1
        result.with_genre += 1 if values else 0
    return result
