# Autospoti

Web para crear y mantener playlists de Spotify a partir de un registro propio de todo lo que escuchas.

* **Estadisticas**: escuchas y horas, evolucion por mes, artistas y canciones mas escuchados y un mapa de cuando escuchas (dia x hora).
* **Playlist personalizada**: por periodo (ultimos N dias, un mes, fechas o todo) con filtros por meses, dias de la semana, franja horaria, artistas, escuchas minimas y "evitar las que sueles saltar".
* **Redescubrimientos**: canciones que escuchaste mucho y llevas meses sin poner.
* **En pareja**: mezclas con otra cuenta enlazada (lo que escuchais los dos, o mitad y mitad). Requiere que las dos personas acepten.
* **Mis recetas**: cualquier playlist anterior puede guardarse para que se recalcule sola cada dia.
* **Automaticas** (cada dia): ultimas reproducciones (50 canciones), mas escuchado en 7 dias (50), 4 semanas (100), 6 meses (100), ultimo ano (100) y **Always**, tus 250 canciones mas escuchadas de siempre.
* **Mensuales**: el dia 1 de cada mes se crea sola la playlist del mes que acaba de terminar con tus 100 canciones mas escuchadas, con nombres como *September 25'*. Las de meses anteriores que te falten se crean desde **Mensuales** en la web (ver abajo).

## Como funciona en la nube (todo gratis)

| Pieza | Servicio | Que hace |
|---|---|---|
| Base de datos | [Neon](https://neon.tech) (Postgres) | historial, recetas, tokens cifrados |
| Registro y recetas | GitHub Actions ([update.yml](./.github/workflows/update.yml)) | cada hora registra escuchas; cada dia actualiza playlists y recetas |
| Web | [Vercel](https://vercel.com) (FastAPI) | login con Spotify, estadisticas, crear playlists |

## Puesta en marcha

### 1. Base de datos (Neon)
Crea un proyecto gratuito y copia la *connection string* (`postgresql://...?sslmode=require`). Sirve tanto la directa como la *pooled*.

### 2. App de Spotify
En el [dashboard de Spotify](https://developer.spotify.com/dashboard), en tu app > **Settings**:
* Copia *Client ID* y *Client secret*.
* En **Redirect URIs** anade `https://TU-PROYECTO.vercel.app/callback` (la URL exacta la tendras en el paso 4; puedes volver a anadirla despues).

### 3. GitHub Actions (registro cada hora)
En el repo, **Settings > Secrets and variables > Actions**, secrets:
* `SPOTIPY_CLIENT_ID`, `SPOTIPY_CLIENT_SECRET`
* `DATABASE_URL`: la de Neon
* `SPOTIFY_CACHE` *(opcional)*: el contenido de un `.cache` generado con `python auth.py`. Solo sirve para que el registro empiece antes de tu primer login en la web; si no lo pones, empieza en cuanto entres en la web.

### 4. Web (Vercel)
1. En Vercel: **Add New > Project**, importa `polreig/autospoti`. *Framework preset*: Other. No hace falta tocar build ni output.
2. En **Settings > Environment Variables** anade:
   * `SPOTIPY_CLIENT_ID`, `SPOTIPY_CLIENT_SECRET`, `DATABASE_URL`
   * `WEB_REDIRECT_URI` = `https://TU-PROYECTO.vercel.app/callback`
   * `ALLOWED_SPOTIFY_IDS` = tu ID de Spotify (y el de tu pareja, separados por comas)
   * `APP_TIMEZONE` = `Europe/Madrid` (opcional)
3. **Deploy**. Abre la URL, pulsa *Iniciar sesion con Spotify* y, ya dentro, **Importar historial** para subir tus `Streaming_History_Audio_*.json`.

Tu ID de Spotify aparece en **Tu cuenta** despues de entrar (o en spotify.com > Cuenta > *Nombre de usuario*).

### 5. Tu pareja
1. Dashboard de Spotify > tu app > **User Management**: anade su nombre y el email de su cuenta de Spotify (Development Mode admite pocas cuentas; para dos sobra).
2. Anade su ID a `ALLOWED_SPOTIFY_IDS` en Vercel y vuelve a desplegar (*Redeploy*).
3. Que entre en la web con su cuenta. Desde **En pareja**, uno invita al otro por su ID y el otro acepta.

## Playlists mensuales de meses anteriores

Hazlo una vez, despues de importar tu historial:

1. Abre la web y entra en **Mensuales** (menu de la izquierda, en *Crear playlists*).
2. Revisa la tabla: cada mes de tu historial sale como **Ya la tienes**, **Se creara** o **Muy pocas escuchas**. Se reconocen tambien tus playlists con nombres parecidos (*Sept 25*, *September 2025*, *Septiembre 25'*), asi que no se duplican.
3. Pulsa **Crear las ... que faltan** y acepta el aviso. Se crean por tandas de 10: si faltan mas, pulsa otra vez hasta que ponga **No falta ninguna**.

Alternativa sin la web: en GitHub, **Actions > Actualizar playlists automaticas > Run workflow**, elige el modo `meses` y pulsa **Run workflow**. Crea todas las que falten de una vez.

Las playlists nuevas se crean privadas. Si borras una que creo autospoti, no se vuelve a crear.

## Uso en tu PC (opcional)

```powershell
python -m pip install -r requirements-local.txt
uvicorn webapp.main:create_app --factory --reload     # la web en http://127.0.0.1:8000
```
Para la web en local anade `http://127.0.0.1:8000/callback` en los Redirect URIs de Spotify. Sin `DATABASE_URL` usa SQLite (`data/history.db`).

La app Streamlit antigua sigue disponible para uso local de una sola cuenta: `python auth.py` y `streamlit run autoplaylist.py` (o *Abrir Autospoti.bat*).

## Tests

```powershell
python -m pip install -r requirements-local.txt
python -m unittest discover -s tests
```
Para pasarlos tambien contra Postgres, define `TEST_DATABASE_URL` (se vacian sus tablas).

Nota: GitHub desactiva los workflows programados de repos publicos tras 60 dias sin actividad en el repo; si pasa, reactivalo desde la pestana Actions.
