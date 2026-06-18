# Nozomi Music

Aplicacion web en Flask para trabajar con Spotify desde una interfaz propia: iniciar sesion, crear playlists desde TXT, exportarlas, analizarlas, explorar recomendaciones y consultar un dashboard con cache persistente y monitorizacion por Discord.

## Que hace la app

- Login OAuth con Spotify.
- Creacion de playlists privadas a partir de un archivo `.txt`.
- Exportacion de playlists propias o colaborativas a `.txt`.
- Biblioteca personal con canciones guardadas, albumes guardados, artistas seguidos y exportacion a `.txt`.
- Analisis de playlists con sugerencias para anadir, quitar y reordenar canciones.
- Descubrimiento musical por artista y recomendaciones relacionadas.
- Dashboard con top tracks, top artists y actividad reciente.
- Cache persistente para reducir llamadas a Spotify.
- Proteccion automatica frente a `429 Too Many Requests`.
- Observabilidad por Discord Webhook con metricas agregadas.

## Stack

- Python 3
- Flask
- SQLAlchemy
- SQLite en local / PostgreSQL en Railway
- Spotify Web API
- Discord Webhooks

## Estructura principal

- `app.py`: rutas Flask, configuracion, OAuth, protecciones y renderizado.
- `models.py`: modelos SQLAlchemy.
- `services/spotify_client.py`: cliente central para Spotify.
- `services/stats_service.py`: construccion del dashboard.
- `services/spotify_api_cache_service.py`: cache persistente de respuestas.
- `services/playlist_manager.py`: import/export de playlists.
- `services/playlist_enhancer.py`: analisis musical de playlists.
- `services/recommender.py`: descubrimiento musical.
- `services/discord_monitoring.py`: monitorizacion y alertas a Discord.
- `templates/`: interfaz HTML.
- `static/`: CSS e imagenes.

## Funcionalidades

### 1. Login con Spotify

La app usa OAuth para conectar una cuenta de Spotify.

Flujo:

1. El usuario entra en `/login`.
2. Spotify devuelve el `code` a `/callback`.
3. La app guarda en sesion y base de datos:
   - `access_token`
   - `refresh_token`
   - `expires_at`
   - datos del usuario Spotify
4. Si el token expira, la app intenta renovarlo automaticamente.

Scopes usados:

- `playlist-modify-public`
- `playlist-modify-private`
- `playlist-read-private`
- `playlist-read-collaborative`
- `user-library-read`
- `user-follow-read`
- `user-top-read`
- `user-read-recently-played`
- `user-read-private`

### 2. Crear playlists desde TXT

Ruta: `/create-playlist`

Permite subir un archivo `.txt` con una cancion por linea usando este formato:

```txt
Bohemian Rhapsody - Queen
Time - Pink Floyd
Instant Crush - Daft Punk
```

Funcionamiento:

- valida el formato linea por linea
- busca coincidencias en Spotify
- usa cache persistente para no repetir busquedas iguales
- crea una playlist privada nueva
- anade los temas encontrados en bloques compatibles con Spotify
- informa de:
  - lineas invalidas
  - canciones no encontradas
  - canciones anadidas

### 3. Exportar playlists a TXT

Ruta: `/export-playlist`

Permite:

- listar playlists propias
- listar playlists colaborativas
- filtrar por nombre
- exportar el contenido a `exports/<nombre>.txt`

Modo de trabajo:

- `normal`: usa cache y consulta Spotify si faltan datos
- `cache_only`: solo usa datos ya cacheados

El TXT exportado queda numerado, listo para descargar o reutilizar.

### 4. Mejorador de playlists

Ruta: `/playlist-enhancer`

Analiza una playlist y genera un reporte con:

- numero total de tracks
- popularidad media
- artistas dominantes
- generos mas frecuentes
- duplicados
- canciones recomendadas para anadir
- canciones candidatas para quitar
- sugerencia de mejor orden

Para ello combina:

- tracks de la playlist
- lookup de artistas
- cache de artistas y generos
- recomendaciones y busquedas de Spotify

Si Spotify limita llamadas o un endpoint falla, la app intenta apoyarse en cache para no vaciar la experiencia.

### 5. Biblioteca personal

Ruta: `/personal-library`

Permite:

- cargar todas tus `Liked Songs`
- cargar albumes guardados
- cargar artistas seguidos
- cambiar entre secciones desde la misma vista
- filtrar por texto segun la seccion activa
- reutilizar cache si Spotify no esta disponible
- exportar el subconjunto visible a TXT

La exportacion sigue el mismo formato simple de la app:

```txt
1. Nights - Frank Ocean
2. Jigsaw Falling Into Place - Radiohead
```

### 6. Descubrimiento musical

Ruta: `/recommendations`

