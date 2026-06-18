from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Callable

from models import SpotifyApiCache, db
from services.discord_monitoring import DiscordMonitoringService
from services.spotify_client import SpotifyRateLimitError


class SpotifyApiCacheService:
    def __init__(self, cache_scope: str = "app", monitoring_service: DiscordMonitoringService | None = None) -> None:
        self.cache_scope = cache_scope or "app"
        self.monitoring_service = monitoring_service

    def get(self, cache_key: str, allow_stale: bool = False) -> Any | None:
        row = self._get_row(cache_key)
        if not row:
            return None
        if allow_stale or row.expires_at >= datetime.utcnow():
            if self.monitoring_service is not None:
                self.monitoring_service.record_cache_hit()
            return self._deserialize_payload(row.payload_json)
        return None

    def set(self, cache_key: str, payload: Any, ttl_seconds: int, source_endpoint: str) -> None:
        now = datetime.utcnow()
        row = self._get_row(cache_key)
        if not row:
            row = SpotifyApiCache()
            row.cache_scope = self.cache_scope
            row.cache_key = cache_key
        row.source_endpoint = source_endpoint
        row.payload_json = json.dumps(payload)
        row.fetched_at = now
        row.expires_at = now + timedelta(seconds=max(ttl_seconds, 1))
        row.updated_at = now
        db.session.add(row)
        db.session.commit()

    def delete(self, cache_key: str) -> None:
        row = self._get_row(cache_key)
        if not row:
            return
        db.session.delete(row)
        db.session.commit()

    def delete_by_prefix(self, cache_key_prefix: str) -> None:
        rows = SpotifyApiCache.query.filter(
            SpotifyApiCache.cache_scope == self.cache_scope,
            SpotifyApiCache.cache_key.like(f"{cache_key_prefix}%"),
        ).all()
        if not rows:
            return
        for row in rows:
            db.session.delete(row)
        db.session.commit()

    def get_metadata(self, cache_key: str) -> dict[str, Any] | None:
        row = self._get_row(cache_key)
        if not row:
            return None
        return self._build_metadata(row)

    def get_latest_metadata_by_prefix(self, cache_key_prefix: str) -> dict[str, Any] | None:
        row = (
            SpotifyApiCache.query.filter(
                SpotifyApiCache.cache_scope == self.cache_scope,
                SpotifyApiCache.cache_key.like(f"{cache_key_prefix}%"),
            )
            .order_by(SpotifyApiCache.fetched_at.desc())
            .first()
        )
        if not row:
            return None
        return self._build_metadata(row)

    @staticmethod
    def _build_metadata(row: SpotifyApiCache) -> dict[str, Any]:
        now = datetime.utcnow()
        return {
            "cache_key": row.cache_key,
            "source_endpoint": row.source_endpoint,
            "fetched_at": row.fetched_at,
            "expires_at": row.expires_at,
            "is_stale": row.expires_at < now,
            "age_seconds": max(int((now - row.fetched_at).total_seconds()), 0) if row.fetched_at else 0,
        }

    def get_or_set(
        self,
        cache_key: str,
        ttl_seconds: int,
        source_endpoint: str,
        fetcher: Callable[[], Any],
        allow_stale_on_rate_limit: bool = True,
        force_refresh: bool = False,
    ) -> Any:
        row = self._get_row(cache_key)
        if row and row.expires_at >= datetime.utcnow() and not force_refresh:
            if self.monitoring_service is not None:
                self.monitoring_service.record_cache_hit()
            return self._deserialize_payload(row.payload_json)

        if self.monitoring_service is not None:
            self.monitoring_service.record_cache_miss()

        stale_payload = self._deserialize_payload(row.payload_json) if row else None
        try:
            payload = fetcher()
        except SpotifyRateLimitError:
            if allow_stale_on_rate_limit and stale_payload is not None:
                if self.monitoring_service is not None:
                    self.monitoring_service.record_cache_hit()
                return stale_payload
            raise

        self.set(cache_key, payload, ttl_seconds=ttl_seconds, source_endpoint=source_endpoint)
        return payload

    def _get_row(self, cache_key: str) -> SpotifyApiCache | None:
        return SpotifyApiCache.query.filter_by(cache_scope=self.cache_scope, cache_key=cache_key).first()

    @staticmethod
    def _deserialize_payload(payload_json: str) -> Any | None:
        try:
            return json.loads(payload_json or "null")
        except json.JSONDecodeError:
            return None
