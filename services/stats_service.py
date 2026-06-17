from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from models import DashboardTopSnapshot, db
from services.artist_cache_service import ArtistCacheService
from services.discord_monitoring import DiscordMonitoringService
from services.spotify_api_cache_service import SpotifyApiCacheService
from services.spotify_client import SpotifyClient, SpotifyClientError, SpotifyRateLimitError


TIME_RANGE_LABELS = {
    "short_term": "Ultimas 4 semanas",
    "medium_term": "Ultimos 6 meses",
    "long_term": "Ultimo ano",
}
ENABLE_ARTIST_GENRES = False
DASHBOARD_TOP_ITEMS_LIMIT = 50
DASHBOARD_PREVIEW_ITEMS_LIMIT = 10


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
    SNAPSHOT_TTL_SECONDS = 600
    SNAPSHOT_CACHE_KEY = "dashboard-snapshot:v3"

    def __init__(
        self,
        spotify_client: SpotifyClient,
        user_id: str,
        monitoring_service: DiscordMonitoringService | None = None,
    ) -> None:
        self.spotify_client = spotify_client
        self.user_id = user_id or "anonymous"
        self.artist_cache_service = ArtistCacheService(spotify_client) if ENABLE_ARTIST_GENRES else None
        self.cache_service = SpotifyApiCacheService(
            cache_scope=f"user:{self.user_id}",
            monitoring_service=monitoring_service,
        )

    def build_snapshot(self, access_token: str) -> StatsSnapshot:
        return self.build_snapshot_with_mode(access_token)

    def build_snapshot_with_mode(
        self,
        access_token: str,
        cache_only: bool = False,
        force_refresh: bool = False,
    ) -> StatsSnapshot:
        if cache_only:
            snapshot_payload = self.cache_service.get(self.SNAPSHOT_CACHE_KEY, allow_stale=True)
            if snapshot_payload is None:
                raise SpotifyClientError(
                    "No hay una copia en cache del dashboard todavia. Cambia a modo normal o fuerza un refresco cuando Spotify lo permita."
                )
            return StatsSnapshot(**snapshot_payload)

        snapshot_payload = self.cache_service.get_or_set(
            cache_key=self.SNAPSHOT_CACHE_KEY,
            ttl_seconds=self.SNAPSHOT_TTL_SECONDS,
            source_endpoint="dashboard_snapshot",
            fetcher=lambda: self._build_snapshot_payload(access_token),
            force_refresh=force_refresh,
        )
        return StatsSnapshot(**snapshot_payload)

    def clear_snapshot_cache(self) -> None:
        self.cache_service.delete(self.SNAPSHOT_CACHE_KEY)

    def _build_snapshot_payload(self, access_token: str) -> dict[str, Any]:
        warnings: list[str] = []
        try:
            profile = self.spotify_client.get_current_user(access_token)
            playlists = self._safe_fetch(
                lambda: self.spotify_client.get_user_playlists(access_token),
                warnings,
                [],
                "No se pudieron cargar playlists.",
            )

            raw_top_tracks = {
                key: self._safe_fetch(
                    lambda key=key: self.spotify_client.get_top_tracks(access_token, key, limit=DASHBOARD_TOP_ITEMS_LIMIT),
                    warnings,
                    [],
                    f"No se pudieron cargar top tracks para {TIME_RANGE_LABELS[key]}",
                )
                for key in TIME_RANGE_LABELS
            }

            raw_top_artists = {
                key: self._safe_fetch(
                    lambda key=key: self.spotify_client.get_top_artists(access_token, key, limit=DASHBOARD_TOP_ITEMS_LIMIT),
                    warnings,
                    [],
                    f"No se pudieron cargar top artists para {TIME_RANGE_LABELS[key]}",
                )
                for key in TIME_RANGE_LABELS
            }

            raw_recent_tracks = self._safe_fetch(
                lambda: self.spotify_client.get_recently_played(access_token, limit=8),
                warnings,
                [],
                "No se pudo cargar actividad reciente.",
            )

            top_tracks = {
                key: self._normalize_top_tracks(access_token, tracks)
                for key, tracks in raw_top_tracks.items()
            }
            top_artists = {
                key: self._normalize_top_artists(access_token, artists)
                for key, artists in raw_top_artists.items()
            }
            recent_tracks = self._normalize_recent_tracks(access_token, raw_recent_tracks)
            self._store_top_snapshots(top_tracks=top_tracks, top_artists=top_artists)

            summary = {
                "playlist_count": len(playlists),
                "recent_count": len(recent_tracks),
                "top_track_count": max((len(items) for items in top_tracks.values()), default=0),
                "top_artist_count": max((len(items) for items in top_artists.values()), default=0),
                "top_genre_count": 0,
            }

            snapshot = StatsSnapshot(
                profile=self._normalize_profile(profile),
                summary=summary,
                top_tracks=top_tracks,
                top_artists=top_artists,
                recent_tracks=recent_tracks,
                top_genres=[],
                warnings=warnings,
                generated_at=datetime.now().strftime("%d/%m/%Y %H:%M"),
            )
            return asdict(snapshot)
        finally:
            if self.artist_cache_service is not None:
                self.artist_cache_service.log_metrics("dashboard")

    def get_top_items(self, item_type: str, time_range: str) -> list[dict[str, Any]]:
        if item_type not in {"tracks", "artists"}:
            return []
        row = DashboardTopSnapshot.query.filter_by(
            spotify_user_id=self.user_id,
            snapshot_type=item_type,
            time_range=time_range,
        ).first()
        if row is None:
            return []
        try:
            payload = json.loads(row.payload_json or "[]")
        except json.JSONDecodeError:
            return []
        return payload if isinstance(payload, list) else []

    @staticmethod
    def _safe_fetch(fetcher, warnings: list[str], fallback, warning_message: str):
        try:
            return fetcher()
        except SpotifyRateLimitError:
            raise
        except Exception as exc:  # noqa: BLE001
            detail = str(exc).strip()
            warnings.append(f"{warning_message} {detail}".strip())
            return fallback

    def _normalize_top_artists(self, access_token: str, artists: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for index, artist in enumerate(artists, start=1):
            images = artist.get("images", []) or []
            normalized.append(
                {
                    "rank": index,
                    "name": artist.get("name", "Artista desconocido"),
                    "image_url": images[0].get("url", "") if images else "",
                    "spotify_url": artist.get("external_urls", {}).get("spotify", ""),
                }
            )
        return normalized

    def _store_top_snapshots(
        self,
        top_tracks: dict[str, list[dict[str, Any]]],
        top_artists: dict[str, list[dict[str, Any]]],
    ) -> None:
        for snapshot_type, payload_by_range in {
            "tracks": top_tracks,
            "artists": top_artists,
        }.items():
            for time_range, items in payload_by_range.items():
                row = DashboardTopSnapshot.query.filter_by(
                    spotify_user_id=self.user_id,
                    snapshot_type=snapshot_type,
                    time_range=time_range,
                ).first()
                if row is None:
                    row = DashboardTopSnapshot(
                        spotify_user_id=self.user_id,
                        snapshot_type=snapshot_type,
                        time_range=time_range,
                    )
                row.payload_json = json.dumps(items)
                row.fetched_at = datetime.utcnow()
                row.updated_at = datetime.utcnow()
                db.session.add(row)
        db.session.commit()

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

    def _normalize_top_tracks(self, access_token: str, tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
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

    def _normalize_recent_tracks(self, access_token: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
