"""Web de autospoti (FastAPI). Sustituye a Streamlit en la nube; Streamlit queda para uso local.

* Login con Spotify (web_auth) y sesión en cookie firmada de 30 días.
* Cada petición usa su propia conexión a la BD por hilo (reutilizada mientras la función
  siga "caliente" en Vercel) y solo llama a Spotify cuando hace falta.
* Toda la lógica vive en los módulos compartidos (history, smart, stats, jobs, blend):
  aquí solo hay rutas y plantillas.

Arranque local:  uvicorn webapp.main:create_app --factory --reload
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import blend
import charts
import history
import jobs
import smart
import stats
import tokens
import web_auth
from config import ConfigError, get_min_play_ms, get_timezone, required_env
from dateranges import MESES_ES, format_ts, range_for_dates, range_last_days
from history_import import ImportStats, rows_from_export
from spotify_core import friendly_error, get_clean_track_uris, get_spotify_client, get_top_tracks
from webapp import forms

BASE = Path(__file__).resolve().parent
SESSION_MAX_AGE = 30 * 24 * 3600
IMPORT_BATCH_MAX = 5000          # entradas por petición (Vercel limita el cuerpo a 4,5 MB)
STATS_CACHE_TTL = 600
MIN_YEAR = 2008

NAV = [
    ("Tu música", [("/", "Inicio", "home"), ("/stats", "Estadísticas", "insights")]),
    ("Crear playlists", [("/custom", "Personalizada", "tune"), ("/rediscover", "Redescubrimientos", "history"),
                         ("/blend", "En pareja", "favorite"), ("/recipes", "Mis recetas", "auto_awesome"),
                         ("/top", "Top de Spotify", "trophy")]),
    ("Ajustes", [("/import", "Importar historial", "upload_file"), ("/account", "Tu cuenta", "account_circle")]),
]

templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.globals.update(MESES=MESES_ES, DIAS=smart.DIAS, FRANJAS=smart.FRANJAS, NAV=NAV,
                             BLEND_MODES=blend.MODES)


# ------------------------------------------------------------------ conexiones

_local = threading.local()
_all_connections: list = []
_conn_lock = threading.Lock()


def get_db():
    """Una conexión por hilo, reutilizada entre peticiones (Postgres: reconecta si se cortó)."""
    db = getattr(_local, "db", None)
    if db is None:
        db = history.connect(check_same_thread=False)
        _local.db = db
        with _conn_lock:
            _all_connections.append(db)
    return db.ensure_alive()


def close_all_connections() -> None:
    """Para tests: cierra todas las conexiones abiertas por los hilos de la app."""
    with _conn_lock:
        for db in _all_connections:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
        _all_connections.clear()
    _local.__dict__.clear()


# ------------------------------------------------------------------ utilidades

class LoginRequired(Exception):
    pass


def _session_secret() -> str:
    explicit = os.environ.get("SESSION_SECRET", "").strip()
    if explicit:
        return explicit
    return hashlib.sha256(b"autospoti/session/v1:" + required_env("SPOTIPY_CLIENT_SECRET").encode()).hexdigest()


def current_user(request: Request) -> str:
    """Usuario de la sesión. Si su cuenta se olvidó o ya no está permitida, cierra la sesión."""
    uid = request.session.get("uid")
    if not uid:
        raise LoginRequired
    db = get_db()
    if not web_auth.is_allowed(uid) or tokens.load_token(db, uid) is None:
        request.session.clear()
        raise LoginRequired
    return uid


def flash(request: Request, kind: str, text: str, url: "str | None" = None) -> None:
    request.session.setdefault("flash", []).append({"kind": kind, "text": text, "url": url})


def flash_outcome(request: Request, outcome: jobs.JobOutcome) -> None:
    if outcome.ok:
        state = "creada" if outcome.result.created else "actualizada"
        flash(request, "ok", f"{outcome.name}: {state} con {outcome.result.track_count} canciones.",
              outcome.result.url)
    else:
        flash(request, "warn", f"{outcome.name}: no se actualizó. {outcome.note}")


def render(request: Request, template: str, active: str = "", **ctx) -> HTMLResponse:
    uid = request.session.get("uid")
    db = get_db() if uid else None
    ctx.update(
        request=request,
        uid=uid,
        display_name=request.session.get("name") or uid,
        flashes=request.session.pop("flash", []),
        active=active,
        pending_invites=len(blend.incoming(db, uid)) if db is not None else 0,
    )
    return templates.TemplateResponse(request, template, ctx)


def spotify_for(db, uid: str):
    return get_spotify_client(cache_handler=tokens.DbCacheHandler(db, uid))


def back(request: Request, default: str = "/") -> RedirectResponse:
    """Vuelve a la página de origen (solo rutas de esta web)."""
    parsed = urlparse(request.headers.get("referer") or "")
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    if parsed.netloc != request.headers.get("host") or not path.startswith("/") or path.startswith("//"):
        path = default
    return RedirectResponse(path, status_code=303)


def years_available(db, uid: str, tz) -> list[int]:
    cov = history.coverage(db, uid)
    this_year = date.today().year
    first = datetime.fromtimestamp(cov.first_ts, tz).year if cov.first_ts else this_year
    return list(range(this_year, max(MIN_YEAR, first) - 1, -1))


def tracks_rows(tracks, last_seen=None, tz=None) -> list[dict]:
    rows = []
    for i, t in enumerate(tracks, 1):
        row = {"pos": i, "name": t.name, "artist": t.artist, "plays": t.plays}
        if last_seen is not None:
            row["last"] = format_ts(last_seen.get(t.uri), tz)[:10]
        rows.append(row)
    return rows


_stats_cache: dict = {}


def cached_stats(db, uid: str, tz, start_ts: int, end_ts: int) -> stats.Stats:
    cov = history.coverage(db, uid)
    key = (uid, str(tz), start_ts, end_ts, cov.total, cov.last_ts, get_min_play_ms())
    hit = _stats_cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    value = stats.compute(history.fetch_plays(db, uid, start_ts, end_ts), tz, min_ms=get_min_play_ms())
    if len(_stats_cache) > 64:
        _stats_cache.clear()
    _stats_cache[key] = (time.time() + STATS_CACHE_TTL, value)
    return value


# ------------------------------------------------------------------ app

def create_app() -> FastAPI:
    app = FastAPI(title="autospoti", docs_url=None, redoc_url=None, openapi_url=None)
    https_only = web_auth.web_redirect_uri().startswith("https://")
    app.add_middleware(SessionMiddleware, secret_key=_session_secret(), session_cookie="autospoti",
                       max_age=SESSION_MAX_AGE, same_site="lax", https_only=https_only)
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")

    @app.middleware("http")
    async def same_origin_posts(request: Request, call_next):
        # Defensa CSRF (además de la cookie SameSite=Lax): un POST debe venir de esta web.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return PlainTextResponse("Origen no permitido.", status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.exception_handler(LoginRequired)
    async def to_login(request: Request, exc: LoginRequired):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "Tu sesión ha caducado. Vuelve a entrar."}, status_code=401)
        return RedirectResponse("/", status_code=303)

    @app.exception_handler(ConfigError)
    async def config_error(request: Request, exc: ConfigError):
        return PlainTextResponse(f"Configuración incompleta: {exc}", status_code=500)

    # -------------------------------------------------------------- login

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        try:
            uid = current_user(request)
        except LoginRequired:
            return render(request, "login.html", error=request.query_params.get("error", ""))
        db, tz = get_db(), get_timezone()
        cov = history.coverage(db, uid)
        week = history.count_plays(db, uid, *range_last_days(7))
        return render(request, "home.html", active="/", cov=cov, week=week,
                      first=format_ts(cov.first_ts, tz)[:10], last=format_ts(cov.last_ts, tz),
                      recipes=len(history.list_recipes(db, uid)), partners=len(blend.partners(db, uid)))

    @app.get("/login")
    def login():
        return RedirectResponse(web_auth.authorize_url(), status_code=303)

    @app.get("/callback")
    def callback(request: Request, code: str = "", state: str = "", error: str = ""):
        if error:
            return RedirectResponse("/?" + urlencode({"error": "Has cancelado el inicio de sesión."}), 303)
        try:
            result = web_auth.complete_login(get_db(), code, state)
        except web_auth.LoginError as exc:
            return RedirectResponse("/?" + urlencode({"error": str(exc)}), 303)
        request.session.clear()
        request.session.update(uid=result.user_id, name=result.display_name)
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/", status_code=303)

    @app.get("/account", response_class=HTMLResponse)
    def account(request: Request):
        uid = current_user(request)
        return render(request, "account.html", active="/account")

    @app.post("/account/forget")
    def forget(request: Request, confirm: str = Form("")):
        uid = current_user(request)
        if confirm != "1":
            flash(request, "warn", "Marca la casilla para confirmar.")
            return RedirectResponse("/account", 303)
        tokens.delete_token(get_db(), uid)
        request.session.clear()
        return RedirectResponse("/?" + urlencode({"error": "Cuenta olvidada: ya no se registra lo que escuchas."}),
                                303)

    # -------------------------------------------------------------- automáticas

    @app.post("/auto")
    def auto(request: Request):
        uid = current_user(request)
        db = get_db()
        try:
            for outcome in jobs.update_auto_playlists(spotify_for(db, uid), db, uid):
                flash_outcome(request, outcome)
        except Exception as exc:  # noqa: BLE001
            flash(request, "error", friendly_error(exc))
        return RedirectResponse("/", 303)

    # -------------------------------------------------------------- estadísticas

    @app.get("/stats", response_class=HTMLResponse)
    def stats_page(request: Request):
        uid = current_user(request)
        db, tz = get_db(), get_timezone()
        today = date.today()
        years = years_available(db, uid, tz)
        p = forms.Params(request.query_params)
        period = p.get("period", "anio_actual")
        year = p.int("year", years[0], MIN_YEAR, today.year)
        if period == "30d":
            start_ts, end_ts = range_for_dates(today - timedelta(days=29), today, tz)
        elif period == "anio":
            start_ts, end_ts = range_for_dates(date(year, 1, 1), date(year, 12, 31), tz)
        elif period == "todo":
            start_ts, end_ts = range_for_dates(date(MIN_YEAR, 1, 1), today, tz)
        else:
            period = "anio_actual"
            start_ts, end_ts = range_for_dates(date(today.year, 1, 1), today, tz)
        result = cached_stats(db, uid, tz, start_ts, end_ts)
        specs = {}
        if result.plays:
            specs = {
                "period": charts.period_bars(result.by_period, result.period_unit).to_dict(),
                "artists": charts.ranked_bars(result.top_artists, "Artista").to_dict(),
                "heatmap": charts.week_heatmap(result.heatmap).to_dict(),
            }
        return render(request, "stats.html", active="/stats", s=result, specs=specs, period=period,
                      year=year, years=years, has_any=history.coverage(db, uid).total > 0)

    # -------------------------------------------------------------- crear playlists

    def preview(request: Request, template: str, active: str, build_spec, **extra):
        uid = current_user(request)
        db, tz = get_db(), get_timezone()
        error, spec, name, rows = "", None, "", []
        try:
            spec, name = build_spec(db, uid)
        except ValueError as exc:
            error = str(exc)
        tracks = smart.build_tracks(db, uid, spec, tz) if spec is not None and not error else []
        last_seen = smart.last_played(db, uid, [t.uri for t in tracks]) if spec and spec.kind == "rediscover" else None
        rows = tracks_rows(tracks, last_seen, tz)
        return render(request, template, active=active, spec=spec, spec_json=spec.to_json() if spec else "",
                      describe=spec.describe() if spec else "", name=name, rows=rows, error=error,
                      q=forms.Params(request.query_params), years=years_available(db, uid, tz),
                      today=date.today(), **extra)

    @app.get("/custom", response_class=HTMLResponse)
    def custom(request: Request):
        return preview(request, "custom.html", "/custom",
                       lambda db, uid: forms.custom_spec(request.query_params, date.today(),
                                                         get_min_play_ms() // 1000))

    @app.get("/rediscover", response_class=HTMLResponse)
    def rediscover(request: Request):
        return preview(request, "rediscover.html", "/rediscover",
                       lambda db, uid: forms.rediscover_spec(request.query_params, get_min_play_ms() // 1000))

    @app.get("/blend", response_class=HTMLResponse)
    def blend_page(request: Request):
        uid = current_user(request)
        db = get_db()
        linked = blend.partners(db, uid)
        incoming = blend.incoming(db, uid)
        outgoing = blend.outgoing(db, uid)
        names = tokens.display_names(db, sorted(set(linked + incoming + outgoing)))

        def build(db_, uid_):
            built = forms.blend_spec(request.query_params, linked, names, get_min_play_ms() // 1000)
            if built is None:
                raise ValueError("")
            return built

        response = preview(request, "blend.html", "/blend", build, partners=linked, incoming=incoming,
                           outgoing=outgoing, names=names)
        return response

    @app.post("/blend/invite")
    def blend_invite(request: Request, partner_id: str = Form("")):
        uid = current_user(request)
        try:
            state = blend.invite(get_db(), uid, partner_id)
        except ValueError as exc:
            flash(request, "error", str(exc))
        else:
            flash(request, "ok", "Ya estáis enlazados." if state == "accepted" else
                  f"Invitación enviada a {partner_id.strip()}. Cuando entre en autospoti verá la invitación.")
        return RedirectResponse("/blend", 303)

    @app.post("/blend/accept")
    def blend_accept(request: Request, from_id: str = Form("")):
        uid = current_user(request)
        if blend.accept(get_db(), uid, from_id):
            flash(request, "ok", "Invitación aceptada: ya podéis crear mezclas.")
        else:
            flash(request, "warn", "Esa invitación ya no existe.")
        return RedirectResponse("/blend", 303)

    @app.post("/blend/remove")
    def blend_remove(request: Request, other_id: str = Form("")):
        uid = current_user(request)
        blend.remove(get_db(), uid, other_id)
        flash(request, "ok", "Enlace eliminado: vuestras mezclas dejarán de actualizarse.")
        return RedirectResponse("/blend", 303)

    @app.post("/playlists/create")
    def create_playlist(request: Request, name: str = Form(""), spec: str = Form(""), keep: str = Form("")):
        uid = current_user(request)
        db, tz = get_db(), get_timezone()
        name = name.strip()[:100]
        try:
            if not name:
                raise ValueError("Pon un nombre a la playlist.")
            parsed = smart.Spec.from_json(spec)
            parsed.validate()  # antes de tocar Spotify
            # Las canciones se recalculan aquí: nunca se fía de una lista enviada por el navegador.
            outcome = jobs.update_recipe(spotify_for(db, uid), db, uid, name, parsed, tz, daily=keep == "1")
        except (ValueError, TypeError) as exc:
            flash(request, "error", str(exc) or "Receta no válida.")
            return back(request)
        except Exception as exc:  # noqa: BLE001
            flash(request, "error", friendly_error(exc))
            return back(request)
        if outcome.ok and keep == "1":
            history.save_recipe(db, uid, name, parsed.to_json())
            flash(request, "ok", f"«{name}» guardada en Mis recetas: se actualizará cada día.")
        flash_outcome(request, outcome)
        return back(request)

    # -------------------------------------------------------------- recetas

    @app.get("/recipes", response_class=HTMLResponse)
    def recipes(request: Request):
        uid = current_user(request)
        items = []
        for name, raw in history.list_recipes(get_db(), uid):
            try:
                spec = smart.Spec.from_json(raw)
                items.append({"name": name, "describe": spec.describe(), "limit": spec.limit, "ok": True})
            except (ValueError, TypeError):
                items.append({"name": name, "describe": "Receta no válida", "limit": 0, "ok": False})
        return render(request, "recipes.html", active="/recipes", items=items)

    @app.post("/recipes/run")
    def recipe_run(request: Request, name: str = Form("")):
        uid = current_user(request)
        db = get_db()
        found = dict(history.list_recipes(db, uid)).get(name)
        if found is None:
            flash(request, "warn", "Esa receta ya no existe.")
            return RedirectResponse("/recipes", 303)
        try:
            flash_outcome(request, jobs.update_recipe(spotify_for(db, uid), db, uid, name,
                                                      smart.Spec.from_json(found), get_timezone()))
        except Exception as exc:  # noqa: BLE001
            flash(request, "error", friendly_error(exc))
        return RedirectResponse("/recipes", 303)

    @app.post("/recipes/run-all")
    def recipe_run_all(request: Request):
        uid = current_user(request)
        db = get_db()
        try:
            for outcome in jobs.update_recipes(spotify_for(db, uid), db, uid, get_timezone()):
                flash_outcome(request, outcome)
        except Exception as exc:  # noqa: BLE001
            flash(request, "error", friendly_error(exc))
        return RedirectResponse("/recipes", 303)

    @app.post("/recipes/delete")
    def recipe_delete(request: Request, name: str = Form("")):
        uid = current_user(request)
        history.delete_recipe(get_db(), uid, name)
        flash(request, "ok", f"Receta «{name}» borrada. La playlist se queda en tu Spotify.")
        return RedirectResponse("/recipes", 303)

    # -------------------------------------------------------------- top de Spotify

    TOP_RANGES = {"short_term": "Últimas ≈4 semanas", "medium_term": "Últimos ≈6 meses",
                  "long_term": "Aprox. último año o más"}

    @app.get("/top", response_class=HTMLResponse)
    def top(request: Request):
        current_user(request)
        return render(request, "top.html", active="/top", ranges=TOP_RANGES)

    @app.post("/top/create")
    def top_create(request: Request, time_range: str = Form("short_term"), name: str = Form(""),
                   limit: int = Form(50)):
        uid = current_user(request)
        db = get_db()
        if time_range not in TOP_RANGES or not name.strip():
            flash(request, "error", "Elige un periodo y pon un nombre.")
            return RedirectResponse("/top", 303)
        try:
            sp = spotify_for(db, uid)
            uris = get_clean_track_uris(get_top_tracks(sp, time_range, max(10, min(100, limit))))
            result = jobs.write_playlist(sp, db, uid, name.strip()[:100], uris,
                                         f"Ranking de Spotify: {TOP_RANGES[time_range]}.")
        except Exception as exc:  # noqa: BLE001
            flash(request, "error", friendly_error(exc))
            return RedirectResponse("/top", 303)
        flash_outcome(request, jobs.JobOutcome(name.strip(), result,
                                               "Spotify no devolvió canciones para ese periodo."))
        return RedirectResponse("/top", 303)

    # -------------------------------------------------------------- importar

    @app.get("/import", response_class=HTMLResponse)
    def import_page(request: Request):
        uid = current_user(request)
        db, tz = get_db(), get_timezone()
        cov = history.coverage(db, uid)
        return render(request, "import.html", active="/import", cov=cov,
                      first=format_ts(cov.first_ts, tz), last=format_ts(cov.last_ts, tz),
                      batch=IMPORT_BATCH_MAX)

    @app.post("/api/import")
    async def import_batch(request: Request):
        uid = current_user(request)
        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse({"error": "El lote no es JSON válido."}, status_code=400)
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return JSONResponse({"error": "Falta la lista de reproducciones."}, status_code=400)
        if len(entries) > IMPORT_BATCH_MAX:
            return JSONResponse({"error": f"Máximo {IMPORT_BATCH_MAX} reproducciones por lote."}, status_code=413)
        rows, result = rows_from_export(entries)
        result.inserted, result.duplicates = history.add_plays(get_db(), uid, rows, "export")
        return JSONResponse({k: getattr(result, k) for k in ImportStats.__dataclass_fields__})

    return app
