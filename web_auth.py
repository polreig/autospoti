"""Login con Spotify en la app web (OAuth Authorization Code), sin dependencias de Streamlit.

Flujo:
  1. ``authorize_url()`` -> el usuario va a Spotify y acepta.
  2. Spotify vuelve a ``WEB_REDIRECT_URI`` con ``?code=...&state=...``.
  3. ``complete_login(db, code, state)`` comprueba el ``state``, canjea el código, mira la
     lista de permitidos y guarda el token cifrado en la BD (tokens.py).

El ``state`` (protección CSRF) va firmado con HMAC y caduca: no hace falta guardarlo en la
sesión, que Streamlit pierde al salir a Spotify y volver.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass

import spotipy
from spotipy.cache_handler import MemoryCacheHandler

import tokens
from config import ConfigError, required_env
from db import Database
from spotify_core import build_auth_manager

DEFAULT_WEB_REDIRECT_URI = "http://127.0.0.1:8000/callback"  # web FastAPI en local (uvicorn)
STATE_MAX_AGE = 15 * 60  # segundos para completar el login en Spotify


class LoginError(RuntimeError):
    """El login no se pudo completar (mensaje apto para mostrar al usuario)."""


@dataclass
class LoginResult:
    user_id: str
    display_name: str


def web_redirect_uri() -> str:
    return os.environ.get("WEB_REDIRECT_URI", "").strip() or DEFAULT_WEB_REDIRECT_URI


def allowed_ids() -> "set[str] | None":
    """IDs de Spotify permitidos (ALLOWED_SPOTIFY_IDS, separados por comas). None = sin lista."""
    raw = os.environ.get("ALLOWED_SPOTIFY_IDS", "").strip()
    if not raw:
        return None
    return {part.strip() for part in raw.split(",") if part.strip()}


def is_allowed(user_id: str) -> bool:
    allowed = allowed_ids()
    return allowed is None or user_id in allowed


def _state_key() -> bytes:
    return hashlib.sha256(b"autospoti/oauth-state/v1:" + required_env("SPOTIPY_CLIENT_SECRET").encode()).digest()


def make_state(now: "float | None" = None) -> str:
    issued = str(int(now if now is not None else time.time()))
    nonce = secrets.token_urlsafe(8)
    payload = f"{issued}.{nonce}"
    sig = hmac.new(_state_key(), payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"


def verify_state(state: str, now: "float | None" = None) -> bool:
    try:
        issued, nonce, sig = state.split(".")
        expected = hmac.new(_state_key(), f"{issued}.{nonce}".encode(), hashlib.sha256).digest()
        given = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
        age = (now if now is not None else time.time()) - int(issued)
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(expected, given) and 0 <= age <= STATE_MAX_AGE


def _oauth(cache: MemoryCacheHandler):
    return build_auth_manager(cache_handler=cache, redirect_uri=web_redirect_uri())


def authorize_url() -> str:
    return _oauth(MemoryCacheHandler()).get_authorize_url(state=make_state())


def complete_login(db: Database, code: str, state: str) -> LoginResult:
    """Canjea el código de Spotify y guarda el token del usuario. Lanza LoginError."""
    if not code or not verify_state(state or ""):
        raise LoginError("El enlace de inicio de sesión no es válido o ha caducado. Vuelve a intentarlo.")
    cache = MemoryCacheHandler()
    try:
        _oauth(cache).get_access_token(code=code, as_dict=False, check_cache=False)
        token_info = cache.get_cached_token()
        if not token_info:
            raise LoginError("Spotify no devolvió ningún token.")
        me = spotipy.Spotify(auth=token_info["access_token"], requests_timeout=20).current_user()
    except (LoginError, ConfigError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise LoginError(f"No se pudo completar el login con Spotify ({type(exc).__name__}: {exc}).") from exc
    user_id = me["id"]
    if not is_allowed(user_id):
        raise LoginError(f"La cuenta '{user_id}' no tiene acceso a esta app.")
    tokens.save_token(db, user_id, token_info)
    display_name = me.get("display_name") or user_id
    tokens.save_profile(db, user_id, display_name)
    return LoginResult(user_id=user_id, display_name=display_name)
