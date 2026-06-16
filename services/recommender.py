from __future__ import annotations

from typing import Any

from services.spotify_client import SpotifyClient, SpotifyClientError


class Recommender:
    def __init__(self, spotify_client: SpotifyClient) -> None:
        self.spotify_client = spotify_client

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

    def recommend_by_genre(self, access_token: str, genre: str, limit: int = 6) -> list[dict[str, Any]]:
        genre_value = genre.strip().lower()
        if not genre_value:
            return []

        try:
            direct_tracks = self.spotify_client.get_recommendations_by_genre(access_token, genre_value, limit=limit)
        except SpotifyClientError:
            direct_tracks = []

        deduplicated = self._deduplicate_tracks(direct_tracks)
        if deduplicated:
            return deduplicated[:limit]

        artists = self.spotify_client.search_artists_by_genre(access_token, genre_value, limit=3)
        tracks: list[dict[str, Any]] = []
        for artist in artists:
            tracks.extend(self.spotify_client.get_artist_top_tracks(access_token, artist["id"]))
            if len(tracks) >= limit * 2:
                break
        return self._deduplicate_tracks(tracks)[:limit]

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

        warnings: list[str] = []
        source = "recommendations"
        try:
            direct_tracks = self.spotify_client.get_recommendations_by_genre(
                access_token,
                genre_value,
                limit=limit,
            )
        except SpotifyClientError:
            direct_tracks = []
            warnings.append(
                "Spotify limito el endpoint principal de recomendaciones; mostramos un fallback basado en artistas del genero."
            )

        tracks = self._deduplicate_tracks(direct_tracks)
        if not tracks:
            source = "artist-fallback"
            try:
                artists = self.spotify_client.search_artists_by_genre(access_token, genre_value, limit=3)
            except SpotifyClientError:
                artists = []
                warnings.append(
                    "Spotify no permitio buscar artistas para este genero con la sesion actual."
                )

            fallback_tracks: list[dict[str, Any]] = []
            for artist in artists:
                try:
                    fallback_tracks.extend(self.spotify_client.get_artist_top_tracks(access_token, artist["id"]))
                except SpotifyClientError:
                    continue
                if len(fallback_tracks) >= limit * 2:
                    break
            tracks = self._deduplicate_tracks(fallback_tracks)

        if not tracks:
            source = "keyword-fallback"
            try:
                keyword_tracks = self.spotify_client.search_tracks_by_keyword(access_token, genre_value, limit=max(limit, 8))
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
