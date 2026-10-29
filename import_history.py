"""Importa tu historial exportado de Spotify desde la línea de comandos.

Uso:
    python import_history.py "C:\\ruta\\Spotify Extended Streaming History"
    python import_history.py Streaming_History_Audio_2022_3.json otro.json
"""
import argparse
import sys

import history
from history_import import import_export_files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", help="carpetas o archivos .json del historial")
    args = parser.parse_args()

    conn = history.connect()
    try:
        stats = import_export_files(conn, args.paths)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1
    finally:
        conn.close()

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
