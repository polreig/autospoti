"""Configuración común: rutas, variables de entorno (.env) y zona horaria."""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

# override=False: las variables de entorno reales (p. ej. secrets de Streamlit Cloud)
# tienen prioridad sobre lo que haya en el archivo .env.
load_dotenv(ENV_PATH)

CACHE_PATH = BASE_DIR / ".cache"
DATA_DIR = BASE_DIR / "data"


def get_db_path() -> Path:
    """Ruta de la base de datos SQLite del historial (se lee en cada llamada)."""
    custom = os.environ.get("HISTORY_DB_PATH", "").strip()
    return Path(custom) if custom else DATA_DIR / "history.db"


class ConfigError(RuntimeError):
    """Falta o es inválida alguna variable de configuración."""


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"Falta la variable {name}. Rellénala en {ENV_PATH} (plantilla: .env.example)."
        )
    return value


def get_timezone() -> ZoneInfo:
    name = os.environ.get("APP_TIMEZONE", "").strip() or "Europe/Madrid"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(
            f"APP_TIMEZONE='{name}' no es una zona horaria válida (ej. Europe/Madrid)."
        ) from exc


def history_enabled() -> bool:
    """False si HISTORY_ENABLED es false/0/no/off (p. ej. Streamlit Cloud, sin disco persistente)."""
    raw = os.environ.get("HISTORY_ENABLED", "").strip().lower()
    return raw not in ("false", "0", "no", "off")


def get_min_play_ms() -> int:
    raw = os.environ.get("MIN_PLAY_SECONDS", "").strip() or "30"
    try:
        return max(0, int(raw)) * 1000
    except ValueError as exc:
        raise ConfigError("MIN_PLAY_SECONDS debe ser un número entero de segundos.") from exc
