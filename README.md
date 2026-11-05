# Autospoti

Aplicacion Streamlit para crear y mantener playlists automaticas de Spotify, con un registro propio de todo lo que escuchas.

## Abrir la aplicacion en local

En Windows, haz doble clic en [Abrir Autospoti.bat](./Abrir%20Autospoti.bat). El script inicia Streamlit y abre la aplicacion en el navegador.

La primera vez, instala las dependencias y autoriza Spotify:

```powershell
python -m pip install -r requirements.txt
python auth.py
```

La configuracion local se guarda en `.env`; no subas ese archivo ni `.cache`. Sin `DATABASE_URL` el historial se guarda en `data/history.db` (SQLite).

## En la nube (gratis): base de datos + registro cada hora

Con una base de datos Postgres en la nube, GitHub Actions registra tus escuchas **cada hora** (sin depender de que tu PC este encendido) y actualiza las 5 playlists cada dia a las 04:00 UTC.

1. Crea una base de datos gratuita en [Neon](https://neon.tech) (o Supabase) y copia la *connection string* (`postgresql://...?sslmode=require`).
2. En **Settings > Secrets and variables > Actions** del repositorio, crea estos secrets:
   * `SPOTIPY_CLIENT_ID` y `SPOTIPY_CLIENT_SECRET`: de tu aplicacion de Spotify.
   * `DATABASE_URL`: la connection string del paso 1.
   * `SPOTIFY_CACHE`: contenido JSON de tu archivo `.cache` (se genera con `python auth.py`). Solo se usa la primera vez: despues el token se guarda **cifrado** en la base de datos y se renueva solo.
   * `TOKEN_ENCRYPTION_KEY` (opcional): clave Fernet propia. Si no existe, se deriva de `SPOTIPY_CLIENT_SECRET`.
3. En la pestana **Actions**, lanza el workflow con **Run workflow** para comprobarlo.

Sin `DATABASE_URL`, el workflow funciona como antes: solo actualiza una vez al dia las 3 playlists que salen de la API de Spotify.

Nota: GitHub desactiva los workflows programados de repos publicos tras 60 dias sin actividad en el repo; si pasa, reactivalo desde la pestana Actions.

## La web (Streamlit Community Cloud, gratis)

Con `DATABASE_URL`, la web pide **iniciar sesion con Spotify**: cada persona entra con su cuenta, su token se guarda cifrado en la base de datos y el registro horario empieza a incluirla automaticamente.

1. En [share.streamlit.io](https://share.streamlit.io) crea una app: repositorio `polreig/autospoti`, rama `main`, archivo `autoplaylist.py`, Python 3.12. Elige la URL (por ejemplo `https://autospoti-pol.streamlit.app`).
2. En **Settings > Secrets** de la app pega (formato TOML):
   ```toml
   SPOTIPY_CLIENT_ID = "..."
   SPOTIPY_CLIENT_SECRET = "..."
   DATABASE_URL = "postgresql://...?sslmode=require"
   WEB_REDIRECT_URI = "https://autospoti-pol.streamlit.app/"
   ALLOWED_SPOTIFY_IDS = "tu_id_de_spotify"
   ```
3. En el [dashboard de Spotify](https://developer.spotify.com/dashboard), en tu app > **Settings > Redirect URIs**, anade exactamente la misma `WEB_REDIRECT_URI`.
4. Abre la web y pulsa **Iniciar sesion con Spotify** (se abre en otra pestana).

Para que entre otra persona: anadela en **User Management** del dashboard de Spotify (Development Mode) y su ID en `ALLOWED_SPOTIFY_IDS`. Desde **Tu cuenta** cada uno puede cerrar sesion u olvidar su cuenta (deja de registrarse lo que escucha).

## Tests

```powershell
python -m unittest discover -s tests
```

Para pasarlos tambien contra Postgres, define `TEST_DATABASE_URL` (se vacian sus tablas).
