# autospoti

App en Python para crear playlists de Spotify (Streamlit + spotipy + SQLite).

## Comandos
- App web: `streamlit run autoplaylist.py`
- Autorizar Spotify (una vez): `python auth.py`
- Tarea programada: `python cron.py` (todo) o `python cron.py --record` (solo registrar)
- Importar historial exportado: `python import_history.py "<carpeta o .json>"`
- Tests: `python -m unittest discover -s tests` (con `TEST_DATABASE_URL` también contra Postgres)

## Estructura
- `config.py` carga `.env`; `spotify_core.py` = API de Spotify (sin Streamlit)
- `db.py` = conexión y esquema: SQLite `data/history.db` en local, Postgres si hay `DATABASE_URL`. SQL portable con `?` (se traduce a `%s`); nada de `INSERT OR IGNORE`/`PRAGMA` fuera de `db.py`
- `tokens.py` = tokens de Spotify cifrados en la BD (modo nube); `history.py` = consultas del historial
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
