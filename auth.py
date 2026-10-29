"""Autorización inicial con Spotify. Ejecutar UNA vez:  python auth.py

Abre el navegador, pides permiso y se guarda el token en .cache (no se sube a git).
Las claves se leen del archivo .env.
"""
import sys

import spotipy

from config import ConfigError
from spotify_core import build_auth_manager, friendly_error


def main() -> int:
    print("Iniciando conexión con Spotify...")
    try:
        sp = spotipy.Spotify(auth_manager=build_auth_manager(open_browser=True))
        user = sp.current_user()
    except ConfigError as exc:
        print(f"Configuración incompleta: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - se muestra la causa real al usuario
        print(f"No se pudo conectar: {friendly_error(exc)}")
        return 1
    print(f"¡Éxito! Conectado correctamente como: {user.get('display_name') or user['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
