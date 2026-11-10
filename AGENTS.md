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

## Excepciones al estándar

- La carpeta de datos se llama `data/` y no `datos/`: cambiarla movería las bases de datos locales existentes. No se renombra.
- `.clineignore` se queda: no son reglas, sino la lista de archivos que Cline no debe leer (protege `.env` y los datos).

## Pendiente 1 · Reorganizar el código en un paquete (estándar v2, punto 2)

> **Para Pol, en casa:**
> 1. Abre la carpeta de autospoti en VS Code y pulsa **Sync Changes** para tener lo último.
> 2. Comprueba que tienes tu `.env` con las claves (hace falta para probar la web en local).
> 3. Dile al agente: **"Haz el Pendiente 1 de AGENTS.md paso a paso, en una rama"**.
> 4. Al final, el agente te pedirá que compruebes la web de prueba de Vercel y que lances el workflow a mano en GitHub. Hazlo antes de aceptar la fusión.
>
> Si en casa el repo no está en `C:\Users\<tu usuario>\Proyectos\autospoti`:
> 1. Cierra VS Code y mueve la carpeta ahí.
> 2. Si usabas las tareas de Windows de `scripts\install_tasks.ps1`, vuelve a ejecutarlo después de moverla, porque guardan la ruta.

### Plan para el agente

**Riesgo principal:** romper la web de Vercel o la GitHub Action que registra las escuchas cada hora. Por eso todo se hace en una rama y se comprueba antes de fusionar.

**0. Preparación**
- Crea la rama `reorganizacion`. Nunca trabajes en `main`.
- Ejecuta `python -m unittest discover -s tests` y apunta cuántas pruebas pasan. Al final tienen que pasar las mismas.

**1. Mover archivos con `git mv`** (para conservar el historial)

| Ahora (raíz) | Después |
|---|---|
| `config.py`, `spotify_core.py`, `db.py`, `history.py`, `history_import.py`, `dateranges.py`, `smart.py`, `stats.py`, `charts.py`, `blend.py`, `jobs.py`, `tokens.py`, `web_auth.py` | `autospoti/` (paquete nuevo, con `__init__.py`) |
| `cron.py`, `auth.py`, `import_history.py` | `autospoti/cron.py`, `autospoti/auth.py`, `autospoti/import_history.py`. Se ejecutan con `python -m autospoti.cron`, etc. |
| `autoplaylist.py` (Streamlit antiguo) | **Pregunta a Pol si aún lo usa.** Si sí, va a `streamlit_antiguo/autoplaylist.py` (`.streamlit/` se queda en la raíz). Si no, se borra junto con `.streamlit/` y `Abrir Autospoti.bat`. |
| `tests/smoke_check.py`, `tests/scan_secrets.py` | `scripts/` |
| `webapp/`, `api/index.py`, `vercel.json`, `tests/` | Se quedan donde están. `api/index.py` lo exige Vercel. |

**2. Imports**
- **Dentro del paquete:** imports relativos (`from . import db`, `from .config import ...`).
- **En `webapp/`:** `from autospoti import blend, smart, ...` y `from autospoti.config import ...`.
- **En las pruebas:** `from autospoti import db`, etc. Mantén el `sys.path.insert` de la raíz.
- **Cuidado con los `mock.patch("jobs.write_playlist")` de las pruebas:** pasan a ser `mock.patch("autospoti.jobs.write_playlist")`. Si no se cambian, las pruebas fallan. Busca todos los `patch("` y todas las cadenas con nombres de módulo.
- **La prueba que importa todos los módulos** (`import auth, blend, charts, ...`) hay que actualizarla.
- **Si algún módulo lee archivos por ruta relativa a sí mismo** (`Path(__file__).parent`, por ejemplo hacia `data/` o `.env`), ahora está un nivel más abajo: corrige la ruta.

**3. Referencias fuera del código**
- **`.github/workflows/update.yml`:** `python cron.py ...` pasa a `python -m autospoti.cron ...`.
- **`scripts/install_tasks.ps1`:** `-Argument "cron.py --record"` pasa a `-Argument "-m autospoti.cron --record"`, y lo mismo para la diaria.
- **`Abrir Autospoti.bat`:** la ruta de `autoplaylist.py`, si se conserva.
- **`.vercelignore`:** añade `streamlit_antiguo/`, si existe.
- **README y la sección "Comandos" de este archivo:** todos los comandos (`python auth.py` pasa a `python -m autospoti.auth`, etc.).
- **`.clineignore`:** no cambia.

**4. Reglas de `.clinerules/spotify-api.md`**
- Pasa su contenido a una sección "Al tocar la API de Spotify" de este AGENTS.md (estándar v2: sin archivos de reglas de una herramienta concreta).
- Borra `.clinerules/`.

**5. Comprobar antes de fusionar**
1. Pruebas: pasan las mismas que en el paso 0.
2. Web en local: `uvicorn webapp.main:create_app --factory`. Abre http://127.0.0.1:8000 y recorre Estadísticas, Playlist personalizada y Mis recetas.
3. Registro en local, si hay `.env`: `python -m autospoti.cron --record` termina sin errores.
4. Sube la rama (Sync / Publish Branch). Vercel crea un **despliegue de prueba (Preview)**. Comprueba que la página carga. El login puede fallar en la Preview porque su URL no está en los Redirect URIs de Spotify: no pasa nada.
5. En GitHub, ve a **Actions → "Actualizar playlists automáticas" → Run workflow**, elige la rama `reorganizacion` y el modo `registro`. Tiene que terminar en verde.
6. Solo entonces: Pull Request a `main` y fusionar.
7. Después de fusionar: comprueba que la siguiente ejecución horaria de la Action sale en verde y que la web real funciona.

**6. Cerrar**
- Actualiza la sección "Estructura" de este AGENTS.md y el README.
- Crea `CHANGELOG.md`, si aún no existe, con la entrada de la reorganización.
- Borra este Pendiente 1.

## Pendiente 2 · Resto del estándar (independiente del anterior)

Es una app web en la nube: aplica la parte "Apps web o en la nube" del punto 3 del estándar.

- **README:** reorganizarlo con la plantilla del estándar (punto 4).
  - "Antes de empezar": cuentas de Spotify Developer, Neon, GitHub y Vercel.
  - Cada servicio en su propio paso, con los botones exactos.
  - **Tabla de variables y secretos**: nombre, para qué sirve, de dónde sale y dónde se pone (GitHub Secrets o Vercel).
  - Comprobación final, "Actualizar", "Problemas frecuentes" (incluido el aviso de los 60 días de GitHub Actions) y capturas en `docs/capturas/`.
- **`CHANGELOG.md`:** crearlo, empezando por la versión actual.
- **Uso local:** un `Instalar.bat` que instale `requirements-local.txt` y cree un acceso directo para abrir la web local.
- **`.gitattributes`:** `*.bat` y `*.ps1` con saltos de línea CRLF.
