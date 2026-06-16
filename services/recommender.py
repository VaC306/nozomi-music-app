from __future__ import annotations

import re
import unicodedata
from typing import Any

from services.spotify_api_cache_service import SpotifyApiCacheService
from services.spotify_client import SpotifyClient, SpotifyClientError, SpotifyRateLimitError


class Recommender:
    RESULT_TTL_SECONDS = 86400

    def __init__(self, spotify_client: SpotifyClient, user_id: str) -> None:
        self.spotify_client = spotify_client
        self.cache_service = SpotifyApiCacheService(cache_scope=f"user:{user_id or 'anonymous'}")

    @staticmethod
    def get_sample_genres() -> list[str]:
        return [
            "indie",
            "synth-pop",
            "j-rock",
            "lo-fi",
            "house",
            "alt-rock",
        ]

    @staticmethod
    def get_sample_artists() -> list[str]:
        return [
            "The Strokes",
            "Tame Impala",
            "Arctic Monkeys",
            "Phoebe Bridgers",
            "Daft Punk",
            "Mitski",
        ]

    def build_artist_discovery_result(
        self,
        access_token: str,
        artist_name: str,
        limit: int = 6,
    ) -> dict[str, Any]:
        artist_value = artist_name.strip()
        if not artist_value:
            return {
                "query": "",
                "base_artist": None,
                "similar_artists": [],
                "similar_tracks": [],
                "artist_count": 0,
                "track_count": 0,
                "source": "none",
                "warning": "",
            }

        normalized_artist = self._normalize_text(artist_value)
        cache_key = f"artist-discovery:v1:{normalized_artist}:{limit}"
        return self.cache_service.get_or_set(
            cache_key=cache_key,
            ttl_seconds=self.RESULT_TTL_SECONDS,
            source_endpoint="artist_discovery_result",
            fetcher=lambda: self._build_artist_discovery_result_uncached(access_token, artist_value, limit),
        )

    def _build_artist_discovery_result_uncached(
        self,
        access_token: str,
        artist_name: str,
        limit: int,
    ) -> dict[str, Any]:
        warnings: list[str] = []
        source = "artist-seeded"
        base_artist = self._find_base_artist(access_token, artist_name)
        if not base_artist:
            return {
                "query": artist_name,
                "base_artist": None,
                "similar_artists": [],
                "similar_tracks": [],
                "artist_count": 0,
                "track_count": 0,
                "source": "none",
                "warning": "No encontramos ese artista en Spotify.",
            }

        artist_id = str(base_artist.get("id", "")).strip()
        top_tracks = self._get_artist_top_tracks(access_token, artist_id)
        seed_track_ids = [str(track.get("id", "")).strip() for track in top_tracks if track.get("id")]

        recommendation_tracks: list[dict[str, Any]] = []
        try:
            recommendation_tracks = self.cache_service.get_or_set(
                cache_key=f"artist-recommendations:v1:{artist_id}:{limit}",
                ttl_seconds=self.RESULT_TTL_SECONDS,
                source_endpoint="recommendations",
                fetcher=lambda: self.spotify_client.get_recommendations(
                    access_token,
                    seed_artists=[artist_id],
                    seed_tracks=seed_track_ids[:2],
                    limit=max(limit * 2, 12),
                ),
            )
        except SpotifyRateLimitError:
            raise
        except SpotifyClientError:
            warnings.append(
                "Spotify limito las recomendaciones directas por artista; usamos una ruta alternativa basada en top tracks y genero."
            )

        filtered_recommendation_tracks = self._filter_tracks_for_discovery(recommendation_tracks, artist_id)

        if not filtered_recommendation_tracks and seed_track_ids:
            source = "track-seeded-fallback"
            try:
                filtered_recommendation_tracks = self._filter_tracks_for_discovery(
                    self.cache_service.get_or_set(
                        cache_key=f"track-seeded-recommendations:v1:{artist_id}:{limit}",
                        ttl_seconds=self.RESULT_TTL_SECONDS,
                        source_endpoint="recommendations",
                        fetcher=lambda: self.spotify_client.get_recommendations(
                            access_token,
                            seed_tracks=seed_track_ids[:3],
                            limit=max(limit * 2, 12),
                        ),
                    ),
                    artist_id,
                )
            except SpotifyRateLimitError:
                raise
            except SpotifyClientError:
                warnings.append("Spotify tambien limito el fallback por canciones del artista base.")

        similar_artists = self._build_similar_artists(
            access_token=access_token,
            base_artist=base_artist,
            candidate_tracks=filtered_recommendation_tracks,
            limit=limit,
        )

        if not similar_artists and base_artist.get("genres"):
            source = "genre-fallback"
            similar_artists = self._build_genre_fallback_artists(access_token, base_artist, limit)

        if not filtered_recommendation_tracks and similar_artists:
            fallback_tracks: list[dict[str, Any]] = []
            for artist in similar_artists[: min(3, len(similar_artists))]:
                fallback_artist_id = artist.get("id", "")
                if not fallback_artist_id:
                    continue
                try:
                    fallback_tracks.extend(self._get_artist_top_tracks(access_token, fallback_artist_id))
                except SpotifyRateLimitError:
                    raise
                except SpotifyClientError:
                    continue
            filtered_recommendation_tracks = self._filter_tracks_for_discovery(fallback_tracks, artist_id)

        similar_tracks = [self._format_track(track) for track in filtered_recommendation_tracks[:limit]]

        if not similar_artists and not similar_tracks and not warnings:
            warnings.append("Spotify no devolvio resultados relacionados para este artista.")

        return {
            "query": artist_name,
            "base_artist": self._format_artist(base_artist),
            "similar_artists": similar_artists,
            "similar_tracks": similar_tracks,
            "artist_count": len(similar_artists),
            "track_count": len(similar_tracks),
            "source": source,
            "warning": " ".join(warnings).strip(),
        }

    def recommend_by_genre(self, access_token: str, genre: str, limit: int = 6) -> list[dict[str, Any]]:
        result = self.build_recommendation_result(access_token, genre, limit=limit)
        return result.get("tracks", [])

    def build_recommendation_result(
        self,
        access_token: str,
        genre: str,
        limit: int = 6,
    ) -> dict[str, Any]:
        genre_value = genre.strip().lower()
        if not genre_value:
            return {
                "genre": "",
                "tracks": [],
                "count": 0,
                "source": "none",
                "warning": "",
            }

        cache_key = f"recommendations:v1:{genre_value}:{limit}"
        return self.cache_service.get_or_set(
            cache_key=cache_key,
            ttl_seconds=self.RESULT_TTL_SECONDS,
            source_endpoint="recommendations_result",
            fetcher=lambda: self._build_recommendation_result_uncached(access_token, genre_value, limit),
        )

    def _build_recommendation_result_uncached(self, access_token: str, genre_value: str, limit: int) -> dict[str, Any]:
        warnings: list[str] = []
        source = "recommendations"
        try:
            direct_tracks = self.spotify_client.get_recommendations_by_genre(
                access_token,
                genre_value,
                limit=limit,
            )
        except SpotifyRateLimitError:
            raise
        except SpotifyClientError:
            direct_tracks = []
            warnings.append(
                "Spotify limito el endpoint principal de recomendaciones; mostramos un fallback basado en artistas del genero."
            )

        tracks = self._deduplicate_tracks(direct_tracks)
        if not tracks:
            source = "artist-fallback"
            try:
                artists = self.cache_service.get_or_set(
                    cache_key=f"search-artists-by-genre:v1:{genre_value}:3",
                    ttl_seconds=self.RESULT_TTL_SECONDS,
                    source_endpoint="search_artists_by_genre",
                    fetcher=lambda: self.spotify_client.search_artists_by_genre(access_token, genre_value, limit=3),
                )
            except SpotifyRateLimitError:
                raise
            except SpotifyClientError:
                artists = []
                warnings.append(
                    "Spotify no permitio buscar artistas para este genero con la sesion actual."
                )

            fallback_tracks: list[dict[str, Any]] = []
            for artist in artists:
                artist_id = str(artist.get("id", "")).strip()
                if not artist_id:
                    continue
                try:
                    artist_tracks = self.cache_service.get_or_set(
                        cache_key=f"artist-top-tracks:v1:{artist_id}",
                        ttl_seconds=self.RESULT_TTL_SECONDS,
                        source_endpoint="artist_top_tracks",
                        fetcher=lambda artist_id=artist_id: self.spotify_client.get_artist_top_tracks(access_token, artist_id),
                    )
                except SpotifyRateLimitError:
                    raise
                except SpotifyClientError:
                    continue
                fallback_tracks.extend(artist_tracks)
                if len(fallback_tracks) >= limit * 2:
                    break
            tracks = self._deduplicate_tracks(fallback_tracks)

        if not tracks:
            source = "keyword-fallback"
            try:
                keyword_tracks = self.cache_service.get_or_set(
                    cache_key=f"search-tracks-keyword:v1:{genre_value}:{max(limit, 8)}",
                    ttl_seconds=self.RESULT_TTL_SECONDS,
                    source_endpoint="search_tracks_by_keyword",
                    fetcher=lambda: self.spotify_client.search_tracks_by_keyword(access_token, genre_value, limit=max(limit, 8)),
                )
            except SpotifyRateLimitError:
                raise
            except SpotifyClientError:
                keyword_tracks = []
                warnings.append(
                    "Spotify tambien bloqueo la busqueda alternativa por palabra clave."
                )
            tracks = self._deduplicate_tracks(keyword_tracks)

        if not tracks and not warnings:
            warnings.append("Spotify no devolvio recomendaciones para este genero.")

        formatted_tracks = [self._format_track(track) for track in tracks[:limit]]
        return {
            "genre": genre_value,
            "tracks": formatted_tracks,
            "count": len(formatted_tracks),
            "source": source,
            "warning": " ".join(warnings).strip(),
        }

    def _find_base_artist(self, access_token: str, artist_name: str) -> dict[str, Any] | None:
        candidates = self.cache_service.get_or_set(
            cache_key=f"search-artists:v1:{self._normalize_text(artist_name)}:5",
            ttl_seconds=self.RESULT_TTL_SECONDS,
            source_endpoint="search_artists",
            fetcher=lambda: self.spotify_client.search_artists(access_token, artist_name, limit=5),
        )
        if not candidates:
            return None

        target = self._normalize_text(artist_name)
        exact_matches = [artist for artist in candidates if self._normalize_text(str(artist.get("name", ""))) == target]
        if exact_matches:
            return exact_matches[0]

        partial_matches = [artist for artist in candidates if target in self._normalize_text(str(artist.get("name", "")))]
        if partial_matches:
            return partial_matches[0]

        return candidates[0]

    def _get_artist_top_tracks(self, access_token: str, artist_id: str) -> list[dict[str, Any]]:
        if not artist_id:
            return []
        return self.cache_service.get_or_set(
            cache_key=f"artist-top-tracks:v1:{artist_id}",
            ttl_seconds=self.RESULT_TTL_SECONDS,
            source_endpoint="artist_top_tracks",
            fetcher=lambda: self.spotify_client.get_artist_top_tracks(access_token, artist_id),
        )

    def _build_similar_artists(
        self,
        access_token: str,
        base_artist: dict[str, Any],
        candidate_tracks: list[dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]]:
        base_artist_id = str(base_artist.get("id", "")).strip()
        candidate_artist_ids: list[str] = []
        for track in candidate_tracks:
            for artist in track.get("artists", []):
                artist_id = str(artist.get("id", "")).strip()
                if not artist_id or artist_id == base_artist_id:
                    continue
                if artist_id not in candidate_artist_ids:
                    candidate_artist_ids.append(artist_id)

        if not candidate_artist_ids:
            return []

        detailed_artists: list[dict[str, Any]] = []
        for start in range(0, len(candidate_artist_ids), 50):
            chunk = candidate_artist_ids[start : start + 50]
            detailed_artists.extend(
                self.cache_service.get_or_set(
                    cache_key=f"artists-batch:v1:{'-'.join(chunk)}",
                    ttl_seconds=self.RESULT_TTL_SECONDS,
                    source_endpoint="artists",
                    fetcher=lambda chunk=chunk: self.spotify_client.get_artists(access_token, chunk),
                )
            )

        unique_artists = self._deduplicate_artists(detailed_artists)
        return [self._format_artist(artist) for artist in unique_artists[:limit]]

    def _build_genre_fallback_artists(
        self,
        access_token: str,
        base_artist: dict[str, Any],
        limit: int,
    ) -> list[dict[str, Any]]:
        base_artist_id = str(base_artist.get("id", "")).strip()
        artists: list[dict[str, Any]] = []
        for genre in (base_artist.get("genres") or [])[:2]:
            try:
                artists.extend(
                    self.cache_service.get_or_set(
                        cache_key=f"search-artists-by-genre:v1:{genre}:{limit * 2}",
                        ttl_seconds=self.RESULT_TTL_SECONDS,
                        source_endpoint="search_artists_by_genre",
                        fetcher=lambda genre=genre: self.spotify_client.search_artists_by_genre(access_token, genre, limit=limit * 2),
                    )
                )
            except SpotifyRateLimitError:
                raise
            except SpotifyClientError:
                continue

        unique_artists = []
        seen: set[str] = set()
        for artist in artists:
            artist_id = str(artist.get("id", "")).strip()
            if not artist_id or artist_id == base_artist_id or artist_id in seen:
                continue
            seen.add(artist_id)
            unique_artists.append(artist)
        return [self._format_artist(artist) for artist in unique_artists[:limit]]

    @staticmethod
    def _format_track(track: dict[str, Any]) -> dict[str, Any]:
        artists = ", ".join(artist.get("name", "") for artist in track.get("artists", []))
        album = (track.get("album") or {}).get("name", "")
        image = ""
        images = (track.get("album") or {}).get("images") or []
        if images:
            image = images[0].get("url", "")
        return {
            "name": track.get("name", "Cancion sin titulo"),
            "artists": artists,
            "album": album,
            "url": track.get("external_urls", {}).get("spotify", ""),
            "image": image,
            "preview_url": track.get("preview_url", ""),
        }

    @staticmethod
    def _format_artist(artist: dict[str, Any]) -> dict[str, Any]:
        image = ""
        images = artist.get("images") or []
        if images:
            image = images[0].get("url", "")
        genres = artist.get("genres") or []
        followers_total = ((artist.get("followers") or {}).get("total")) or 0
        return {
            "id": str(artist.get("id", "")).strip(),
            "name": artist.get("name", "Artista sin nombre"),
            "image": image,
            "url": artist.get("external_urls", {}).get("spotify", ""),
            "genres": genres,
            "genres_label": ", ".join(genres[:2]) or "Sin genero disponible",
            "followers": followers_total,
            "popularity": artist.get("popularity", 0),
        }

    @staticmethod
    def _deduplicate_artists(artists: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        unique_artists: list[dict[str, Any]] = []
        for artist in artists:
            artist_id = str(artist.get("id", "")).strip()
            if not artist_id or artist_id in seen:
                continue
            seen.add(artist_id)
            unique_artists.append(artist)
        return unique_artists

    @staticmethod
    def _filter_tracks_for_discovery(tracks: list[dict[str, Any]], base_artist_id: str) -> list[dict[str, Any]]:
        filtered_tracks: list[dict[str, Any]] = []
        for track in Recommender._deduplicate_tracks(tracks):
            artist_ids = [str(artist.get("id", "")).strip() for artist in track.get("artists", [])]
            if base_artist_id and base_artist_id in artist_ids:
                continue
            filtered_tracks.append(track)
        return filtered_tracks

    @staticmethod
    def _normalize_text(value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value or "")
        normalized = normalized.encode("ascii", "ignore").decode("ascii")
        normalized = normalized.lower().strip()
        normalized = re.sub(r"\s+", " ", normalized)
        normalized = re.sub(r"[^a-z0-9 ]", "", normalized)
        return normalized

    @staticmethod
    def _deduplicate_tracks(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        unique_tracks: list[dict[str, Any]] = []
        for track in tracks:
            uri = track.get("uri")
            if not uri or uri in seen:
                continue
            seen.add(uri)
            unique_tracks.append(track)
        return unique_tracks
