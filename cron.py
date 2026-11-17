"""Tarea programada: registra tus reproducciones y actualiza las playlists automáticas.

Uso:
    python cron.py             # registra + actualiza las 6 automáticas y las recetas, y crea
                               # la playlist mensual del mes pasado si aún no existe
    python cron.py --record    # solo registra reproducciones (ejecutar cada hora)
    python cron.py --meses     # crea TODAS las playlists mensuales que faltan
    python cron.py --meses --vista-previa   # solo enseña qué meses se crearían
    python cron.py --generos   # clasifica por género una tanda grande de artistas (~20 min)
    python cron.py --api-only  # sin base de datos: solo las 3 playlists que salen de la API
                               # (no toca las de 7 días, último año ni Always).

Dónde se guarda todo:
  * Local (sin DATABASE_URL): SQLite ``data/history.db`` y token en ``.cache``.
  * Nube (con DATABASE_URL): Postgres; los tokens de TODAS las cuentas viven en la BD y se
    procesan una a una (la primera vez se importa el token del secret SPOTIFY_CACHE).

``recently_played`` solo devuelve las últimas 50: programa --record cada hora o perderás
reproducciones si escuchas mucho.

Cada ejecución (también la horaria) clasifica además una tanda pequeña de artistas por
género (genres.py), así todo el historial queda clasificado solo en uno o dos días.
"""
import argparse
import sys

import genres
import history
import jobs
import monthly
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


def _print_month_plan(plans, prefix: str = "") -> None:
    labels = {monthly.EXISTS: "ya la tienes", monthly.MANAGED: "ya la creó autospoti",
              monthly.FEW: "muy pocas escuchas, no se crea", monthly.TO_CREATE: "SE CREARÁ"}
    for p in plans:
        extra = f" ({len(p.tracks)} canciones)" if p.status == monthly.TO_CREATE else ""
        print(f"{prefix}{p.name:<16} {labels[p.status]}{extra}")
    missing = sum(p.status == monthly.TO_CREATE for p in plans)
    print(f"{prefix}{len(plans)} meses completos en tu historial; se crearían {missing}.")


# Tandas de clasificación por género: (artistas como máximo, segundos como máximo).
GENRES_SMALL = (60, 90)          # en cada ejecución horaria y diaria
GENRES_BIG = (3000, 20 * 60)     # modo --generos (a mano)


def _tag_genres(sp, conn, user_id: str, budget: tuple[int, int], prefix: str = "") -> None:
    """Clasifica una tanda de artistas. Un fallo aquí nunca hace fallar el registro."""
    try:
        result = genres.tag_artists(sp, conn, user_id, max_artists=budget[0], max_seconds=budget[1])
        if result.processed or result.stopped:
            left = genres.coverage(conn, user_id).pending
            print(f"{prefix}Géneros: {result.processed} artistas clasificados ({result.with_genre} con "
                  f"género); faltan {left}." + (f" Parado: {result.stopped}." if result.stopped else ""))
    except Exception as exc:  # noqa: BLE001
        print(f"{prefix}Géneros: no se pudieron clasificar ahora ({friendly_error(exc)}).")


def _run_user(sp, conn, user_id: str, mode: str, prefix: str = "") -> int:
    """Procesa una cuenta. Devuelve el número de playlists que no se pudieron actualizar.

    ``mode``: "record" (solo registrar), "full" (diaria), "months" (crear todas las mensuales
    que faltan), "months-preview" (solo enseñar cuáles) o "genres" (tanda grande de géneros).
    """
    if mode == "record":
        new, dup = jobs.record_recent_plays(sp, conn, user_id)
        print(f"{prefix}Registro: {new} nuevas, {dup} ya existentes.")
        _tag_genres(sp, conn, user_id, GENRES_SMALL, prefix)
        return 0
    if mode == "genres":
        _tag_genres(sp, conn, user_id, GENRES_BIG, prefix)
        return 0
    tz = get_timezone()
    index = PlaylistIndex(sp, user_id)  # compartido por todas las escrituras de la ejecución
    if mode == "months-preview":
        _print_month_plan(jobs.monthly_plan(sp, conn, user_id, tz, index), prefix)
        return 0
    if mode == "months":
        jobs.record_recent_plays(sp, conn, user_id)
        outcomes = jobs.create_monthly_playlists(sp, conn, user_id, tz, index)
        if not outcomes:
            print(f"{prefix}No falta ninguna playlist mensual.")
        return _print_outcomes(outcomes, prefix)
    outcomes = jobs.update_auto_playlists(sp, conn, user_id, index=index)
    outcomes += jobs.update_recipes(sp, conn, user_id, tz, index=index)
    outcomes += jobs.create_monthly_playlists(sp, conn, user_id, tz, index, only_previous_month=True)
    failures = _print_outcomes(outcomes, prefix)
    _tag_genres(sp, conn, user_id, GENRES_SMALL, prefix)  # después: nunca retrasa las playlists
    return failures


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


def _run_local(conn, mode: str) -> int:
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
    return 0 if _run_user(sp, conn, user_id, mode) == 0 else 2


def _run_cloud(conn, mode: str) -> int:
    """Todas las cuentas con token en la BD. Un fallo en una no detiene a las demás."""
    try:
        tokens.bootstrap_from_env(conn)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR al importar SPOTIFY_CACHE: {friendly_error(exc)}")
        return 1
    accounts = tokens.list_accounts(conn)
    if not accounts:
        # Normal hasta el primer login en la web: no es un fallo (no llenar el correo de avisos).
        print("Todavía no hay cuentas: entra una vez en la web con Spotify (o define el secret "
              "SPOTIFY_CACHE) y el registro empezará solo.")
        return 0
    errors = failures = 0
    for user_id in accounts:
        prefix = f"[{user_id}] " if len(accounts) > 1 else ""
        try:
            sp = get_spotify_client(cache_handler=tokens.DbCacheHandler(conn, user_id))
            failures += _run_user(sp, conn, user_id, mode, prefix)
        except (ConfigError, AuthRequiredError) as exc:
            errors += 1
            print(f"{prefix}ERROR: {exc}")
        except Exception as exc:  # noqa: BLE001
            errors += 1
            print(f"{prefix}ERROR: {friendly_error(exc)}")
    if errors:
        return 1
    return 0 if failures == 0 else 2


def run(record_only: bool = False, api_only: bool = False, months: bool = False,
        preview: bool = False, genres_only: bool = False) -> int:
    if sum((record_only, api_only, months, genres_only)) > 1:
        print("ERROR: --record, --api-only, --meses y --generos no se pueden combinar.")
        return 1
    if preview and not months:
        print("ERROR: --vista-previa solo se usa junto con --meses.")
        return 1
    if api_only:
        return _run_api_only()
    if record_only:
        mode = "record"
    elif months:
        mode = "months-preview" if preview else "months"
    elif genres_only:
        mode = "genres"
    else:
        mode = "full"
    try:
        conn = history.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: no se pudo abrir la base de datos: {type(exc).__name__}: {exc}")
        return 1
    try:
        return _run_cloud(conn, mode) if use_db_tokens() else _run_local(conn, mode)
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
    parser.add_argument("--meses", action="store_true",
                        help="crear todas las playlists mensuales que faltan")
    parser.add_argument("--vista-previa", action="store_true",
                        help="con --meses: solo enseñar qué meses se crearían")
    parser.add_argument("--generos", action="store_true",
                        help="clasificar por género una tanda grande de artistas")
    args = parser.parse_args()
    sys.exit(run(record_only=args.record, api_only=args.api_only, months=args.meses,
                 preview=args.vista_previa, genres_only=args.generos))
