"""Tokens de Spotify guardados en la base de datos (modo nube, con ``DATABASE_URL``).

* Un token por usuario de Spotify, cifrado con Fernet (AES + HMAC).
* Clave: ``TOKEN_ENCRYPTION_KEY`` si existe; si no, se deriva de ``SPOTIPY_CLIENT_SECRET``,
  que ya es secreto (así no hace falta otro secret más).
* La primera vez se copia el token del secret ``SPOTIFY_CACHE`` (contenido de tu ``.cache``);
  a partir de ahí spotipy lo renueva y lo guarda aquí solo, aunque Spotify rote el refresh token.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time

from spotipy.cache_handler import CacheHandler, MemoryCacheHandler

from config import ConfigError, required_env
from db import Database

_KEY_CONTEXT = b"autospoti/spotify-tokens/v1:"


def _fernet():
    from cryptography.fernet import Fernet

    explicit = os.environ.get("TOKEN_ENCRYPTION_KEY", "").strip()
    if explicit:
        try:
            return Fernet(explicit.encode())
        except ValueError as exc:
            raise ConfigError(
                "TOKEN_ENCRYPTION_KEY no es una clave Fernet válida (44 caracteres base64)."
            ) from exc
    secret = required_env("SPOTIPY_CLIENT_SECRET").encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(_KEY_CONTEXT + secret).digest()))


def encrypt_token(token_info: dict) -> str:
    return _fernet().encrypt(json.dumps(token_info).encode()).decode()


def decrypt_token(blob: str) -> "dict | None":
    """None si no se puede descifrar (p. ej. cambió la clave): hay que volver a autorizar."""
    from cryptography.fernet import InvalidToken

    try:
        return json.loads(_fernet().decrypt(blob.encode()))
    except (InvalidToken, ValueError):
        return None


def save_token(db: Database, user_id: str, token_info: dict) -> None:
    db.execute(
        "INSERT INTO spotify_tokens (user_id, token, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT (user_id) DO UPDATE SET token = excluded.token, updated_at = excluded.updated_at",
        (user_id, encrypt_token(token_info), int(time.time())),
    )
    db.commit()


def load_token(db: Database, user_id: str) -> "dict | None":
    row = db.execute("SELECT token FROM spotify_tokens WHERE user_id = ?", (user_id,)).fetchone()
    return decrypt_token(row[0]) if row else None


def delete_token(db: Database, user_id: str) -> None:
    """Olvida la cuenta: sin token, el cron deja de registrar sus escuchas."""
    db.execute("DELETE FROM spotify_tokens WHERE user_id = ?", (user_id,))
    db.commit()


def list_accounts(db: Database) -> list[str]:
    """Usuarios de Spotify con token guardado (orden estable)."""
    return [r[0] for r in db.execute("SELECT user_id FROM spotify_tokens ORDER BY user_id").fetchall()]


class DbCacheHandler(CacheHandler):
    """CacheHandler de spotipy que lee/escribe el token de ``user_id`` en la BD."""

    def __init__(self, db: Database, user_id: str):
        self.db = db
        self.user_id = user_id

    def get_cached_token(self):
        return load_token(self.db, self.user_id)

    def save_token_to_cache(self, token_info):
        save_token(self.db, self.user_id, token_info)


def bootstrap_from_env(db: Database) -> "str | None":
    """Si la BD no tiene ningún token y existe ``SPOTIFY_CACHE``, lo guarda a nombre de su
    usuario. Devuelve ese user_id (o None si no había nada que importar)."""
    raw = os.environ.get("SPOTIFY_CACHE", "").strip()
    if not raw or list_accounts(db):
        return None
    try:
        token_info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError("SPOTIFY_CACHE no es un JSON válido (copia el contenido de .cache).") from exc

    from spotify_core import get_spotify_client  # evita import circular

    memory = MemoryCacheHandler(token_info=token_info)
    sp = get_spotify_client(cache_handler=memory)
    user_id = sp.current_user()["id"]
    save_token(db, user_id, memory.get_cached_token() or token_info)
    return user_id
