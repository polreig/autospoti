"""Tarea programada: registra tus reproducciones y actualiza las playlists automáticas.

Uso:
    python cron.py             # registra + actualiza las 5 playlists (en tu PC, con historial)
    python cron.py --record    # solo registra reproducciones (ejecutar cada 1-3 h)
    python cron.py --api-only  # sin base de datos: solo las 3 playlists que salen de la API
                               # (GitHub Actions). No toca las de 7 días ni último año.

``recently_played`` solo devuelve las últimas 50: si escuchas mucho, programa --record
con frecuencia o perderás reproducciones del registro.
"""
import argparse
import sys

import history
import jobs
from config import ConfigError
from spotify_core import AuthRequiredError, friendly_error, get_spotify_client


def run(record_only: bool = False, api_only: bool = False) -> int:
    if record_only and api_only:
        print("ERROR: --record y --api-only no se pueden combinar.")
        return 1
    try:
        sp = get_spotify_client()
        user_id = sp.current_user()["id"]
    except (ConfigError, AuthRequiredError) as exc:
        print(f"ERROR: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {friendly_error(exc)}")
        return 1

    conn = None if api_only else history.connect()
    try:
        if conn is not None:
            # Historial migrado del esquema antiguo (sin usuario): pasa a ser del dueño.
            history.claim_orphan_plays(conn, user_id)
        if record_only:
            new, dup = jobs.record_recent_plays(sp, conn, user_id)
            print(f"Registro: {new} nuevas, {dup} ya existentes.")
            return 0
        failures = 0
        for outcome in jobs.update_auto_playlists(sp, conn, user_id, api_only=api_only):
            if outcome.ok:
                state = "creada" if outcome.result.created else "actualizada"
                print(f"OK  {outcome.name}: {state} ({outcome.result.track_count} canciones)")
            else:
                failures += 1
                print(f"--  {outcome.name}: no actualizada. {outcome.note}")
        return 0 if failures == 0 else 2
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {friendly_error(exc)}")
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--record", action="store_true", help="solo registrar reproducciones")
    parser.add_argument("--api-only", action="store_true",
                        help="sin base de datos: solo playlists que salen de la API (GitHub Actions)")
    args = parser.parse_args()
    sys.exit(run(record_only=args.record, api_only=args.api_only))
