from __future__ import annotations

from datetime import datetime

from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()


class SpotifyUser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    spotify_user_id = db.Column(db.String(120), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(255), nullable=False, default="Spotify User")
    email = db.Column(db.String(255), nullable=False, default="")
    profile_url = db.Column(db.Text, nullable=False, default="")
    image_url = db.Column(db.Text, nullable=False, default="")
    access_token = db.Column(db.Text, nullable=False, default="")
    refresh_token = db.Column(db.Text, nullable=False, default="")
    token_expires_at = db.Column(db.Integer, nullable=False, default=0)
    client_id = db.Column(db.Text, nullable=False, default="")
    client_secret = db.Column(db.Text, nullable=False, default="")
    redirect_uri = db.Column(db.Text, nullable=False, default="")
    rate_limited_until = db.Column(db.DateTime, nullable=True, index=True)
    forced_cache_until = db.Column(db.DateTime, nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class ArtistGenresCache(db.Model):
    __tablename__ = "artist_genres_cache"

    id = db.Column(db.Integer, primary_key=True)
    artist_id = db.Column(db.String(120), unique=True, nullable=False, index=True)
    artist_name = db.Column(db.String(255), nullable=False, default="")
    genres_json = db.Column(db.Text, nullable=False, default="[]")
    popularity = db.Column(db.Integer, nullable=False, default=0)
    fetched_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class SpotifyApiCache(db.Model):
    __tablename__ = "spotify_api_cache"

    id = db.Column(db.Integer, primary_key=True)
    cache_scope = db.Column(db.String(255), nullable=False, index=True, default="app")
    cache_key = db.Column(db.String(512), nullable=False, index=True)
    source_endpoint = db.Column(db.String(255), nullable=False, default="")
    payload_json = db.Column(db.Text, nullable=False, default="null")
    fetched_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("cache_scope", "cache_key", name="uq_spotify_api_cache_scope_key"),
    )


class DashboardTopSnapshot(db.Model):
    __tablename__ = "dashboard_top_snapshot"

    id = db.Column(db.Integer, primary_key=True)
    spotify_user_id = db.Column(db.String(120), nullable=False, index=True)
    snapshot_type = db.Column(db.String(20), nullable=False, index=True)
    time_range = db.Column(db.String(20), nullable=False, index=True)
    payload_json = db.Column(db.Text, nullable=False, default="[]")
    fetched_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint(
            "spotify_user_id",
            "snapshot_type",
            "time_range",
            name="uq_dashboard_top_snapshot_user_type_range",
        ),
    )


class SpotifyMonitoringEvent(db.Model):
    __tablename__ = "spotify_monitoring_event"

    id = db.Column(db.Integer, primary_key=True)
    spotify_user_id = db.Column(db.String(120), nullable=False, index=True, default="")
    display_name = db.Column(db.String(255), nullable=False, default="Spotify User")
    event_type = db.Column(db.String(60), nullable=False, index=True)
    operation_name = db.Column(db.String(120), nullable=False, default="")
    endpoint = db.Column(db.String(255), nullable=False, default="")
    cache_hits = db.Column(db.Integer, nullable=False, default=0)
    cache_misses = db.Column(db.Integer, nullable=False, default=0)
    spotify_call_count = db.Column(db.Integer, nullable=False, default=0)
    retry_after_seconds = db.Column(db.Integer, nullable=True)
    details_json = db.Column(db.Text, nullable=False, default="{}")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)


class AppRuntimeState(db.Model):
    __tablename__ = "app_runtime_state"

    key = db.Column(db.String(120), primary_key=True)
    value = db.Column(db.Text, nullable=False, default="")
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
