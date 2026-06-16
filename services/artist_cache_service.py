from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from models import ArtistGenresCache, db
from services.spotify_client import SpotifyClient, SpotifyClientError


logger = logging.getLogger(__name__)


@dataclass
class ArtistCacheMetrics:
    artist_ids_consulted: list[str]
    cache_hits: int


class ArtistCacheService:
    CACHE_TTL = timedelta(days=30)

    def __init__(self, spotify_client: SpotifyClient) -> None:
        self.spotify_client = spotify_client
        self._artist_cache: dict[str, dict[str, Any]] = {}
        self._client_credentials_token: str | None = None
        self._consulted_artist_ids: list[str] = []
        self._consulted_artist_ids_seen: set[str] = set()
        self._cache_hits = 0
        self._pending_upserts: dict[str, dict[str, Any]] = {}

    def remember_artist_payload(self, artist: dict[str, Any]) -> None:
        artist_id = str(artist.get("id", "")).strip()
        if not artist_id:
            return
        genres = self._normalize_genres(artist.get("genres", []) or [])
        payload = {
            **artist,
            "id": artist_id,
            "name": str(artist.get("name", "")).strip(),
            "genres": genres,
            "popularity": int(artist.get("popularity", 0) or 0),
        }
        self._artist_cache[artist_id] = payload
        if "genres" in artist or "popularity" in artist:
            self._pending_upserts[artist_id] = payload

    def get_artist_genres(self, access_token: str, artist: dict[str, Any]) -> list[str]:
        genres = self._normalize_genres(artist.get("genres", []) or [])
        if genres:
            self.remember_artist_payload(artist)
            return genres
        artist_id = str(artist.get("id", "")).strip()
        if not artist_id:
            return []
        return self._normalize_genres(self.get_artists_lookup(access_token, [artist_id]).get(artist_id, {}).get("genres", []))

    def get_artists_lookup(self, access_token: str, artist_ids: list[str]) -> dict[str, dict[str, Any]]:
        lookup: dict[str, dict[str, Any]] = {}
        unique_artist_ids = self._dedupe_artist_ids(artist_ids)
        if not unique_artist_ids:
            return lookup

        missing_artist_ids: list[str] = []
        for artist_id in unique_artist_ids:
            payload = self._artist_cache.get(artist_id)
            if payload:
                lookup[artist_id] = payload
                continue
            missing_artist_ids.append(artist_id)

        fresh_cached_rows = self._get_fresh_cached_rows(missing_artist_ids)
        remaining_artist_ids: list[str] = []

        for artist_id in missing_artist_ids:
            cached_row = fresh_cached_rows.get(artist_id)
            if cached_row is None:
                remaining_artist_ids.append(artist_id)
                continue
            payload = self._row_to_payload(cached_row)
            self._artist_cache[artist_id] = payload
            lookup[artist_id] = payload
            self._cache_hits += 1

        if remaining_artist_ids:
            for start in range(0, len(remaining_artist_ids), 50):
                batch_ids = remaining_artist_ids[start : start + 50]
                for artist in self._fetch_artists_batch(access_token, batch_ids):
                    artist_id = str(artist.get("id", "")).strip()
                    if not artist_id:
                        continue
                    self.remember_artist_payload(artist)
                    lookup[artist_id] = self._artist_cache[artist_id]
                for artist_id in batch_ids:
                    if artist_id not in self._artist_cache:
                        payload = {"id": artist_id, "name": "", "genres": [], "popularity": 0}
                        self._artist_cache[artist_id] = payload
                        self._pending_upserts[artist_id] = payload

        self.flush_pending_writes()

        for artist_id in unique_artist_ids:
            if artist_id in self._artist_cache and artist_id not in lookup:
                lookup[artist_id] = self._artist_cache[artist_id]
        return lookup

    def get_metrics(self) -> ArtistCacheMetrics:
        return ArtistCacheMetrics(
            artist_ids_consulted=self._consulted_artist_ids.copy(),
            cache_hits=self._cache_hits,
        )

    def log_metrics(self, context: str) -> None:
        self.flush_pending_writes()
        metrics = self.get_metrics()
        logger.info(
            "[%s] Spotify calls=%s artist_ids=%s cache_hits=%s",
            context,
            self.spotify_client.spotify_call_count,
            metrics.artist_ids_consulted,
            metrics.cache_hits,
        )

    def _dedupe_artist_ids(self, artist_ids: list[str]) -> list[str]:
        unique_artist_ids: list[str] = []
        seen: set[str] = set()

        for raw_artist_id in artist_ids:
            artist_id = str(raw_artist_id).strip()
            if not artist_id or artist_id in seen:
                continue
            seen.add(artist_id)
            unique_artist_ids.append(artist_id)
            if artist_id not in self._consulted_artist_ids_seen:
                self._consulted_artist_ids_seen.add(artist_id)
                self._consulted_artist_ids.append(artist_id)
        return unique_artist_ids

    def _get_fresh_cached_rows(self, artist_ids: list[str]) -> dict[str, ArtistGenresCache]:
        if not artist_ids:
            return {}
        freshness_limit = datetime.utcnow() - self.CACHE_TTL
        rows = (
            ArtistGenresCache.query.filter(ArtistGenresCache.artist_id.in_(artist_ids))
            .filter(ArtistGenresCache.fetched_at >= freshness_limit)
            .all()
        )
        return {row.artist_id: row for row in rows}

    def _fetch_artists_batch(self, access_token: str, artist_ids: list[str]) -> list[dict[str, Any]]:
        if not artist_ids:
            return []
        try:
            return self.spotify_client.get_artists(access_token, artist_ids)
        except SpotifyClientError as exc:
            if str(exc).strip().lower() != "forbidden":
                raise
        fallback_token = self._get_client_credentials_access_token()
        return self.spotify_client.get_artists(fallback_token, artist_ids)

    def _get_client_credentials_access_token(self) -> str:
        if self._client_credentials_token:
            return self._client_credentials_token
        token_data = self.spotify_client.get_client_credentials_token()
        self._client_credentials_token = str(token_data.get("access_token", "")).strip()
        if not self._client_credentials_token:
            raise SpotifyClientError("Spotify no devolvio un token de aplicacion valido.")
        return self._client_credentials_token

    def flush_pending_writes(self) -> None:
        if not self._pending_upserts:
            return

        artist_ids = list(self._pending_upserts)
        existing_rows = {
            row.artist_id: row
            for row in ArtistGenresCache.query.filter(ArtistGenresCache.artist_id.in_(artist_ids)).all()
        }
        now = datetime.utcnow()

        for artist_id, artist in self._pending_upserts.items():
            cached_row = existing_rows.get(artist_id)
            if not cached_row:
                cached_row = ArtistGenresCache()
                cached_row.artist_id = artist_id
            cached_row.artist_name = str(artist.get("name", "")).strip()
            cached_row.genres_json = json.dumps(self._normalize_genres(artist.get("genres", []) or []))
            cached_row.popularity = int(artist.get("popularity", 0) or 0)
            cached_row.fetched_at = now
            cached_row.updated_at = now
            db.session.add(cached_row)

        db.session.commit()
        self._pending_upserts.clear()

    @staticmethod
    def _row_to_payload(row: ArtistGenresCache) -> dict[str, Any]:
        return {
            "id": row.artist_id,
            "name": row.artist_name,
            "genres": ArtistCacheService._parse_cached_genres(row.genres_json),
            "popularity": int(row.popularity or 0),
        }

    @staticmethod
    def _parse_cached_genres(genres_json: str) -> list[str]:
        try:
            payload = json.loads(genres_json or "[]")
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return ArtistCacheService._normalize_genres(payload)

    @staticmethod
    def _normalize_genres(genres: list[Any]) -> list[str]:
        return sorted({str(genre).strip() for genre in genres if str(genre).strip()})
