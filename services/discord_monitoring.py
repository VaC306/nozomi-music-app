from __future__ import annotations

import json
import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import requests
from flask import Flask

from models import AppRuntimeState, SpotifyMonitoringEvent, SpotifyUser, db


logger = logging.getLogger(__name__)

_SENSITIVE_KEYS = {"access_token", "refresh_token", "client_secret", "database_url"}
_CURRENT_OPERATION: ContextVar["MonitoringOperation | None"] = ContextVar("monitoring_operation", default=None)


@dataclass
class MonitoringOperation:
    operation_name: str
    spotify_user_id: str
    display_name: str
    started_at: datetime = field(default_factory=datetime.utcnow)
    spotify_calls: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    endpoints: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        return max((datetime.utcnow() - self.started_at).total_seconds(), 0.0)


class DiscordMonitoringService:
    DAILY_SUMMARY_STATE_KEY = "discord_daily_summary_last_sent_at"

    def __init__(self, app: Flask) -> None:
        self.app = app

    @property
    def enabled(self) -> bool:
        raw_value = str(self.app.config.get("ENABLE_DISCORD_MONITORING", "false")).strip().lower()
        return raw_value == "true" and bool(str(self.app.config.get("USER_RATE_WEBHOOK", "")).strip())

    @property
    def webhook_url(self) -> str:
        return str(self.app.config.get("USER_RATE_WEBHOOK", "")).strip()

    def start_operation(self, operation_name: str, spotify_user_id: str, display_name: str) -> Token:
        operation = MonitoringOperation(
            operation_name=operation_name,
            spotify_user_id=spotify_user_id.strip(),
            display_name=display_name.strip() or spotify_user_id.strip() or "Spotify User",
        )
        return _CURRENT_OPERATION.set(operation)

    def finish_operation(self, token: Token | None, send_dashboard_summary: bool = False) -> MonitoringOperation | None:
        operation = _CURRENT_OPERATION.get()
        if token is not None:
            _CURRENT_OPERATION.reset(token)
        if operation is None:
            return None
        if send_dashboard_summary:
            self.record_event(
                spotify_user_id=operation.spotify_user_id,
                display_name=operation.display_name,
                event_type="dashboard_refresh",
                operation_name=operation.operation_name,
                cache_hits=operation.cache_hits,
                cache_misses=operation.cache_misses,
                spotify_call_count=operation.spotify_calls,
                details={"duration_seconds": round(operation.duration_seconds, 2)},
            )
            self.send_dashboard_refresh_summary(operation)
        return operation

    def get_operation(self) -> MonitoringOperation | None:
        return _CURRENT_OPERATION.get()

    def record_cache_hit(self) -> None:
        operation = self.get_operation()
        if operation is not None:
            operation.cache_hits += 1

    def record_cache_miss(self) -> None:
        operation = self.get_operation()
        if operation is not None:
            operation.cache_misses += 1

    def record_spotify_call(self, endpoint: str) -> None:
        operation = self.get_operation()
        if operation is not None:
            operation.spotify_calls += 1
            if endpoint and endpoint not in operation.endpoints:
                operation.endpoints.append(endpoint)

    def record_event(
        self,
        spotify_user_id: str,
        display_name: str,
        event_type: str,
        operation_name: str = "",
        endpoint: str = "",
        cache_hits: int = 0,
        cache_misses: int = 0,
        spotify_call_count: int = 0,
        retry_after_seconds: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload = self._sanitize_payload(details or {})
        row = SpotifyMonitoringEvent()
        row.spotify_user_id = (spotify_user_id or "").strip()
        row.display_name = (display_name or spotify_user_id or "Spotify User").strip()
        row.event_type = event_type
        row.operation_name = operation_name
        row.endpoint = endpoint
        row.cache_hits = max(int(cache_hits or 0), 0)
        row.cache_misses = max(int(cache_misses or 0), 0)
        row.spotify_call_count = max(int(spotify_call_count or 0), 0)
        row.retry_after_seconds = retry_after_seconds
        row.details_json = json.dumps(payload)
        row.created_at = datetime.utcnow()
        db.session.add(row)
        db.session.commit()

    def send_dashboard_refresh_summary(self, operation: MonitoringOperation) -> None:
        total_cache_events = operation.cache_hits + operation.cache_misses
        hit_rate = (operation.cache_hits / total_cache_events * 100) if total_cache_events else 0.0
        content = (
            "Dashboard Refresh\n\n"
            f"Usuario: {operation.display_name}\n"
            f"Spotify User ID: {operation.spotify_user_id or '-'}\n\n"
            f"Spotify calls realizadas: {operation.spotify_calls}\n"
            f"Cache hits: {operation.cache_hits}\n"
            f"Cache misses: {operation.cache_misses}\n\n"
            f"Hit rate:\n{hit_rate:.2f} %\n\n"
            f"Duracion:\n{operation.duration_seconds:.2f} segundos\n\n"
            f"Timestamp UTC:\n{datetime.utcnow().isoformat()}Z"
        )
        self._post_message("📊 " + content)

    def send_spotify_429_alert(
        self,
        spotify_user_id: str,
        display_name: str,
        endpoint: str,
        retry_after_seconds: int | None,
        operation_calls: int,
    ) -> None:
        content = (
            "🚨 Spotify Rate Limit\n\n"
            f"Usuario: {display_name or spotify_user_id or 'Spotify User'}\n"
            f"Spotify User ID: {spotify_user_id or '-'}\n\n"
            f"Endpoint:\n{endpoint or '-'}\n\n"
            f"Retry-After:\n{retry_after_seconds if retry_after_seconds is not None else '-'}\n\n"
            f"Hora UTC:\n{datetime.utcnow().isoformat()}Z\n\n"
            f"Llamadas realizadas durante la operacion:\n{operation_calls}"
        )
        self._post_message(content)

    def send_user_blocked_alert(
        self,
        spotify_user_id: str,
        display_name: str,
        retry_after_seconds: int | None,
        blocked_until: datetime,
    ) -> None:
        content = (
            "🚨 Spotify User Blocked\n\n"
            f"Usuario: {display_name or spotify_user_id or 'Spotify User'}\n"
            f"Spotify User ID: {spotify_user_id or '-'}\n\n"
            "Motivo:\n429 Too Many Requests\n\n"
            f"Retry-After recibido:\n{retry_after_seconds if retry_after_seconds is not None else '-'}\n\n"
            f"Bloqueado hasta:\n{blocked_until.isoformat()}Z\n\n"
            f"Timestamp UTC:\n{datetime.utcnow().isoformat()}Z"
        )
        self._post_message(content)

    def send_forced_cache_alert(
        self,
        spotify_user_id: str,
        display_name: str,
        recent_429_count: int,
        forced_cache_until: datetime,
    ) -> None:
        content = (
            "🛡️ Forced Cache Mode Activated\n\n"
            f"Usuario: {display_name or spotify_user_id or 'Spotify User'}\n"
            f"Spotify User ID: {spotify_user_id or '-'}\n\n"
            f"429 recibidos ultimas 24h:\n{recent_429_count}\n\n"
            f"Modo cache activo hasta:\n{forced_cache_until.isoformat()}Z\n\n"
            f"Timestamp UTC:\n{datetime.utcnow().isoformat()}Z"
        )
        self._post_message(content)

    def send_user_quota_alert(
        self,
        spotify_user_id: str,
        display_name: str,
        refresh_count_24h: int,
        next_allowed_refresh_at: datetime,
    ) -> None:
        content = (
            "⚠️ User Quota Exceeded\n\n"
            f"Usuario: {display_name or spotify_user_id or 'Spotify User'}\n"
            f"Spotify User ID: {spotify_user_id or '-'}\n\n"
            "Accion:\nDashboard Refresh\n\n"
            f"Refresh usados ultimas 24h:\n{refresh_count_24h}\n\n"
            f"Proximo refresh permitido:\n{next_allowed_refresh_at.isoformat()}Z"
        )
        self._post_message(content)

    def maybe_send_daily_summary(self) -> None:
        if not self.enabled:
            return
        now = datetime.utcnow()
        state = AppRuntimeState.query.filter_by(key=self.DAILY_SUMMARY_STATE_KEY).first()
        if state and state.value:
            try:
                last_sent_at = datetime.fromisoformat(state.value)
            except ValueError:
                last_sent_at = None
            if last_sent_at and now - last_sent_at < timedelta(hours=24):
                return

        last_24h = now - timedelta(hours=24)
        events = SpotifyMonitoringEvent.query.filter(SpotifyMonitoringEvent.created_at >= last_24h).all()
        users_active = {event.spotify_user_id for event in events if event.spotify_user_id}
        spotify_calls = sum(int(event.spotify_call_count or 0) for event in events)
        cache_hits = sum(int(event.cache_hits or 0) for event in events)
        cache_misses = sum(int(event.cache_misses or 0) for event in events)
        total_cache_events = cache_hits + cache_misses
        hit_rate = (cache_hits / total_cache_events * 100) if total_cache_events else 0.0
        rate_limit_count = sum(1 for event in events if event.event_type == "spotify_429")
        blocked_users = SpotifyUser.query.filter(
            (SpotifyUser.rate_limited_until.isnot(None) & (SpotifyUser.rate_limited_until >= now))
            | (SpotifyUser.forced_cache_until.isnot(None) & (SpotifyUser.forced_cache_until >= now))
        ).count()

        content = (
            "📈 Nozomi Music Daily Stats\n\n"
            f"Usuarios activos: {len(users_active)}\n\n"
            f"Spotify calls totales:\n{spotify_calls}\n\n"
            f"Cache hits:\n{cache_hits}\n\n"
            f"Cache misses:\n{cache_misses}\n\n"
            f"Hit rate global:\n{hit_rate:.2f} %\n\n"
            f"429 recibidos:\n{rate_limit_count}\n\n"
            f"Usuarios bloqueados:\n{blocked_users}"
        )
        self._post_message(content)

        if state is None:
            state = AppRuntimeState()
            state.key = self.DAILY_SUMMARY_STATE_KEY
        state.value = now.isoformat()
        state.updated_at = now
        db.session.add(state)
        db.session.commit()

    def count_recent_429s(self, spotify_user_id: str, window_hours: int = 24) -> int:
        threshold = datetime.utcnow() - timedelta(hours=window_hours)
        return SpotifyMonitoringEvent.query.filter_by(
            spotify_user_id=spotify_user_id,
            event_type="spotify_429",
        ).filter(SpotifyMonitoringEvent.created_at >= threshold).count()

    def count_dashboard_refreshes_last_24h(self, spotify_user_id: str) -> int:
        threshold = datetime.utcnow() - timedelta(hours=24)
        return SpotifyMonitoringEvent.query.filter_by(
            spotify_user_id=spotify_user_id,
            event_type="dashboard_refresh",
        ).filter(SpotifyMonitoringEvent.created_at >= threshold).count()

    def get_next_dashboard_refresh_at(self, spotify_user_id: str, limit: int) -> datetime | None:
        threshold = datetime.utcnow() - timedelta(hours=24)
        rows = (
            SpotifyMonitoringEvent.query.filter_by(
                spotify_user_id=spotify_user_id,
                event_type="dashboard_refresh",
            )
            .filter(SpotifyMonitoringEvent.created_at >= threshold)
            .order_by(SpotifyMonitoringEvent.created_at.asc())
            .limit(limit)
            .all()
        )
        if len(rows) < limit:
            return None
        return rows[0].created_at + timedelta(hours=24)

    def _post_message(self, content: str) -> None:
        logger.info("Discord monitoring message queued")
        if not self.enabled:
            return
        try:
            response = requests.post(self.webhook_url, json={"content": content[:1900]}, timeout=10)
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Discord monitoring webhook failed: %s", str(exc).strip())

    @classmethod
    def _sanitize_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        safe_payload: dict[str, Any] = {}
        for key, value in payload.items():
            normalized_key = str(key).strip().lower()
            if normalized_key in _SENSITIVE_KEYS:
                continue
            if isinstance(value, dict):
                safe_payload[str(key)] = cls._sanitize_payload(value)
            elif isinstance(value, list):
                safe_payload[str(key)] = [cls._sanitize_payload(item) if isinstance(item, dict) else item for item in value]
            else:
                safe_payload[str(key)] = value
        return safe_payload
