"""Importa tu historial exportado de Spotify desde la línea de comandos.

Uso:
    python import_history.py "C:\\ruta\\Spotify Extended Streaming History"
    python import_history.py Streaming_History_Audio_2022_3.json otro.json
    python import_history.py --user-id TU_ID_DE_SPOTIFY carpeta   # sin consultar a Spotify

Las reproducciones se guardan a nombre de la cuenta autorizada con ``python auth.py``
(o de ``--user-id``), para que no se mezclen si en el futuro hay varios usuarios.
"""
import argparse
import sys

import history
from config import ConfigError
from history_import import import_export_files
from spotify_core import AuthRequiredError, friendly_error, get_spotify_client


def _resolve_user_id(explicit: "str | None") -> str:
    if explicit:
        return explicit.strip()
    return get_spotify_client().current_user()["id"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", help="carpetas o archivos .json del historial")
    parser.add_argument("--user-id", help="ID de usuario de Spotify (por defecto, el de la cuenta autorizada)")
    args = parser.parse_args()

    try:
        user_id = _resolve_user_id(args.user_id)
    except (ConfigError, AuthRequiredError) as exc:
        print(f"ERROR: {exc}\nO indica el usuario con --user-id.")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {friendly_error(exc)}")
        return 1

    conn = history.connect()
    try:
        history.claim_orphan_plays(conn, user_id)
        stats = import_export_files(conn, user_id, args.paths)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1
    finally:
        conn.close()

    print(f"Usuario: {user_id}")
    print(f"Archivos: {stats.files} | Entradas: {stats.entries}")
    print(f"Insertadas: {stats.inserted} | Duplicadas: {stats.duplicates}")
    print(f"Omitidas -> sin URI/podcasts: {stats.no_uri}, no son canciones: {stats.not_track}, "
          f"formato antiguo: {stats.legacy_format}, inválidas: {stats.invalid}")
    if stats.files == 0:
        print("No se encontró ningún Streaming_History_Audio_*.json en las rutas indicadas.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
