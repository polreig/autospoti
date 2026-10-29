---
paths:
  - "spotify_core.py"
  - "jobs.py"
  - "cron.py"
  - "auth.py"
---

# Reglas al tocar código de la API de Spotify

- Spotify cambió la API en febrero de 2026: playlists usan `/items` (no `/tracks`) y el campo `tracks` pasó a `items`.
- Con apps en Development Mode el dueño necesita Premium y los usuarios deben estar añadidos en el dashboard (error 403 si no).
- No reautorices dentro de `cron.py`: si falta el token, falla con un mensaje que pide `python auth.py`.
- Máximo 100 URIs por petición de playlist y 300 caracteres de descripción.
- Paginar siempre `current_user_playlists` (máx. 50 por página) y comprobar que el dueño es el usuario.
- Cualquier cambio de SCOPE obliga a repetir `python auth.py`.
