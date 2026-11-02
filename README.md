# Autospoti

Aplicacion Streamlit para crear y mantener playlists automaticas de Spotify.

## Abrir la aplicacion

En Windows, haz doble clic en [Abrir Autospoti.bat](./Abrir%20Autospoti.bat). El script inicia Streamlit y abre la aplicacion en el navegador.

La primera vez, instala las dependencias y autoriza Spotify:

```powershell
python -m pip install -r requirements.txt
python auth.py
```

La configuracion local se guarda en `.env`; no subas ese archivo ni `.cache`.

## Actualizacion diaria con GitHub Actions

El workflow [`.github/workflows/update.yml`](./.github/workflows/update.yml) ejecuta `python cron.py --api-only` cada dia a las 04:00 UTC. Actualiza las playlists basadas en la API de Spotify (ultimas reproducciones, aproximadamente 4 semanas y aproximadamente 6 meses). Las playlists que necesitan el historial SQLite local se siguen actualizando desde el ordenador.

En **Settings > Secrets and variables > Actions** del repositorio, crea estos secrets:

* `SPOTIPY_CLIENT_ID`: Client ID de tu aplicacion de Spotify.
* `SPOTIPY_CLIENT_SECRET`: Client Secret de tu aplicacion de Spotify.
* `SPOTIFY_CACHE`: contenido JSON de tu archivo `.cache`, generado despues de ejecutar `python auth.py`.

El workflow tambien se puede lanzar manualmente desde la pestana **Actions** con **Run workflow**.