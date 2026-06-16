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
        playlist_id = str(playlist.get("id", "")).strip()
        if not playlist_id:
            raise SpotifyClientError("La playlist seleccionada no es valida.")

        try:
            return self.cache_service.get_or_set(
                cache_key=f"playlist-enhancer-report:v1:{playlist_id}",
                ttl_seconds=self.REPORT_TTL_SECONDS,
                source_endpoint="playlist_enhancer_report",
                fetcher=lambda: self._build_playlist_report_uncached(access_token, playlist, playlist_id),
            )
        finally:
            self.artist_cache_service.log_metrics("playlist-enhancer")

    def _build_playlist_report_uncached(
        self,
        access_token: str,
        playlist: dict[str, Any],
        playlist_id: str,
    ) -> dict[str, Any]:
        playlist_tracks = self.cache_service.get_or_set(
            cache_key=f"playlist-tracks:v1:{playlist_id}",
            ttl_seconds=self.PLAYLIST_TRACKS_TTL_SECONDS,
            source_endpoint="get_playlist_tracks",
            fetcher=lambda: self.spotify_client.get_playlist_tracks(access_token, playlist_id),
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

        return {
            "playlist": {
                "id": playlist_id,
                "name": playlist.get("name", "Playlist"),
                "owner_name": playlist.get("owner_name") or playlist.get("owner", {}).get("display_name") or "Spotify",
                "track_total": len(tracks),
                "spotify_url": playlist.get("external_urls", {}).get("spotify", ""),
            },
            "stats": self._build_stats(tracks, artist_counter, genre_counter, average_popularity, duplicate_groups),
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