Permite buscar un artista base y obtener:

- artistas similares
- canciones relacionadas
- resultados alternativos si el endpoint principal no responde como se espera

La idea es explorar musica nueva a partir de una referencia conocida, con un fallback apoyado en generos del artista cuando Spotify se pone restrictivo.

### 7. Prompt generator

Ruta: `/prompt-generator`

Genera prompts musicales listos para usar en herramientas de IA externas.

Se pueden ajustar campos como:

- tipo de prompt
- genero
- mood
- referencias
- objetivo
- restricciones
- idioma de salida

La app no integra la IA; solo produce el prompt final.

### 8. Dashboard musical

Rutas:

- `/dashboard`
- `/dashboard/top/<item_type>`
- `/dashboard/export`

Muestra:

- perfil del usuario Spotify
- top tracks por rango temporal
- top artists por rango temporal
- actividad reciente
- resumen rapido de volumen de datos

Rangos temporales:

- `short_term`: ultimas 4 semanas
- `medium_term`: ultimos 6 meses
- `long_term`: ultimo ano

El dashboard usa snapshot persistente en cache para no recalcularlo continuamente.

Tambien permite:

- forzar refresco
- ver top 50 completos
- exportar un HTML con el reporte

## Cache de la aplicacion

La app utiliza cache persistente en base de datos para reducir llamadas a Spotify.

### Que se cachea

- snapshots del dashboard
- playlists exportables del usuario
- tracks de playlists
- busquedas de canciones para importacion
- respuestas de Spotify API por clave logica
- artistas, generos y popularidad

### Donde se guarda

En tablas SQLAlchemy, principalmente:

- `spotify_api_cache`
- `artist_genres_cache`
- `dashboard_top_snapshot`

### Modos de cache

Desde `/profile` se puede escoger:

- `cache_only`: no hace llamadas nuevas a Spotify en exportar y mejorar
- `normal`: usa cache primero y consulta Spotify cuando falta informacion

## Proteccion anti-rate-limit

La app implementa proteccion persistente por usuario para evitar cadenas de `429`.

### Bloqueo tras 429

Cuando Spotify devuelve `429`:

- se lee `Retry-After` si existe
- se guarda `rate_limited_until` en base de datos
- si `Retry-After` es menor de 1 hora, igualmente se bloquea 1 hora
- si `Retry-After` es mayor de 1 hora, se usa el valor mayor

Mientras el bloqueo esta activo:

- no se hace ninguna llamada real a Spotify
- no se consume cuota interna de refresh
- no se intenta refrescar con Spotify
- solo se permite usar cache
- la UI muestra mensaje visible y contador restante

Mensaje mostrado al usuario:

```txt
Spotify ha limitado temporalmente las solicitudes para esta cuenta. Puedes seguir utilizando los datos en cache. Proximo intento permitido: {fecha y hora}.
```

### Forced Cache Mode

Si un usuario acumula `2 o mas 429` en menos de 24 horas:

- se activa automaticamente `forced_cache_until`
- duracion: 24 horas
- toda la app pasa a usar solo cache para ese usuario
- se deshabilitan botones de refresco y acciones que generen llamadas nuevas a Spotify

Mensaje mostrado al usuario:

```txt
Se ha activado el modo cache temporal para proteger la aplicacion frente a los limites de Spotify. Podras volver a realizar actualizaciones el {fecha y hora}.
```

Cuando expira `forced_cache_until`, el funcionamiento normal vuelve automaticamente.

### Cuota interna de refresh del dashboard

La app limita las recargas forzadas del dashboard por usuario en una ventana de 24 horas.

Variable:

- `DASHBOARD_REFRESH_LIMIT_24H` (por defecto `12`)

Si el usuario supera la cuota:

- no se ejecuta el refresh forzado
- no se invalida el snapshot util existente
- se muestra un aviso con el proximo momento permitido
- se envia alerta agregada a Discord

## Monitorizacion por Discord

La observabilidad esta pensada para eventos agregados e importantes, no para cada request individual.

### Variables

- `USER_RATE_WEBHOOK=`
- `ENABLE_DISCORD_MONITORING=true`

Si `ENABLE_DISCORD_MONITORING=false`, no se envia nada a Discord.

### Eventos enviados

#### 1. Dashboard Refresh Summary

Se envia al terminar una recarga forzada del dashboard.

Incluye:

- usuario
- Spotify User ID
- llamadas reales a Spotify realizadas en esa operacion
- cache hits
- cache misses
- hit rate
- duracion
- timestamp UTC

#### 2. Spotify 429 Alert

Se envia cuando Spotify devuelve `429`.

Incluye:

- usuario
- Spotify User ID
- endpoint
- `Retry-After`
- hora UTC
- llamadas realizadas durante la operacion

