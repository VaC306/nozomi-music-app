from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from services.spotify_client import SpotifyClient


TIME_RANGE_LABELS = {
    "short_term": "Ultimas 4 semanas",
    "medium_term": "Ultimos 6 meses",
    "long_term": "Ultimo ano",
}


@dataclass
class StatsSnapshot:
    profile: dict[str, Any]
    summary: dict[str, Any]
    top_tracks: dict[str, list[dict[str, Any]]]
    top_artists: dict[str, list[dict[str, Any]]]
    recent_tracks: list[dict[str, Any]]
    top_genres: list[tuple[str, int]]
    warnings: list[str]
    generated_at: str


class StatsService:
    def __init__(self, spotify_client: SpotifyClient) -> None:
        self.spotify_client = spotify_client
        self._artist_cache: dict[str, dict[str, Any]] = {}

    def build_snapshot(self, access_token: str) -> StatsSnapshot:
        warnings: list[str] = []
        profile = self.spotify_client.get_current_user(access_token)
        playlists = self._safe_fetch(
            lambda: self.spotify_client.get_user_playlists(access_token),
            warnings,
            [],
            "No se pudieron cargar playlists.",
        )

        top_tracks = {
            key: self._safe_fetch(
                lambda key=key: self._normalize_top_tracks(self.spotify_client.get_top_tracks(access_token, key, limit=5)),
                warnings,
                [],
                f"No se pudieron cargar top tracks para {TIME_RANGE_LABELS[key]}",
            )
            for key in TIME_RANGE_LABELS
        }

        top_artists = {
            key: self._safe_fetch(
                lambda key=key: self._normalize_top_artists(access_token, self.spotify_client.get_top_artists(access_token, key, limit=5)),
                warnings,
                [],
                f"No se pudieron cargar top artists para {TIME_RANGE_LABELS[key]}",
            )
            for key in TIME_RANGE_LABELS
        }

        recent_tracks = self._safe_fetch(
            lambda: self._normalize_recent_tracks(self.spotify_client.get_recently_played(access_token, limit=8)),
            warnings,
            [],
            "No se pudo cargar actividad reciente.",
        )

        top_genres = self._build_top_genres(top_artists)
        summary = {
            "playlist_count": len(playlists),
            "recent_count": len(recent_tracks),
            "top_track_count": sum(len(items) for items in top_tracks.values()),
            "top_genre_count": len(top_genres),
        }

        return StatsSnapshot(
            profile=self._normalize_profile(profile),
            summary=summary,
            top_tracks=top_tracks,
            top_artists=top_artists,
            recent_tracks=recent_tracks,
            top_genres=top_genres,
            warnings=warnings,
            generated_at=datetime.now().strftime("%d/%m/%Y %H:%M"),
        )

    @staticmethod
    def _safe_fetch(fetcher, warnings: list[str], fallback, warning_message: str):
        try:
            return fetcher()
        except Exception as exc:  # noqa: BLE001
            detail = str(exc).strip()
            warnings.append(f"{warning_message} {detail}".strip())
            return fallback

    def _normalize_top_artists(self, access_token: str, artists: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for index, artist in enumerate(artists, start=1):
            images = artist.get("images", []) or []
            genres = self._get_artist_genres(access_token, artist)
            normalized.append(
                {
                    "rank": index,
                    "name": artist.get("name", "Artista desconocido"),
                    "genres": genres,
                    "genres_label": ", ".join(genres[:2]) or "Sin genero",
                    "image_url": images[0].get("url", "") if images else "",
                    "spotify_url": artist.get("external_urls", {}).get("spotify", ""),
                }
            )
        return normalized

    def _get_artist_genres(self, access_token: str, artist: dict[str, Any]) -> list[str]:
        genres = artist.get("genres", []) or []
        if genres:
            return genres
        artist_id = artist.get("id", "")
        if not artist_id:
            return []
        if artist_id not in self._artist_cache:
            try:
                self._artist_cache[artist_id] = self.spotify_client.get_artist(access_token, artist_id)
            except Exception:  # noqa: BLE001
                self._artist_cache[artist_id] = {}
        return self._artist_cache[artist_id].get("genres", []) or []

    @staticmethod
    def _normalize_profile(profile: dict[str, Any]) -> dict[str, Any]:
        images = profile.get("images", []) or []
        return {
            "display_name": profile.get("display_name") or profile.get("id") or "Spotify User",
            "user_id": profile.get("id", "spotify"),
            "product": profile.get("product", ""),
            "profile_url": profile.get("external_urls", {}).get("spotify", ""),
            "avatar_url": images[0].get("url", "") if images else "",
        }

    @staticmethod
    def _normalize_top_tracks(tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for index, track in enumerate(tracks, start=1):
            album = track.get("album", {})
            images = album.get("images", []) or []
            normalized.append(
                {
                    "rank": index,
                    "name": track.get("name", "Sin titulo"),
                    "artists": ", ".join(artist.get("name", "") for artist in track.get("artists", [])),
                    "album": album.get("name", "Album"),
                    "image_url": images[0].get("url", "") if images else "",
                    "spotify_url": track.get("external_urls", {}).get("spotify", ""),
                }
            )
        return normalized

    @staticmethod
    def _normalize_recent_tracks(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in items:
            track = item.get("track") or {}
            album = track.get("album", {})
            images = album.get("images", []) or []
            played_at_raw = item.get("played_at", "")
            played_at_label = played_at_raw
            if played_at_raw:
                try:
                    played_at_label = datetime.fromisoformat(played_at_raw.replace("Z", "+00:00")).strftime("%d/%m %H:%M")
                except ValueError:
                    played_at_label = played_at_raw
            normalized.append(
                {
                    "name": track.get("name", "Sin titulo"),
                    "artists": ", ".join(artist.get("name", "") for artist in track.get("artists", [])),
                    "played_at": played_at_raw,
                    "played_at_label": played_at_label,
                    "context_type": (item.get("context") or {}).get("type", ""),
                    "image_url": images[0].get("url", "") if images else "",
                    "spotify_url": track.get("external_urls", {}).get("spotify", ""),
                }
            )
        return normalized

    @staticmethod
    def _build_top_genres(top_artists: dict[str, list[dict[str, Any]]]) -> list[tuple[str, int]]:
        counter: Counter[str] = Counter()
        for artists in top_artists.values():
            for artist in artists:
                for genre in artist.get("genres", []):
                    counter[genre] += 1
        return counter.most_common(6)
