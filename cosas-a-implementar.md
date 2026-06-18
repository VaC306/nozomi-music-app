cosas a implementar:

- ver lo de devolver generos en las cosas

- Biblioteca personal: leer Liked Songs, albumes guardados y artistas seguidos; luego exportar, analizar y reutilizar esa libreria.
- Sincronizacion programada: refrescos automáticos suaves por usuario con colas y backoff, para tener cache caliente sin depender siempre del refresh manual.
- Comparador musical: comparar dos playlists, dos artistas o dos usuarios y sacar solapamiento, compatibilidad, huecos y recomendaciones.
- Historial y tendencias: guardar snapshots diarios/semanales para ver cómo cambian top artists, top tracks y estilos con el tiempo.
- Buscador global: una sola vista para buscar tracks, artistas, albums y playlists con filtros y acciones rápidas.
- Exportaciones avanzadas: CSV, JSON y HTML mejorado con métricas, portada, enlaces y resumen ejecutivo.
- Deteccion de duplicados y limpieza: encontrar duplicados exactos/casi duplicados, versiones live/remaster y tracks huérfanos en playlists largas.
- Scoring de playlists: dar una nota a una playlist según coherencia, variedad, energía, repetición y popularidad.
- Modo curator: sugerir automáticamente qué 10 temas abrir, cuáles dejar en el centro y cómo cerrar mejor una playlist.
- Centro de salud del sistema: panel interno con cache hit rate por feature, usuarios bloqueados, endpoints que más 429 generan y tiempos medios.
- Cola de trabajos: mover operaciones pesadas a background jobs para no bloquear requests web.
- Permisos y multiusuario: si luego la usas con más gente, separar usuarios, límites, auditoría y preferencias por cuenta.
- Onboarding técnico: wizard en /profile para validar redirect URI, scopes, sesión, webhook y estado de base de datos.
- Tests automáticos: sobre todo para protección 429, cuota interna, cache-only y degradación elegante.
- PRD de producto: definir qué es “Nozomi Music” a largo plazo: utilidad de curación, analítica personal o herramienta operativa para Spotify.
Si tuviera que priorizar, haría esto:
1. Biblioteca personal
2. Historial y tendencias
3. Comparador musical
4. Cola de trabajos + sincronización programada
5. Centro de salud del sistema