#### 3. Spotify User Blocked

Se envia cuando se persiste el bloqueo del usuario.

Incluye:

- usuario
- Spotify User ID
- motivo
- retry-after recibido
- bloqueado hasta
- timestamp UTC

#### 4. User Quota Alert

Se envia cuando el usuario supera la cuota interna de refresh del dashboard.

Incluye:

- usuario
- Spotify User ID
- accion afectada
- refresh usados en 24h
- proximo refresh permitido

#### 5. Forced Cache Mode Activated

Se envia cuando un usuario entra en modo cache obligatoria por multiples `429`.

Incluye:

- usuario
- Spotify User ID
- `429` recibidos en 24h
- fecha de fin del forced cache
- timestamp UTC

#### 6. Daily Summary

Se envia una vez cada 24 horas, al arrancar la app o cuando toca en la primera ejecucion posterior.

Incluye:

- usuarios activos
- Spotify calls totales
- cache hits
- cache misses
- hit rate global
- `429` recibidos
- usuarios bloqueados

### Seguridad de la monitorizacion

Nunca se envian al webhook:

- `access_token`
- `refresh_token`
- `client_secret`
- `database_url`

La app mantiene ademas logs normales en local/Railway.

## Base de datos

Modelos principales:

- `SpotifyUser`: datos del usuario, tokens, expiracion y protecciones (`rate_limited_until`, `forced_cache_until`).
- `ArtistGenresCache`: cache de artistas, generos y popularidad.
- `SpotifyApiCache`: cache generica de respuestas.
- `DashboardTopSnapshot`: snapshots persistidos de tops del dashboard.
- `SpotifyMonitoringEvent`: eventos de observabilidad y metricas agregadas.
- `AppRuntimeState`: estado simple de runtime, como el ultimo daily summary enviado.

## Variables de entorno

Ejemplo minimo:

```env
FLASK_SECRET_KEY=change-me-in-production
DATABASE_URL=
SPOTIFY_CLIENT_ID=
SPOTIFY_CLIENT_SECRET=
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/callback
USER_REQUEST_WEBHOOK=
USER_RATE_WEBHOOK=
ENABLE_DISCORD_MONITORING=true
DASHBOARD_REFRESH_LIMIT_24H=12
```

### Significado

- `FLASK_SECRET_KEY`: clave de sesion Flask.
- `DATABASE_URL`: URL de SQLite o PostgreSQL.
- `SPOTIFY_CLIENT_ID`: client id de Spotify Developers.
- `SPOTIFY_CLIENT_SECRET`: client secret de Spotify Developers.
- `SPOTIFY_REDIRECT_URI`: callback OAuth registrado en Spotify.
- `USER_REQUEST_WEBHOOK`: webhook para solicitudes manuales desde perfil.
- `USER_RATE_WEBHOOK`: webhook de observabilidad y alertas.
- `ENABLE_DISCORD_MONITORING`: activa o desactiva el envio a Discord.
- `DASHBOARD_REFRESH_LIMIT_24H`: limite de refresh forzado por usuario cada 24h.

## Ejecucion local

### 1. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 2. Crear `.env`

Usa `.env.example` como base.

### 3. Configurar Spotify Developers

En tu app de Spotify debes registrar exactamente la redirect URI correcta, por ejemplo:

```txt
http://127.0.0.1:8888/callback
```

### 4. Arrancar la app

```bash
python app.py
```

La app corre por defecto en:

```txt
http://127.0.0.1:8888
```

## Despliegue en Railway

Puntos importantes:

- configurar todas las variables de entorno en Railway
- usar `DATABASE_URL` de PostgreSQL si quieres persistencia real en produccion
- revisar que `SPOTIFY_REDIRECT_URI` apunte a la URL publica del deploy
- si cambias la URL del servicio, actualizar tambien Spotify Developers

## Flujo de uso recomendado

1. Configurar Spotify y variables de entorno.
2. Iniciar sesion con Spotify.
3. Ajustar el modo de cache en `/profile`.
4. Usar crear/exportar/mejorar/dashboard segun necesidad.
5. Revisar Discord para observar refrescos agregados, `429`, bloqueos y resumen diario.

## Limitaciones actuales

- depende de la disponibilidad y permisos de la API de Spotify
- algunas vistas solo pueden trabajar con cache si Spotify ha limitado al usuario
- el resumen diario se dispara desde runtime de la app, no desde un cron externo

## Ideas futuras ya detectadas en el proyecto

- mejorar el descubrimiento musical basado en artista
- biblioteca personal con canciones guardadas y exportacion
- analizador de compatibilidad entre artistas

## Licencia

Uso interno / proyecto personal, salvo que decidas anadir una licencia publica mas adelante.
