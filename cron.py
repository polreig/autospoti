"""Tarea programada: registra tus reproducciones y actualiza las playlists automáticas.

Uso:
    python cron.py             # registra + actualiza las 5 playlists
    python cron.py --record    # solo registra reproducciones (ejecutar cada hora)
    python cron.py --api-only  # sin base de datos: solo las 3 playlists que salen de la API
                               # (no toca las de 7 días ni último año).

Dónde se guarda todo:
  * Local (sin DATABASE_URL): SQLite ``data/history.db`` y token en ``.cache``.
  * Nube (con DATABASE_URL): Postgres; los tokens de TODAS las cuentas viven en la BD y se
    procesan una a una (la primera vez se importa el token del secret SPOTIFY_CACHE).

``recently_played`` solo devuelve las últimas 50: programa --record cada hora o perderás
reproducciones si escuchas mucho.
"""
import argparse
import sys

import history
import jobs
import tokens
from config import ConfigError, get_timezone, use_db_tokens
from spotify_core import AuthRequiredError, PlaylistIndex, friendly_error, get_spotify_client


def _print_outcomes(outcomes: list[jobs.JobOutcome], prefix: str = "") -> int:
    failures = 0
    for outcome in outcomes:
        if outcome.ok:
            state = "creada" if outcome.result.created else "actualizada"
            print(f"{prefix}OK  {outcome.name}: {state} ({outcome.result.track_count} canciones)")
        else:
            failures += 1
            print(f"{prefix}--  {outcome.name}: no actualizada. {outcome.note}")
    return failures


def _run_user(sp, conn, user_id: str, record_only: bool, prefix: str = "") -> int:
    """Procesa una cuenta. Devuelve el número de playlists que no se pudieron actualizar."""
    if record_only:
        new, dup = jobs.record_recent_plays(sp, conn, user_id)
        print(f"{prefix}Registro: {new} nuevas, {dup} ya existentes.")
        return 0
    index = PlaylistIndex(sp, user_id)  # compartido por las automáticas y las recetas
    outcomes = jobs.update_auto_playlists(sp, conn, user_id, index=index)
    outcomes += jobs.update_recipes(sp, conn, user_id, get_timezone(), index=index)
    return _print_outcomes(outcomes, prefix)


def _run_api_only() -> int:
    try:
        sp = get_spotify_client()
        user_id = sp.current_user()["id"]
        return 0 if _print_outcomes(jobs.update_auto_playlists(sp, None, user_id, api_only=True)) == 0 else 2
    except (ConfigError, AuthRequiredError) as exc:
        print(f"ERROR: {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {friendly_error(exc)}")
    return 1


def _run_local(conn, record_only: bool) -> int:
    """Un único usuario con el token de .cache (tu PC)."""
    try:
        sp = get_spotify_client()
        user_id = sp.current_user()["id"]
    except (ConfigError, AuthRequiredError) as exc:
        print(f"ERROR: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {friendly_error(exc)}")
        return 1
    # Historial migrado del esquema antiguo (sin usuario): pasa a ser del dueño.
    history.claim_orphan_plays(conn, user_id)
    return 0 if _run_user(sp, conn, user_id, record_only) == 0 else 2


def _run_cloud(conn, record_only: bool) -> int:
    """Todas las cuentas con token en la BD. Un fallo en una no detiene a las demás."""
    try:
        tokens.bootstrap_from_env(conn)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR al importar SPOTIFY_CACHE: {friendly_error(exc)}")
        return 1
    accounts = tokens.list_accounts(conn)
    if not accounts:
        print("ERROR: no hay ninguna cuenta de Spotify en la base de datos. "
              "Define el secret SPOTIFY_CACHE (contenido de tu .cache) para la primera ejecución.")
        return 1
    errors = failures = 0
    for user_id in accounts:
        prefix = f"[{user_id}] " if len(accounts) > 1 else ""
        try:
            sp = get_spotify_client(cache_handler=tokens.DbCacheHandler(conn, user_id))
            failures += _run_user(sp, conn, user_id, record_only, prefix)
        except (ConfigError, AuthRequiredError) as exc:
            errors += 1
            print(f"{prefix}ERROR: {exc}")
        except Exception as exc:  # noqa: BLE001
            errors += 1
            print(f"{prefix}ERROR: {friendly_error(exc)}")
    if errors:
        return 1
    return 0 if failures == 0 else 2


def run(record_only: bool = False, api_only: bool = False) -> int:
    if record_only and api_only:
        print("ERROR: --record y --api-only no se pueden combinar.")
        return 1
    if api_only:
        return _run_api_only()
    try:
        conn = history.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: no se pudo abrir la base de datos: {type(exc).__name__}: {exc}")
        return 1
    try:
        return _run_cloud(conn, record_only) if use_db_tokens() else _run_local(conn, record_only)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {friendly_error(exc)}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--record", action="store_true", help="solo registrar reproducciones")
    parser.add_argument("--api-only", action="store_true",
                        help="sin base de datos: solo playlists que salen de la API")
    args = parser.parse_args()
    sys.exit(run(record_only=args.record, api_only=args.api_only))
