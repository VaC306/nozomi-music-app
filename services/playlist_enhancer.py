from __future__ import annotations

from collections import Counter
from statistics import mean
from typing import Any

from services.artist_cache_service import ArtistCacheService
from services.spotify_api_cache_service import SpotifyApiCacheService
from services.playlist_manager import normalize_text
from services.spotify_client import SpotifyClient, SpotifyClientError, SpotifyRateLimitError


class PlaylistEnhancer:
    REPORT_TTL_SECONDS = 1800
    PLAYLIST_TRACKS_TTL_SECONDS = 1800

    def __init__(self, spotify_client: SpotifyClient, user_id: str) -> None:
        self.spotify_client = spotify_client
        self.artist_cache_service = ArtistCacheService(spotify_client)
        self.cache_service = SpotifyApiCacheService(cache_scope=f"user:{user_id or 'anonymous'}")

    def build_playlist_report(self, access_token: str, playlist: dict[str, Any]) -> dict[str, Any]:
        return self.build_playlist_report_with_mode(access_token, playlist)

    def build_playlist_report_with_mode(
        self,
        access_token: str,
        playlist: dict[str, Any],
        cache_only: bool = False,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        playlist_id = str(playlist.get("id", "")).strip()
        if not playlist_id:
            raise SpotifyClientError("La playlist seleccionada no es valida.")

        try:
            return self.cache_service.get_or_set(
                cache_key=f"playlist-enhancer-report:v2:{playlist_id}",
                ttl_seconds=self.REPORT_TTL_SECONDS,
                source_endpoint="playlist_enhancer_report",
                fetcher=lambda: self._build_playlist_report_uncached(
                    access_token,
                    playlist,
                    playlist_id,
                    cache_only,
                    force_refresh,
                ),
                force_refresh=force_refresh,
            )
        finally:
            self.artist_cache_service.log_metrics("playlist-enhancer")

    def _build_playlist_report_uncached(
        self,
        access_token: str,
        playlist: dict[str, Any],
        playlist_id: str,
        cache_only: bool,
        force_refresh: bool,
    ) -> dict[str, Any]:
        cache_key = f"playlist-tracks:v1:{playlist_id}"
        playlist_tracks = None if force_refresh else self.cache_service.get(cache_key, allow_stale=True)
        if playlist_tracks is None and cache_only:
            raise SpotifyClientError(
                "No hay una copia en cache de esa playlist todavia. Desactiva 'solo cache' o fuerza un refresco cuando Spotify lo permita."
            )
        if playlist_tracks is None:
            playlist_tracks = self.cache_service.get_or_set(
                cache_key=cache_key,
                ttl_seconds=self.PLAYLIST_TRACKS_TTL_SECONDS,
                source_endpoint="get_playlist_tracks",
                fetcher=lambda: self.spotify_client.get_playlist_tracks(access_token, playlist_id),
                force_refresh=force_refresh,
            )
        tracks = self._normalize_playlist_tracks(access_token, playlist_tracks)
        if not tracks:
            raise SpotifyClientError("La playlist no tiene canciones analizables.")

        artist_counter = Counter(track["primary_artist"] for track in tracks if track["primary_artist"])
        genre_counter = Counter()
        for track in tracks:
            for genre in track["genres"]:
                genre_counter[genre] += 1

        average_popularity = round(mean(track["popularity"] for track in tracks)) if tracks else 0
        duplicate_groups = self._build_duplicate_groups(tracks)
        stats = self._build_stats(tracks, artist_counter, genre_counter, average_popularity, duplicate_groups)
        score = self._build_playlist_score(
            tracks,
            artist_counter,
            genre_counter,
            average_popularity,
            duplicate_groups,
        )

        return {
            "playlist": {
                "id": playlist_id,
                "name": playlist.get("name", "Playlist"),
                "owner_name": playlist.get("owner_name") or playlist.get("owner", {}).get("display_name") or "Spotify",
                "track_total": len(tracks),
                "spotify_url": playlist.get("external_urls", {}).get("spotify", ""),
            },
            "stats": stats,
            "score": score,
            "recommendations_to_add": self._build_add_recommendations(
                access_token,
                tracks,
                artist_counter,
                genre_counter,
                average_popularity,
            ),
            "recommendations_to_remove": self._build_remove_recommendations(
                tracks,
                artist_counter,
                genre_counter,
                average_popularity,
                duplicate_groups,
            ),
            "ordering": self._build_ordering_recommendation(tracks, artist_counter, genre_counter),
        }

    def _normalize_playlist_tracks(self, access_token: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        raw_tracks: list[dict[str, Any]] = []
        artist_ids: list[str] = []

        for item in items:
            track = item.get("track") or item.get("item") or {}
            if not track or track.get("type") == "episode" or track.get("is_local"):
                continue
            if not track.get("id") or not track.get("uri"):
                continue
            raw_tracks.append(track)
            for artist in track.get("artists", []):
                artist_id = str(artist.get("id", "")).strip()
                if artist_id and artist_id not in artist_ids:
                    artist_ids.append(artist_id)

        artists_lookup = self.artist_cache_service.get_artists_lookup(access_token, artist_ids)
        normalized_tracks: list[dict[str, Any]] = []

        for index, track in enumerate(raw_tracks, start=1):
            artists = track.get("artists", []) or []
            primary_artist = artists[0].get("name", "") if artists else ""
            primary_artist_id = artists[0].get("id", "") if artists else ""
            genres = self._build_track_genres(artists, artists_lookup)
            estimated_energy = self._estimate_track_energy(track, genres)
            normalized_tracks.append(
                {
                    "position": index,
                    "id": track.get("id", ""),
                    "uri": track.get("uri", ""),
                    "name": track.get("name", "Cancion sin titulo"),
                    "artist_names": ", ".join(artist.get("name", "") for artist in artists),
                    "primary_artist": primary_artist,
                    "primary_artist_id": primary_artist_id,
                    "artist_ids": [artist.get("id", "") for artist in artists if artist.get("id")],
                    "popularity": int(track.get("popularity", 0) or 0),
                    "duration_ms": int(track.get("duration_ms", 0) or 0),
                    "album": (track.get("album") or {}).get("name", ""),
                    "image": self._extract_image(track),
                    "spotify_url": track.get("external_urls", {}).get("spotify", ""),
                    "genres": genres,
                    "primary_genre": genres[0] if genres else "Sin genero",
                    "estimated_energy": estimated_energy,
                    "search_key": normalize_text(
                        f"{track.get('name', '')} {' '.join(artist.get('name', '') for artist in artists)}"
                    ),
                }
            )

        return normalized_tracks

    def _build_stats(
        self,
        tracks: list[dict[str, Any]],
        artist_counter: Counter[str],
        genre_counter: Counter[str],
        average_popularity: int,
        duplicate_groups: list[dict[str, Any]],
    ) -> dict[str, Any]:
        high_popularity = sum(1 for track in tracks if track["popularity"] >= 70)
        medium_popularity = sum(1 for track in tracks if 40 <= track["popularity"] < 70)
        low_popularity = sum(1 for track in tracks if track["popularity"] < 40)
        total_duration_ms = sum(track["duration_ms"] for track in tracks)
        top_artists = [
            {"name": name, "count": count}
            for name, count in artist_counter.most_common(5)
            if name
        ]
        top_genres = [
            {"name": name, "count": count}
            for name, count in genre_counter.most_common(6)
            if name
        ]

        return {
            "track_count": len(tracks),
            "unique_artist_count": len([artist for artist in artist_counter if artist]),
            "unique_genre_count": len([genre for genre in genre_counter if genre]),
            "average_popularity": average_popularity,
            "high_popularity_count": high_popularity,
            "medium_popularity_count": medium_popularity,
            "low_popularity_count": low_popularity,
            "duplicate_count": sum(max(group["count"] - 1, 0) for group in duplicate_groups),
            "duration_label": self._format_duration(total_duration_ms),
            "top_artists": top_artists,
            "top_genres": top_genres,
        }

    def _build_playlist_score(
        self,
        tracks: list[dict[str, Any]],
        artist_counter: Counter[str],
        genre_counter: Counter[str],
        average_popularity: int,
        duplicate_groups: list[dict[str, Any]],
    ) -> dict[str, Any]:
        weights = {
            "coherence": 0.30,
            "variety": 0.20,
            "energy": 0.20,
            "repetition": 0.15,
            "popularity": 0.15,
        }

        coherence_value = self._score_coherence(tracks, artist_counter, genre_counter)
        variety_value = self._score_variety(tracks, artist_counter, genre_counter)
        energy_value = self._score_energy(tracks)
        repetition_value = self._score_repetition(tracks, artist_counter, duplicate_groups)
        popularity_value = self._score_popularity(tracks, average_popularity)

        overall = round(
            coherence_value * weights["coherence"]
            + variety_value * weights["variety"]
            + energy_value * weights["energy"]
            + repetition_value * weights["repetition"]
            + popularity_value * weights["popularity"]
        )

        components = {
            "coherence": {
                "label": "Coherencia",
                "value": coherence_value,
                "weight": weights["coherence"],
                "reason": self._build_coherence_reason(coherence_value, tracks, genre_counter),
            },
            "variety": {
                "label": "Variedad",
                "value": variety_value,
                "weight": weights["variety"],
                "reason": self._build_variety_reason(variety_value, tracks, artist_counter, genre_counter),
            },
            "energy": {
                "label": "Energia",
                "value": energy_value,
                "weight": weights["energy"],
                "reason": self._build_energy_reason(energy_value, tracks),
            },
            "repetition": {
                "label": "Repeticion",
                "value": repetition_value,
                "weight": weights["repetition"],
                "reason": self._build_repetition_reason(repetition_value, tracks, artist_counter, duplicate_groups),
            },
            "popularity": {
                "label": "Popularidad",
                "value": popularity_value,
                "weight": weights["popularity"],
                "reason": self._build_popularity_reason(popularity_value, average_popularity, tracks),
            },
        }

        return {
            "overall": overall,
            "label": self._build_score_label(overall),
            "summary": self._build_score_summary(overall, components),
            "components": components,
            "highlights": self._build_score_highlights(components),
            "warnings": self._build_score_warnings(components),
        }

    def _score_coherence(
        self,
        tracks: list[dict[str, Any]],
        artist_counter: Counter[str],
        genre_counter: Counter[str],
    ) -> int:
        if not tracks:
            return 0

        top_genre_total = sum(count for _genre, count in genre_counter.most_common(3))
        total_genre_tags = sum(genre_counter.values()) or len(tracks)
        genre_focus = min(top_genre_total / total_genre_tags, 1.0)

        transition_scores = [
            self._transition_score(previous, current, artist_counter, genre_counter)
            for previous, current in zip(tracks, tracks[1:])
        ]
        average_transition = mean(transition_scores) if transition_scores else 20
        normalized_transition = self._clamp((average_transition / 40) * 100)

        dominant_artist_share = max(artist_counter.values(), default=0) / len(tracks)
        artist_balance = 100 - self._clamp((dominant_artist_share - 0.35) * 180)

        score = normalized_transition * 0.4 + genre_focus * 100 * 0.35 + artist_balance * 0.25
        return self._clamp(score)

    def _score_variety(
        self,
        tracks: list[dict[str, Any]],
        artist_counter: Counter[str],
        genre_counter: Counter[str],
    ) -> int:
        track_count = len(tracks)
        if track_count == 0:
            return 0

        unique_artist_ratio = len(artist_counter) / track_count
        unique_genre_ratio = len(genre_counter) / track_count if genre_counter else 0.15
        artist_variety = self._score_ratio_in_band(unique_artist_ratio, 0.55, 0.9)
        genre_variety = self._score_ratio_in_band(unique_genre_ratio, 0.12, 0.35)

        dominant_artist_share = max(artist_counter.values(), default=0) / track_count
        concentration_penalty = self._clamp(max(dominant_artist_share - 0.22, 0) * 120)

        score = artist_variety * 0.5 + genre_variety * 0.35 + (100 - concentration_penalty) * 0.15
        return self._clamp(score)

    def _score_energy(self, tracks: list[dict[str, Any]]) -> int:
        if not tracks:
            return 0

        energy_values = [track["estimated_energy"] for track in tracks]
        average_energy = mean(energy_values)
        smoothness_gaps = [abs(previous - current) for previous, current in zip(energy_values, energy_values[1:])]
        average_gap = mean(smoothness_gaps) if smoothness_gaps else 8
        smoothness_score = 100 - self._clamp(average_gap * 3)

        score = average_energy * 0.55 + smoothness_score * 0.45
        return self._clamp(score)

    def _score_repetition(
        self,
        tracks: list[dict[str, Any]],
        artist_counter: Counter[str],
        duplicate_groups: list[dict[str, Any]],
    ) -> int:
        track_count = len(tracks) or 1
        duplicate_penalty = sum(max(group["count"] - 1, 0) for group in duplicate_groups) * 18
        dominant_artist_share = max(artist_counter.values(), default=0) / track_count
        artist_penalty = self._clamp(max(dominant_artist_share - 0.28, 0) * 110)
        repeated_album_penalty = self._build_album_concentration_penalty(tracks)
        score = 100 - duplicate_penalty - artist_penalty - repeated_album_penalty
        return self._clamp(score)

    def _score_popularity(self, tracks: list[dict[str, Any]], average_popularity: int) -> int:
        if not tracks:
            return 0

        gaps = [abs(track["popularity"] - average_popularity) for track in tracks]
        consistency = 100 - self._clamp((mean(gaps) if gaps else 0) * 2)
        score = average_popularity * 0.7 + consistency * 0.3
        return self._clamp(score)

    def _build_add_recommendations(
        self,
        access_token: str,
        tracks: list[dict[str, Any]],
        artist_counter: Counter[str],
        genre_counter: Counter[str],
        average_popularity: int,
    ) -> list[dict[str, Any]]:
        existing_uris = {track["uri"] for track in tracks}
        seed_artists = [track["primary_artist_id"] for track in tracks if track["primary_artist_id"]]
        seed_artists = self._top_unique(seed_artists, 2)
        seed_genres = [genre for genre, _count in genre_counter.most_common(2)]
        seed_tracks = [track["id"] for track in sorted(tracks, key=lambda item: item["popularity"], reverse=True)[:2]]

        candidates: list[dict[str, Any]] = []
        try:
            candidates.extend(
                self.spotify_client.get_recommendations(
                    access_token,
                    seed_artists=seed_artists,
                    seed_genres=seed_genres,
                    seed_tracks=seed_tracks[:1],
                    limit=12,
                )
            )
        except SpotifyRateLimitError:
            raise
        except SpotifyClientError:
            pass

        if not candidates:
            for genre in seed_genres or [tracks[0]["primary_genre"]]:
                try:
                    candidates.extend(self.spotify_client.search_tracks_by_keyword(access_token, genre, limit=6))
                except SpotifyRateLimitError:
                    raise
                except SpotifyClientError:
                    continue

        curated: list[dict[str, Any]] = []
        seen_uris: set[str] = set()
        top_artist_names = {name for name, _count in artist_counter.most_common(4) if name}
        top_genres = {genre for genre, _count in genre_counter.most_common(4) if genre}

        artist_ids: list[str] = []
        for candidate in candidates:
            for artist in candidate.get("artists", []):
                artist_id = str(artist.get("id", "")).strip()
                if artist_id and artist_id not in artist_ids:
                    artist_ids.append(artist_id)
        artist_lookup = self.artist_cache_service.get_artists_lookup(access_token, artist_ids) if artist_ids else {}

        for candidate in candidates:
            uri = candidate.get("uri", "")
            if not uri or uri in existing_uris or uri in seen_uris:
                continue
            seen_uris.add(uri)

            candidate_artist_names = [artist.get("name", "") for artist in candidate.get("artists", [])]
            candidate_genres = self._build_track_genres(candidate.get("artists", []), artist_lookup)
            shared_genres = [genre for genre in candidate_genres if genre in top_genres]
            shared_artists = [name for name in candidate_artist_names if name in top_artist_names]
            popularity = int(candidate.get("popularity", 0) or 0)
            popularity_gap = abs(popularity - average_popularity)
            score = len(shared_genres) * 5 + len(shared_artists) * 4 - popularity_gap

            curated.append(
                {
                    "name": candidate.get("name", "Cancion"),
                    "artists": ", ".join(candidate_artist_names),
                    "album": (candidate.get("album") or {}).get("name", ""),
                    "image": self._extract_image(candidate),
                    "spotify_url": candidate.get("external_urls", {}).get("spotify", ""),
                    "popularity": popularity,
                    "genres_label": ", ".join(shared_genres[:2] or candidate_genres[:2]) or "Sin genero disponible",
                    "reason": self._build_add_reason(shared_genres, shared_artists, popularity, average_popularity),
                    "score": score,
                }
            )

        curated.sort(key=lambda item: (item["score"], item["popularity"]), reverse=True)
        return curated[:6]

    def _build_remove_recommendations(
        self,
        tracks: list[dict[str, Any]],
        artist_counter: Counter[str],
        genre_counter: Counter[str],
        average_popularity: int,
        duplicate_groups: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        duplicate_keys = {
            group["key"]: group["tracks"][1:]
            for group in duplicate_groups
            if group["count"] > 1
        }

        suggestions: list[dict[str, Any]] = []
        dominant_genres = {genre for genre, _count in genre_counter.most_common(4)}
        for track in tracks:
            track_key = track["search_key"]
            reasons: list[str] = []
            score = 0

            duplicate_items = duplicate_keys.get(track_key, [])
            if any(item["uri"] == track["uri"] for item in duplicate_items):
                reasons.append("Esta repetida dentro de la playlist.")
                score += 100

            if track["popularity"] < max(average_popularity - 18, 22):
                reasons.append(
                    f"Su popularidad ({track['popularity']}) cae bastante por debajo de la media ({average_popularity})."
                )
                score += max(average_popularity - track["popularity"], 0)

            if artist_counter.get(track["primary_artist"], 0) <= 1:
                reasons.append("Ese artista solo aparece una vez y no ayuda a consolidar el mood principal.")
                score += 12

            if track["genres"] and not any(genre in dominant_genres for genre in track["genres"]):
                reasons.append("Sus generos quedan fuera del nucleo dominante de la playlist.")
                score += 15

            if score < 28:
                continue

            suggestions.append(
                {
                    "name": track["name"],
                    "artists": track["artist_names"],
                    "album": track["album"],
                    "image": track["image"],
                    "spotify_url": track["spotify_url"],
                    "popularity": track["popularity"],
                    "reason": " ".join(reasons[:3]),
                    "score": score,
                    "position": track["position"],
                }
            )

        suggestions.sort(key=lambda item: (item["score"], -item["popularity"]), reverse=True)
        return suggestions[:6]

    def _build_ordering_recommendation(
        self,
        tracks: list[dict[str, Any]],
        artist_counter: Counter[str],
        genre_counter: Counter[str],
    ) -> dict[str, Any]:
        remaining = tracks.copy()
        ordered: list[dict[str, Any]] = []

        opener = max(
            remaining,
            key=lambda track: (
                track["popularity"]
                + artist_counter.get(track["primary_artist"], 0) * 3
                + genre_counter.get(track["primary_genre"], 0) * 2
            ),
        )
        ordered.append(opener)
        remaining.remove(opener)

        while remaining:
            previous = ordered[-1]
            next_track = max(remaining, key=lambda track: self._transition_score(previous, track, artist_counter, genre_counter))
            ordered.append(next_track)
            remaining.remove(next_track)

        preview: list[dict[str, Any]] = []
        last_genre = ""
        for index, track in enumerate(ordered[:12], start=1):
            if index == 1:
                reason = "Abre fuerte: combina tiron general y ADN central de la playlist."
            elif track["primary_genre"] == last_genre:
                reason = f"Mantiene el bloque {track['primary_genre'].lower()} sin romper la inercia."
            elif index >= 10:
                reason = "Funciona mejor en la recta final para cerrar con mas estabilidad."
            else:
                reason = "Sirve como puente entre artistas o generos cercanos."

            preview.append(
                {
                    "position": index,
                    "name": track["name"],
                    "artists": track["artist_names"],
                    "image": track["image"],
                    "spotify_url": track["spotify_url"],
                    "reason": reason,
                }
            )
            last_genre = track["primary_genre"]

        return {
            "strategy": "Abre con las canciones mas reconocibles, agrupa bloques de genero compatibles en el centro y deja las transiciones mas suaves para el cierre.",
            "preview": preview,
            "preview_count": len(preview),
            "remaining_count": max(len(ordered) - len(preview), 0),
        }

    def _build_coherence_reason(
        self,
        score: int,
        tracks: list[dict[str, Any]],
        genre_counter: Counter[str],
    ) -> str:
        dominant_genre, dominant_count = genre_counter.most_common(1)[0] if genre_counter else ("sin genero claro", 0)
        share = round((dominant_count / max(len(tracks), 1)) * 100)
        if score >= 80:
            return f"Hay un nucleo claro y el genero dominante ({dominant_genre}) sostiene buena parte del recorrido ({share}%)."
        if score >= 60:
            return f"La playlist conserva una linea reconocible, aunque el bloque principal ({dominant_genre}) no manda en todo el recorrido."
        return "El set cambia de foco con frecuencia y las transiciones pierden algo de continuidad."

    def _build_variety_reason(
        self,
        score: int,
        tracks: list[dict[str, Any]],
        artist_counter: Counter[str],
        genre_counter: Counter[str],
    ) -> str:
        unique_artists = len(artist_counter)
        unique_genres = len(genre_counter)
        if score >= 80:
            return f"Respira bien: mezcla {unique_artists} artistas y {unique_genres} generos sin romper el perfil central."
        if score >= 60:
            return f"Hay variedad razonable ({unique_artists} artistas, {unique_genres} generos), pero todavia queda margen para abrir mas el abanico."
        return "La seleccion se siente demasiado cerrada o demasiado dispersa para el numero de tracks actual."

    def _build_energy_reason(self, score: int, tracks: list[dict[str, Any]]) -> str:
        average_energy = round(mean(track["estimated_energy"] for track in tracks)) if tracks else 0
        if score >= 80:
            return f"La energia estimada se mantiene firme y bastante estable durante el recorrido (media {average_energy}/100)."
        if score >= 60:
            return f"La energia estimada es funcional, aunque hay algunos cambios de intensidad entre bloques (media {average_energy}/100)."
        return "La intensidad sube y baja demasiado; conviene ordenar mejor o recortar outliers para que el viaje fluya."

    def _build_repetition_reason(
        self,
        score: int,
        tracks: list[dict[str, Any]],
        artist_counter: Counter[str],
        duplicate_groups: list[dict[str, Any]],
    ) -> str:
        duplicate_count = sum(max(group["count"] - 1, 0) for group in duplicate_groups)
        dominant_artist, dominant_count = artist_counter.most_common(1)[0] if artist_counter else ("", 0)
        if score >= 80:
            return "Se detectan pocas repeticiones y la concentracion por artista sigue en una zona sana."
        if duplicate_count:
            return f"Hay {duplicate_count} duplicadas claras y {dominant_artist} aparece {dominant_count} veces, lo que comprime la escucha."
        return "No hay demasiadas duplicadas exactas, pero algunos artistas o albumes se repiten mas de la cuenta."

    def _build_popularity_reason(self, score: int, average_popularity: int, tracks: list[dict[str, Any]]) -> str:
        if score >= 80:
            return f"La popularidad media es alta ({average_popularity}) y el set mantiene bastante consistencia entre tracks."
        if score >= 60:
            return f"La popularidad media es solida ({average_popularity}) y acompana bien al perfil general de la playlist."
        return f"La popularidad media ({average_popularity}) cae bastante o mezcla extremos que hacen menos uniforme la seleccion."

    def _build_score_label(self, overall: int) -> str:
        if overall >= 85:
            return "Muy solida"
        if overall >= 75:
            return "Buena"
        if overall >= 60:
            return "Prometedora"
        if overall >= 45:
            return "Irregular"
        return "Dispersa"

    def _build_score_summary(self, overall: int, components: dict[str, dict[str, Any]]) -> str:
        strongest = max(components.values(), key=lambda item: item["value"])
        weakest = min(components.values(), key=lambda item: item["value"])
        if overall >= 75:
            return f"La playlist funciona bien: destaca en {strongest['label'].lower()} y solo necesita afinar {weakest['label'].lower()}."
        if overall >= 60:
            return f"Tiene una base util, pero {weakest['label'].lower()} todavia limita la experiencia global."
        return f"Ahora mismo pesa mas la debilidad en {weakest['label'].lower()} que las fortalezas del conjunto."

    def _build_score_highlights(self, components: dict[str, dict[str, Any]]) -> list[str]:
        highlights = [
            f"{component['label']} fuerte" for component in components.values() if component["value"] >= 78
        ]
        return highlights[:3] or ["Base util para seguir curando la playlist"]

    def _build_score_warnings(self, components: dict[str, dict[str, Any]]) -> list[str]:
        warnings = [
            f"{component['label']} necesita ajuste" for component in components.values() if component["value"] < 60
        ]
        return warnings[:3]

    @staticmethod
    def _build_album_concentration_penalty(tracks: list[dict[str, Any]]) -> int:
        albums = [track["album"] for track in tracks if track["album"]]
        if not albums:
            return 0
        album_counter = Counter(albums)
        dominant_share = max(album_counter.values(), default=0) / max(len(tracks), 1)
        return PlaylistEnhancer._clamp(max(dominant_share - 0.35, 0) * 70)

    @staticmethod
    def _build_duplicate_groups(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for track in tracks:
            grouped.setdefault(track["search_key"], []).append(track)
        return [
            {"key": key, "count": len(items), "tracks": items}
            for key, items in grouped.items()
            if len(items) > 1
        ]

    @staticmethod
    def _top_unique(values: list[str], limit: int) -> list[str]:
        seen: set[str] = set()
        unique: list[str] = []
        for value in values:
            if not value or value in seen:
                continue
            seen.add(value)
            unique.append(value)
            if len(unique) >= limit:
                break
        return unique

    @staticmethod
    def _extract_image(track: dict[str, Any]) -> str:
        images = (track.get("album") or {}).get("images") or []
        return images[0].get("url", "") if images else ""

    @staticmethod
    def _format_duration(total_duration_ms: int) -> str:
        total_minutes = round(total_duration_ms / 60000)
        hours, minutes = divmod(total_minutes, 60)
        return f"{hours} h {minutes:02d} min" if hours else f"{minutes} min"

    @staticmethod
    def _build_track_genres(artists: list[dict[str, Any]], artists_lookup: dict[str, dict[str, Any]]) -> list[str]:
        genres: list[str] = []
        for artist in artists:
            artist_id = str(artist.get("id", "")).strip()
            artist_payload = artists_lookup.get(artist_id, {})
            for genre in artist_payload.get("genres", []) or []:
                if genre not in genres:
                    genres.append(genre)
        return genres[:4]

    @staticmethod
    def _build_add_reason(
        shared_genres: list[str],
        shared_artists: list[str],
        popularity: int,
        average_popularity: int,
    ) -> str:
        if shared_artists:
            return f"Refuerza la linea de {shared_artists[0]} sin salirse del sonido actual."
        if shared_genres:
            return f"Comparte el nucleo de genero ({', '.join(shared_genres[:2])}) que domina la playlist."
        if popularity >= average_popularity:
            return "Aporta un pico de traccion sin desentonar demasiado con el resto."
        return "Funciona como variacion cercana para ensanchar la playlist sin romperla."

    @staticmethod
    def _estimate_track_energy(track: dict[str, Any], genres: list[str]) -> int:
        popularity = int(track.get("popularity", 0) or 0)
        duration_ms = int(track.get("duration_ms", 0) or 0)
        duration_minutes = duration_ms / 60000 if duration_ms else 0
        genre_text = " ".join(genres).lower()

        score = popularity * 0.55
        if 2.1 <= duration_minutes <= 4.4:
            score += 18
        elif duration_minutes > 0:
            score += 10

        energetic_terms = ("dance", "edm", "house", "techno", "electro", "hyperpop", "punk", "metal", "club", "trap")
        mellow_terms = ("ambient", "acoustic", "sleep", "piano", "chill", "folk", "jazz")
        if any(term in genre_text for term in energetic_terms):
            score += 18
        if any(term in genre_text for term in mellow_terms):
            score -= 10
        return PlaylistEnhancer._clamp(score)

    @staticmethod
    def _score_ratio_in_band(value: float, low: float, high: float) -> int:
        if low <= value <= high:
            return 100
        if value < low:
            distance = (low - value) / max(low, 0.01)
        else:
            distance = (value - high) / max(1 - high, 0.01)
        return PlaylistEnhancer._clamp(100 - distance * 100)

    @staticmethod
    def _clamp(value: float, minimum: int = 0, maximum: int = 100) -> int:
        return max(minimum, min(maximum, round(value)))

    @staticmethod
    def _transition_score(
        previous: dict[str, Any],
        current: dict[str, Any],
        artist_counter: Counter[str],
        genre_counter: Counter[str],
    ) -> int:
        score = 0
        shared_genres = set(previous["genres"]).intersection(current["genres"])
        popularity_gap = abs(previous["popularity"] - current["popularity"])
        score += len(shared_genres) * 8
        score += max(20 - popularity_gap, 0)
        score += genre_counter.get(current["primary_genre"], 0) * 2
        score += artist_counter.get(current["primary_artist"], 0)
        if previous["primary_artist"] == current["primary_artist"]:
            score -= 6
        return score
