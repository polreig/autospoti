# autospoti

Sigue [ESTANDAR_PROYECTOS.md](ESTANDAR_PROYECTOS.md) (obligatorio). Aquí solo va lo propio de este proyecto.

App en Python para crear playlists de Spotify: web FastAPI (Vercel) + spotipy + Postgres/SQLite; app Streamlit solo para uso local.

## Comandos
- Instalar todo en local: `python -m pip install -r requirements-local.txt` (`requirements.txt` = nube, sin Streamlit)
- Web: `uvicorn webapp.main:create_app --factory --reload` (Vercel entra por `api/index.py`)
- App Streamlit (local, una cuenta): `streamlit run autoplaylist.py`
- Autorizar Spotify (una vez): `python auth.py`
- Tarea programada: `python cron.py` (todo) o `python cron.py --record` (solo registrar)
- Importar historial exportado: `python import_history.py "<carpeta o .json>"`
- Tests: `python -m unittest discover -s tests` (con `TEST_DATABASE_URL` también contra Postgres)

## Estructura
- `config.py` carga `.env`; `spotify_core.py` = API de Spotify (sin Streamlit)
- `db.py` = conexión y esquema: SQLite `data/history.db` en local, Postgres si hay `DATABASE_URL`. SQL portable con `?` (se traduce a `%s`); nada de `INSERT OR IGNORE`/`PRAGMA` fuera de `db.py`
- `tokens.py` = tokens de Spotify cifrados en la BD (modo nube); `history.py` = consultas del historial
- `smart.py` = recetas (`Spec` en JSON, tabla `recipes`) con filtros y redescubrimientos; `stats.py` = cálculos del panel; `charts.py` = gráficos Altair (una serie, color de acento, tooltip). Filtros de hora/día/mes en Python con la zona horaria, nunca en SQL
- `blend.py` = enlaces de pareja con consentimiento (`blend_links`) y mezclas; nunca leer el historial de otra cuenta sin `blend.is_linked`
- `webapp/` = web FastAPI: `main.py` (rutas), `forms.py` (query params -> Spec), `templates/` (Jinja2, autoescape), `static/`. Toda ruta privada llama a `current_user()`; los POST pasan el control de mismo origen; las canciones se recalculan en el servidor, nunca se aceptan del navegador
- `web_auth.py` = login OAuth de la web (state firmado con HMAC, lista `ALLOWED_SPOTIFY_IDS`), sin Streamlit
- `autoplaylist.py` = interfaz Streamlit (local): una función `page_*(ctx)` por página registrada en `_pages()`; tema en `.streamlit/config.toml`. Cada página nueva (en Streamlit o en `webapp/`) debe añadirse a su `test_every_page_renders`
- `history_import.py` = export de Spotify; `dateranges.py` = rangos
- `jobs.py` = lógica compartida por la app y `cron.py`; `autoplaylist.py` = solo interfaz

## Reglas
- NUNCA escribir claves en el código ni leer/mostrar `.env`, `.cache` o `data/`. Las claves viven solo en `.env`.
- Mantener un único SCOPE (`spotify_core.SCOPE`): si cambia, hay que repetir `python auth.py`.
- `recently_played` devuelve máx. 50: no es un ranking ni "7 días". El top de 7 días sale de la BD propia.
- La API de Spotify (feb 2026) usa `/items` en playlists; spotipy >= 2.26. No usar endpoints antiguos `/tracks`.
- Listas vacías nunca deben vaciar una playlist existente.
- Todo instante se guarda en UTC (segundos Unix); la zona (`APP_TIMEZONE`) solo se aplica al interpretar fechas.
- Toda consulta o escritura del historial lleva `user_id` (preparado para varios usuarios). Las playlists se escriben con `jobs.write_playlist` (recuerda su ID en `managed_playlists`) y compartiendo un `PlaylistIndex` por ejecución.
- Añadir un test en `tests/` por cada cambio de lógica. Respuestas y textos de la interfaz en español.

## Pendiente para cumplir el estándar

Es una app web en la nube: aplica la parte "Apps web o en la nube" del punto 3 del estándar.

- **README:** reorganizarlo con la plantilla del estándar (punto 4).
  - "Antes de empezar": cuentas de Spotify Developer, Neon, GitHub y Vercel.
  - Cada servicio en su propio paso, con los botones exactos.
  - **Tabla de variables y secretos**: nombre, para qué sirve, de dónde sale y dónde se pone (GitHub Secrets o Vercel).
  - Comprobación final, "Actualizar", "Problemas frecuentes" (incluido el aviso de los 60 días de GitHub Actions) y capturas en `docs/capturas/`.
- **`CHANGELOG.md`:** crearlo, empezando por la versión actual.
- **Uso local:** un `Instalar.bat` que instale `requirements-local.txt` y cree un acceso directo a `Abrir Autospoti.bat`.
- **`.gitattributes`:** `*.bat` y `*.ps1` con saltos de línea CRLF.
