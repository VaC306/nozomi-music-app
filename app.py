from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from dotenv import dotenv_values, load_dotenv
from flask import Flask, flash, redirect, render_template, request, send_file, session, url_for
import requests
from sqlalchemy import inspect, text

from models import SpotifyUser, db
from services.dashboard_report import DashboardReportBuilder
from services.discord_monitoring import DiscordMonitoringService
from services.playlist_enhancer import PlaylistEnhancer
from services.playlist_manager import PlaylistImportError, PlaylistManager
from services.personal_library_service import PersonalLibraryService
from services.prompt_generator import PromptGenerator, PromptGeneratorError
from services.recommender import Recommender
from services.spotify_client import SpotifyAuthError, SpotifyClient, SpotifyClientError, SpotifyRateLimitError
from services.stats_service import DASHBOARD_PREVIEW_ITEMS_LIMIT, StatsService, TIME_RANGE_LABELS


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DOTENV_VALUES = {key: (value or "").strip() for key, value in dotenv_values(BASE_DIR / ".env").items()}
logging.basicConfig(level=logging.INFO)


def is_running_on_railway() -> bool:
    return bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"))


def get_config_value(key: str, default: str = "") -> str:
    if not is_running_on_railway() and DOTENV_VALUES.get(key, "").strip():
        return DOTENV_VALUES[key].strip()
    return os.getenv(key, default).strip()


def create_app() -> Flask:
    def ensure_artist_cache_schema() -> None:
        inspector = inspect(db.engine)
        if "artist_genres_cache" not in inspector.get_table_names():
            return

        existing_columns = {column["name"] for column in inspector.get_columns("artist_genres_cache")}
        statements: list[str] = []
        dialect_name = db.engine.dialect.name.lower()

        if "popularity" not in existing_columns:
            statements.append("ALTER TABLE artist_genres_cache ADD COLUMN popularity INTEGER NOT NULL DEFAULT 0")

        if "fetched_at" not in existing_columns:
            timestamp_type = "TIMESTAMP" if dialect_name == "postgresql" else "DATETIME"
            if dialect_name == "sqlite":
                statements.append(f"ALTER TABLE artist_genres_cache ADD COLUMN fetched_at {timestamp_type}")
            else:
                statements.append(
                    f"ALTER TABLE artist_genres_cache ADD COLUMN fetched_at {timestamp_type} DEFAULT CURRENT_TIMESTAMP"
                )
            if "updated_at" in existing_columns:
                statements.append(
                    "UPDATE artist_genres_cache SET fetched_at = updated_at WHERE fetched_at IS NULL"
                )

        if not statements:
            return

        with db.engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))

    def ensure_spotify_user_monitoring_schema() -> None:
        inspector = inspect(db.engine)
        if "spotify_user" not in inspector.get_table_names():
            return

        existing_columns = {column["name"] for column in inspector.get_columns("spotify_user")}
        statements: list[str] = []
        dialect_name = db.engine.dialect.name.lower()
        timestamp_type = "TIMESTAMP" if dialect_name == "postgresql" else "DATETIME"

        if "rate_limited_until" not in existing_columns:
            statements.append(f"ALTER TABLE spotify_user ADD COLUMN rate_limited_until {timestamp_type}")
        if "forced_cache_until" not in existing_columns:
            statements.append(f"ALTER TABLE spotify_user ADD COLUMN forced_cache_until {timestamp_type}")

        if not statements:
            return

        with db.engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))

    app = Flask(__name__)
    app.config["SECRET_KEY"] = get_config_value("FLASK_SECRET_KEY", secrets.token_hex(32))
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
    app.config["UPLOAD_FOLDER"] = str(BASE_DIR / "uploads")
    app.config["EXPORT_FOLDER"] = str(BASE_DIR / "exports")
    database_url = os.getenv("DATABASE_URL", "").strip()
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url or f"sqlite:///{BASE_DIR / 'nozomi.db'}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SPOTIFY_CLIENT_ID"] = get_config_value("SPOTIFY_CLIENT_ID")
    app.config["SPOTIFY_CLIENT_SECRET"] = get_config_value("SPOTIFY_CLIENT_SECRET")
    app.config["SPOTIFY_REDIRECT_URI"] = get_config_value("SPOTIFY_REDIRECT_URI")
    app.config["USER_REQUEST_WEBHOOK"] = get_config_value("USER_REQUEST_WEBHOOK")
    app.config["USER_RATE_WEBHOOK"] = get_config_value("USER_RATE_WEBHOOK")
    app.config["ENABLE_DISCORD_MONITORING"] = get_config_value("ENABLE_DISCORD_MONITORING", "false")
    app.config["DASHBOARD_REFRESH_LIMIT_24H"] = int(get_config_value("DASHBOARD_REFRESH_LIMIT_24H", "12") or 12)
    app.config["SPOTIFY_SCOPES"] = [
        "playlist-modify-public",
        "playlist-modify-private",
        "playlist-read-private",
        "playlist-read-collaborative",
        "user-library-read",
        "user-follow-read",
        "user-top-read",
        "user-read-recently-played",
        "user-read-private",
    ]

    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
    Path(app.config["EXPORT_FOLDER"]).mkdir(parents=True, exist_ok=True)
    db.init_app(app)

    with app.app_context():
        db.create_all()
        ensure_artist_cache_schema()
        ensure_spotify_user_monitoring_schema()

    monitoring_service = DiscordMonitoringService(app)

    with app.app_context():
        monitoring_service.maybe_send_daily_summary()

    def load_env_values() -> dict[str, str]:
        return {
            "FLASK_SECRET_KEY": get_config_value("FLASK_SECRET_KEY"),
            "SPOTIFY_CLIENT_ID": get_config_value("SPOTIFY_CLIENT_ID"),
            "SPOTIFY_CLIENT_SECRET": get_config_value("SPOTIFY_CLIENT_SECRET"),
            "SPOTIFY_REDIRECT_URI": get_config_value("SPOTIFY_REDIRECT_URI"),
        }

    def is_local_request_host() -> bool:
        return request.host.split(":", 1)[0].lower() in {"127.0.0.1", "localhost"}

    def get_effective_redirect_uri() -> str:
        configured_redirect = app.config["SPOTIFY_REDIRECT_URI"].strip()
        suggested_redirect = get_suggested_redirect_uri().strip()
        if is_local_request_host() and configured_redirect and _is_local_redirect_uri(configured_redirect):
            return configured_redirect
        if is_local_request_host() and not configured_redirect:
            return suggested_redirect
        return configured_redirect

    def get_current_spotify_user_identity() -> tuple[str, str]:
        spotify_user = session.get("spotify_user", {}) or {}
        spotify_user_id = str(spotify_user.get("id", "")).strip()
        display_name = str(spotify_user.get("display_name", "")).strip() or spotify_user_id or "Spotify User"
        return spotify_user_id, display_name

    def format_utc_label(value: datetime | None) -> str:
        if value is None:
            return "-"
        return value.strftime("%Y-%m-%d %H:%M:%S UTC")

    def build_spotify_protection_state() -> dict[str, Any]:
        spotify_user_id, display_name = get_current_spotify_user_identity()
        state = {
            "spotify_user_id": spotify_user_id,
            "display_name": display_name,
            "rate_limited_until": None,
            "forced_cache_until": None,
            "rate_limited_active": False,
            "forced_cache_active": False,
            "cache_only_active": False,
            "message": "",
            "remaining_seconds": 0,
            "until_label": "-",
        }
        if not spotify_user_id:
            return state

        user = SpotifyUser.query.filter_by(spotify_user_id=spotify_user_id).first()
        if user is None:
            return state

        now = datetime.utcnow()
        forced_cache_until = user.forced_cache_until if user.forced_cache_until and user.forced_cache_until > now else None
        rate_limited_until = user.rate_limited_until if user.rate_limited_until and user.rate_limited_until > now else None

        if user.forced_cache_until and forced_cache_until is None:
            user.forced_cache_until = None
            db.session.add(user)
            db.session.commit()
        if user.rate_limited_until and rate_limited_until is None:
            user.rate_limited_until = None
            db.session.add(user)
            db.session.commit()

        state["rate_limited_until"] = rate_limited_until
        state["forced_cache_until"] = forced_cache_until
        state["rate_limited_active"] = rate_limited_until is not None
        state["forced_cache_active"] = forced_cache_until is not None
        state["cache_only_active"] = state["rate_limited_active"] or state["forced_cache_active"]

        active_until = forced_cache_until or rate_limited_until
        if active_until is not None:
            state["remaining_seconds"] = max(int((active_until - now).total_seconds()), 0)
            state["until_label"] = format_utc_label(active_until)

        if forced_cache_until is not None:
            state["message"] = (
                "Se ha activado el modo cache temporal para proteger la aplicacion frente a los limites de Spotify. "
                f"Podras volver a realizar actualizaciones el {format_utc_label(forced_cache_until)}."
            )
        elif rate_limited_until is not None:
            state["message"] = (
                "Spotify ha limitado temporalmente las solicitudes para esta cuenta. Puedes seguir utilizando los datos en cache. "
                f"Proximo intento permitido: {format_utc_label(rate_limited_until)}."
            )
        return state

    def enforce_spotify_protection() -> None:
        state = build_spotify_protection_state()
        if state["cache_only_active"]:
            raise SpotifyRateLimitError(state["message"], blocked_until=state["forced_cache_until"] or state["rate_limited_until"])

    def handle_spotify_rate_limit(endpoint: str, retry_after_seconds: int | None) -> datetime | None:
        spotify_user_id, display_name = get_current_spotify_user_identity()
        if not spotify_user_id:
            operation = monitoring_service.get_operation()
            monitoring_service.send_spotify_429_alert(
                spotify_user_id=spotify_user_id,
                display_name=display_name,
                endpoint=endpoint,
                retry_after_seconds=retry_after_seconds,
                operation_calls=operation.spotify_calls if operation is not None else 0,
            )
            return None

        user = SpotifyUser.query.filter_by(spotify_user_id=spotify_user_id).first()
        if user is None:
            return None

        now = datetime.utcnow()
        wait_seconds = max(int(retry_after_seconds or 0), 3600)
        blocked_until = now + timedelta(seconds=wait_seconds)
        user.rate_limited_until = blocked_until
        db.session.add(user)
        db.session.commit()

        operation = monitoring_service.get_operation()
        monitoring_service.record_event(
            spotify_user_id=spotify_user_id,
            display_name=display_name,
            event_type="spotify_429",
            operation_name=operation.operation_name if operation is not None else "",
            endpoint=endpoint,
            cache_hits=operation.cache_hits if operation is not None else 0,
            cache_misses=operation.cache_misses if operation is not None else 0,
            spotify_call_count=operation.spotify_calls if operation is not None else 0,
            retry_after_seconds=retry_after_seconds,
            details={"blocked_until": blocked_until.isoformat()},
        )
        monitoring_service.send_spotify_429_alert(
            spotify_user_id=spotify_user_id,
            display_name=display_name,
            endpoint=endpoint,
            retry_after_seconds=retry_after_seconds,
            operation_calls=operation.spotify_calls if operation is not None else 0,
        )
        monitoring_service.send_user_blocked_alert(
            spotify_user_id=spotify_user_id,
            display_name=display_name,
            retry_after_seconds=retry_after_seconds,
            blocked_until=blocked_until,
        )

        recent_429_count = monitoring_service.count_recent_429s(spotify_user_id, window_hours=24)
        if recent_429_count >= 2:
            forced_cache_until = now + timedelta(hours=24)
            if not user.forced_cache_until or user.forced_cache_until < forced_cache_until:
                user.forced_cache_until = forced_cache_until
                db.session.add(user)
                db.session.commit()
                monitoring_service.record_event(
                    spotify_user_id=spotify_user_id,
                    display_name=display_name,
                    event_type="forced_cache_activated",
                    operation_name=operation.operation_name if operation is not None else "",
                    endpoint=endpoint,
                    retry_after_seconds=retry_after_seconds,
                    details={"forced_cache_until": forced_cache_until.isoformat(), "recent_429_count": recent_429_count},
                )
                monitoring_service.send_forced_cache_alert(
                    spotify_user_id=spotify_user_id,
                    display_name=display_name,
                    recent_429_count=recent_429_count,
                    forced_cache_until=forced_cache_until,
                )
        return blocked_until

    def get_spotify_client(redirect_uri: str | None = None) -> SpotifyClient:
        spotify_user_id, display_name = get_current_spotify_user_identity()
        return SpotifyClient(
            client_id=app.config["SPOTIFY_CLIENT_ID"],
            client_secret=app.config["SPOTIFY_CLIENT_SECRET"],
            redirect_uri=(redirect_uri or get_effective_redirect_uri()),
            scopes=app.config["SPOTIFY_SCOPES"],
            spotify_user_id=spotify_user_id,
            display_name=display_name,
            request_guard=enforce_spotify_protection if spotify_user_id else None,
            spotify_call_listener=monitoring_service.record_spotify_call,
            rate_limit_listener=handle_spotify_rate_limit if spotify_user_id else None,
        )

    def get_suggested_redirect_uri() -> str:
        return url_for("callback", _external=True)

    @staticmethod
    def _is_local_redirect_uri(value: str) -> bool:
        if not value.strip():
            return False
        parsed = urlparse(value)
        hostname = (parsed.hostname or "").lower()
        return hostname in {"127.0.0.1", "localhost"}

    def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view)
        def wrapped_view(*args: Any, **kwargs: Any) -> Any:
            if not session.get("spotify_user"):
                flash("Necesitas conectar Spotify para usar esta seccion.", "warning")
                return redirect(url_for("login"))
            return view(*args, **kwargs)

        return wrapped_view

    def _store_token_session(token_data: dict[str, Any]) -> None:
        session["spotify_access_token"] = token_data.get("access_token", "")
        session["spotify_refresh_token"] = token_data.get(
            "refresh_token",
            session.get("spotify_refresh_token", ""),
        )
        session["spotify_expires_at"] = token_data.get("expires_at", 0)
        scope_value = token_data.get("scope", "")
        session["spotify_scopes"] = scope_value.split() if scope_value else []

        spotify_user_id = session.get("spotify_user", {}).get("id", "")
        if spotify_user_id:
            stored_user = SpotifyUser.query.filter_by(spotify_user_id=spotify_user_id).first()
            if stored_user:
                stored_user.access_token = session["spotify_access_token"]
                stored_user.refresh_token = session["spotify_refresh_token"]
                stored_user.token_expires_at = int(session["spotify_expires_at"] or 0)
                db.session.add(stored_user)
                db.session.commit()

    def has_spotify_config() -> bool:
        return bool(
            app.config["SPOTIFY_CLIENT_ID"]
            and app.config["SPOTIFY_CLIENT_SECRET"]
            and app.config["SPOTIFY_REDIRECT_URI"]
        )

    def ensure_spotify_session() -> SpotifyClient:
        client = get_spotify_client()
        access_token = session.get("spotify_access_token")
        refresh_token = session.get("spotify_refresh_token")
        expires_at = int(session.get("spotify_expires_at", 0) or 0)

        if not access_token:
            raise SpotifyAuthError("No hay una sesion activa de Spotify.")

        protection_state = build_spotify_protection_state()
        if protection_state["cache_only_active"]:
            return client

        if client.is_token_expired(expires_at):
            if not refresh_token:
                raise SpotifyAuthError("La sesion de Spotify expiro. Vuelve a iniciar sesion.")
            token_data = client.refresh_access_token(refresh_token)
            _store_token_session(token_data)
        return client

    def ensure_required_spotify_scopes(required_scopes: list[str], feature_name: str) -> None:
        granted_scopes = set(session.get("spotify_scopes") or [])
        missing_scopes = [scope for scope in required_scopes if scope not in granted_scopes]
        if not missing_scopes:
            return
        raise SpotifyAuthError(
            (
                f"Tu sesion de Spotify no tiene los permisos necesarios para usar {feature_name}. "
                "Cierra sesion y vuelve a entrar para renovar los permisos."
            )
        )

    def get_token_status() -> dict[str, Any]:
        access_token = session.get("spotify_access_token", "")
        refresh_token = session.get("spotify_refresh_token", "")
        expires_at = int(session.get("spotify_expires_at", 0) or 0)

        status = {
            "has_access_token": bool(access_token),
            "has_refresh_token": bool(refresh_token),
            "expires_at": expires_at,
            "expires_label": datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M:%S") if expires_at else "-",
            "is_expired": True,
            "is_valid": False,
            "message": "No hay sesion activa.",
        }

        if not access_token:
            return status

        client = get_spotify_client()
        status["is_expired"] = client.is_token_expired(expires_at)

        try:
            user = client.get_current_user(access_token)
            session["spotify_user"] = {
                "id": user.get("id", ""),
                "display_name": user.get("display_name") or user.get("id") or "Spotify User",
                "email": user.get("email", ""),
                "profile_url": user.get("external_urls", {}).get("spotify", ""),
                "image_url": (user.get("images") or [{}])[0].get("url", ""),
            }
            status["is_valid"] = True
            status["message"] = "Token y sesion validos. Spotify responde correctamente."
        except SpotifyClientError as exc:
            status["message"] = str(exc)

        return status

    def get_playlist_cache_mode() -> str:
        cache_mode = str(session.get("playlist_cache_mode", "cache_only")).strip().lower()
        return cache_mode if cache_mode in {"cache_only", "normal"} else "cache_only"

    def start_dashboard_operation() -> Any:
        spotify_user_id, display_name = get_current_spotify_user_identity()
        return monitoring_service.start_operation("dashboard_refresh", spotify_user_id, display_name)

    def can_force_dashboard_refresh() -> tuple[bool, datetime | None, int]:
        spotify_user_id, display_name = get_current_spotify_user_identity()
        if not spotify_user_id:
            return True, None, 0
        limit = int(app.config["DASHBOARD_REFRESH_LIMIT_24H"] or 12)
        refresh_count = monitoring_service.count_dashboard_refreshes_last_24h(spotify_user_id)
        next_allowed_refresh_at = monitoring_service.get_next_dashboard_refresh_at(spotify_user_id, limit)
        if refresh_count < limit or next_allowed_refresh_at is None:
            return True, None, refresh_count
        monitoring_service.record_event(
            spotify_user_id=spotify_user_id,
            display_name=display_name,
            event_type="dashboard_quota_exceeded",
            operation_name="dashboard_refresh",
            details={"refresh_count_24h": refresh_count, "next_allowed_refresh_at": next_allowed_refresh_at.isoformat()},
        )
        monitoring_service.send_user_quota_alert(
            spotify_user_id=spotify_user_id,
            display_name=display_name,
            refresh_count_24h=refresh_count,
            next_allowed_refresh_at=next_allowed_refresh_at,
        )
        return False, next_allowed_refresh_at, refresh_count

    def send_developer_request(username: str, email: str) -> None:
        webhook_url = app.config["USER_REQUEST_WEBHOOK"].strip()
        if not webhook_url:
            raise RuntimeError("No hay webhook configurado para enviar la solicitud.")

        payload = {
            "embeds": [
                {
                    "title": "Nueva solicitud Spotify Developers",
                    "color": 15179945,
                    "fields": [
                        {"name": "Developer user", "value": username, "inline": True},
                        {"name": "Correo", "value": email, "inline": True},
                        {"name": "Origen", "value": request.url_root.rstrip("/")},
                    ],
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                }
            ]
        }

        try:
            response = requests.post(webhook_url, json=payload, timeout=10)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError("No se pudo enviar la solicitud a Discord.") from exc

    @app.context_processor
    def inject_globals() -> dict[str, Any]:
        return {
            "spotify_user": session.get("spotify_user"),
            "current_year": datetime.now().year,
            "request_endpoint": request.endpoint,
            "suggested_redirect_uri": get_suggested_redirect_uri(),
            "spotify_protection_state": build_spotify_protection_state(),
        }

    @app.route("/")
    def index() -> str:
        features = [
            {
                "title": "Login Spotify",
                "description": "Conecta tu cuenta y gestiona la sesion desde la web.",
                "status": "Activo",
                "url": url_for("login"),
            },
            {
                "title": "Crear playlists desde TXT",
                "description": "Importa un TXT, valida lineas y crea la playlist directamente en Spotify.",
                "status": "Activo",
                "url": url_for("create_playlist"),
            },
            {
                "title": "Exportar playlists",
                "description": "Busca playlists propias o colaborativas y exportalas a TXT listo para descargar.",
                "status": "Activo",
                "url": url_for("export_playlist"),
            },
            {
                "title": "Biblioteca personal",
                "description": "Consulta tus canciones guardadas, filtralas y exportalas a TXT desde una sola vista.",
                "status": "Activo",
                "url": url_for("personal_library"),
            },
            {
                "title": "Mejorador de playlists",
                "description": "Analiza artistas, generos y popularidad para sugerir que anadir, quitar y reordenar.",
                "status": "Activo",
                "url": url_for("playlist_enhancer"),
            },
            {
                "title": "Recomendaciones por genero",
                "description": "Explora canciones por genero con fallback automatico si Spotify limita el endpoint principal.",
                "status": "Activo",
                "url": url_for("recommendations"),
            },
            {
                "title": "Prompt generator",
                "description": "Crea prompts musicales listos para copiar en cualquier IA, sin integrarla dentro de la app.",
                "status": "Activo",
                "url": url_for("prompt_generator"),
            },
            {
                "title": "Dashboard",
                "description": "Resumen visual de top tracks, artists, generos y actividad reciente.",
                "status": "Activo",
                "url": url_for("dashboard"),
            },
        ]
        if session.get("spotify_user"):
            primary_url = url_for("create_playlist")
            primary_label = "Crear desde TXT"
            secondary_url = url_for("export_playlist")
            secondary_label = "Exportar"
        elif has_spotify_config():
            primary_url = url_for("login")
            primary_label = "Entrar con Spotify"
            secondary_url = url_for("profile")
            secondary_label = "Ver perfil"
        else:
            primary_url = url_for("profile")
            primary_label = "Configurar app"
            secondary_url = url_for("prompt_generator")
            secondary_label = "Abrir prompts"

        return render_template(
            "index.html",
            features=features,
            signed_in_feature_cards=[features[1], features[2], features[3], features[4]],
            public_feature_cards=[features[0], features[6]],
            primary_url=primary_url,
            primary_label=primary_label,
            secondary_url=secondary_url,
            secondary_label=secondary_label,
        )

    @app.route("/login")
    def login() -> Any:
        if (
            not app.config["SPOTIFY_CLIENT_ID"]
            or not app.config["SPOTIFY_CLIENT_SECRET"]
            or not app.config["SPOTIFY_REDIRECT_URI"]
        ):
            flash(
                "Faltan variables de entorno de Spotify. En local se cargan desde `.env`; en deploy, desde Railway.",
                "danger",
            )
            return render_template("login.html", auth_url=None, effective_redirect_uri=get_effective_redirect_uri())

        current_host_is_local = is_local_request_host()
        configured_redirect = app.config["SPOTIFY_REDIRECT_URI"].strip()
        if not current_host_is_local and _is_local_redirect_uri(configured_redirect):
            flash(
                "La variable SPOTIFY_REDIRECT_URI sigue apuntando a localhost. Actualizala en Railway con la URL publica actual antes de iniciar sesion.",
                "warning",
            )
            return redirect(url_for("profile"))

        effective_redirect_uri = get_effective_redirect_uri()
        client = get_spotify_client(effective_redirect_uri)
        state = secrets.token_urlsafe(24)
        session["spotify_oauth_state"] = state
        session["spotify_oauth_redirect_uri"] = effective_redirect_uri
        auth_url = client.build_authorization_url(state)

        if request.args.get("start") == "1":
            return redirect(auth_url)

        return render_template("login.html", auth_url=auth_url, effective_redirect_uri=effective_redirect_uri)

    @app.route("/callback")
    def callback() -> Any:
        error = request.args.get("error")
        if error:
            if error == "server_error":
                flash(
                    "Spotify devolvio `server_error`. En local suele indicar que la Redirect URI registrada no coincide exactamente. Usa `http://127.0.0.1:8888/callback` en tu entorno y en Spotify Developers.",
                    "danger",
                )
                return redirect(url_for("profile"))
            flash(f"Spotify devolvio un error de autorizacion: {error}", "danger")
            return redirect(url_for("login"))

        state = request.args.get("state", "")
        expected_state = session.get("spotify_oauth_state", "")
        if not state or state != expected_state:
            flash(
                "No se pudo validar la solicitud OAuth. Intenta iniciar sesion otra vez.",
                "danger",
            )
            return redirect(url_for("login"))

        code = request.args.get("code", "")
        if not code:
            flash("Spotify no devolvio un codigo de autorizacion valido.", "danger")
            return redirect(url_for("login"))

        redirect_uri = str(session.get("spotify_oauth_redirect_uri", get_effective_redirect_uri()))
        client = get_spotify_client(redirect_uri)
        try:
            token_data = client.exchange_code_for_token(code)
            _store_token_session(token_data)
            user = client.get_current_user(session["spotify_access_token"])
        except (SpotifyAuthError, SpotifyClientError) as exc:
            flash(str(exc), "danger")
            return redirect(url_for("login"))

        session["spotify_user"] = {
            "id": user.get("id", ""),
            "display_name": user.get("display_name") or user.get("id") or "Spotify User",
            "email": user.get("email", ""),
            "profile_url": user.get("external_urls", {}).get("spotify", ""),
            "image_url": (user.get("images") or [{}])[0].get("url", ""),
        }
        stored_user = SpotifyUser.query.filter_by(spotify_user_id=session["spotify_user"]["id"]).first()
        if not stored_user:
            stored_user = SpotifyUser()
            stored_user.spotify_user_id = session["spotify_user"]["id"]
        stored_user.display_name = session["spotify_user"]["display_name"]
        stored_user.email = session["spotify_user"]["email"]
        stored_user.profile_url = session["spotify_user"]["profile_url"]
        stored_user.image_url = session["spotify_user"]["image_url"]
        stored_user.access_token = session.get("spotify_access_token", "")
        stored_user.refresh_token = session.get("spotify_refresh_token", "")
        stored_user.token_expires_at = int(session.get("spotify_expires_at", 0) or 0)
        stored_user.client_id = app.config["SPOTIFY_CLIENT_ID"] or ""
        stored_user.client_secret = app.config["SPOTIFY_CLIENT_SECRET"] or ""
        stored_user.redirect_uri = redirect_uri or ""
        db.session.add(stored_user)
        db.session.commit()
        session.pop("spotify_oauth_state", None)
        session.pop("spotify_oauth_redirect_uri", None)
        flash("Sesion iniciada correctamente con Spotify.", "success")
        return redirect(url_for("index"))

    @app.route("/logout")
    def logout() -> Any:
        for key in [
            "spotify_access_token",
            "spotify_refresh_token",
            "spotify_expires_at",
            "spotify_scopes",
            "spotify_user",
            "spotify_oauth_state",
        ]:
            session.pop(key, None)
        flash("Sesion cerrada correctamente.", "info")
        return redirect(url_for("index"))

    @app.route("/create-playlist", methods=["GET", "POST"])
    @login_required
    def create_playlist() -> Any:
        try:
            spotify_client = ensure_spotify_session()
        except SpotifyAuthError as exc:
            flash(str(exc), "warning")
            return redirect(url_for("login"))

        manager = PlaylistManager(
            Path(app.config["UPLOAD_FOLDER"]),
            Path(app.config["EXPORT_FOLDER"]),
            user_id=str(session.get("spotify_user", {}).get("id", "")),
        )
        result = None

        if request.method == "POST":
            playlist_name = request.form.get("playlist_name", "")
            upload = request.files.get("playlist_file")
            try:
                txt_path = manager.save_uploaded_txt(upload)
                result = manager.create_playlist_from_txt(
                    spotify_client=spotify_client,
                    access_token=session["spotify_access_token"],
                    playlist_name=playlist_name,
                    txt_path=txt_path,
                )
                flash("Playlist creada correctamente en Spotify.", "success")
            except (PlaylistImportError, SpotifyClientError) as exc:
                flash(str(exc), "danger")

        return render_template(
            "create_playlist.html",
            upload_help=manager.get_upload_help(),
            result=result,
        )

    @app.route("/export-playlist", methods=["GET", "POST"])
    @login_required
    def export_playlist() -> Any:
        try:
            spotify_client = ensure_spotify_session()
            ensure_required_spotify_scopes(
                ["playlist-read-private", "playlist-read-collaborative"],
                "la exportacion de playlists",
            )
        except SpotifyAuthError as exc:
            flash(str(exc), "warning")
            return redirect(url_for("login"))

        manager = PlaylistManager(
            Path(app.config["UPLOAD_FOLDER"]),
            Path(app.config["EXPORT_FOLDER"]),
            user_id=str(session.get("spotify_user", {}).get("id", "")),
        )
        current_user_id = str(session.get("spotify_user", {}).get("id", ""))
        cache_mode = get_playlist_cache_mode()
        refresh_cache = request.method == "GET" and request.args.get("refresh_cache", "").strip() == "1"
        protection_state = build_spotify_protection_state()
        use_cache_only = cache_mode == "cache_only" or protection_state["cache_only_active"]
        search_query = request.form.get("playlist_query", "") if request.method == "POST" else ""
        selected_playlist_id = request.form.get("playlist_id", "") if request.method == "POST" else ""

        if refresh_cache and protection_state["cache_only_active"]:
            flash(protection_state["message"], "warning")
            refresh_cache = False
        if refresh_cache:
            manager.clear_exportable_playlists_cache()

        try:
            all_playlists = manager.list_exportable_playlists(
                spotify_client=spotify_client,
                access_token=session["spotify_access_token"],
                current_user_id=current_user_id,
                prefer_cached=use_cache_only and not refresh_cache,
                allow_stale=use_cache_only,
            )
        except SpotifyClientError as exc:
            all_playlists = manager.get_cached_exportable_playlists(current_user_id)
            flash(str(exc), "danger")
            if all_playlists:
                flash("Mostramos una copia en cache de tus playlists para que el selector no se vacie.", "warning")

        if search_query.strip():
            playlists = [
                playlist for playlist in all_playlists if search_query.strip().lower() in playlist.get("search_text", "")
            ]
        else:
            playlists = all_playlists

        result = None
        if request.method == "POST" and selected_playlist_id:
            selected_playlist = next(
                (playlist for playlist in all_playlists if playlist.get("id") == selected_playlist_id),
                None,
            )
            if not selected_playlist:
                flash("Selecciona una playlist valida para exportar.", "danger")
            else:
                try:
                    result = manager.export_playlist_to_txt(
                        spotify_client=spotify_client,
                        access_token=session["spotify_access_token"],
                        playlist=selected_playlist,
                        cache_only=use_cache_only,
                    )
                    flash("Playlist exportada correctamente a TXT.", "success")
                except (PlaylistImportError, SpotifyClientError) as exc:
                    flash(str(exc), "danger")

        return render_template(
            "export_playlist.html",
            playlists=playlists,
            result=result,
            search_query=search_query,
            selected_playlist_id=selected_playlist_id,
            cache_mode=cache_mode,
            protection_state=protection_state,
        )

    @app.route("/exports/<path:filename>")
    @login_required
    def download_export(filename: str) -> Any:
        file_path = Path(app.config["EXPORT_FOLDER"]) / filename
        if not file_path.exists() or not file_path.is_file():
            flash("No se encontro el archivo exportado solicitado.", "danger")
            return redirect(url_for("export_playlist"))
        return send_file(file_path, as_attachment=True, download_name=file_path.name)

    @app.route("/personal-library", methods=["GET", "POST"])
    @login_required
    def personal_library() -> Any:
        try:
            spotify_client = ensure_spotify_session()
            ensure_required_spotify_scopes(["user-library-read", "user-follow-read"], "tu biblioteca personal")
        except SpotifyAuthError as exc:
            flash(str(exc), "warning")
            return redirect(url_for("login"))

        library_service = PersonalLibraryService(
            Path(app.config["EXPORT_FOLDER"]),
            user_id=str(session.get("spotify_user", {}).get("id", "")),
        )
        selected_tab = PersonalLibraryService.normalize_tab(request.values.get("tab", "tracks"))
        cache_mode = get_playlist_cache_mode()
        refresh_cache = request.method == "GET" and request.args.get("refresh_cache", "").strip() == "1"
        protection_state = build_spotify_protection_state()
        use_cache_only = cache_mode == "cache_only" or protection_state["cache_only_active"]
        search_query = request.form.get("item_query", "") if request.method == "POST" else request.args.get("q", "")

        if refresh_cache and protection_state["cache_only_active"]:
            flash(protection_state["message"], "warning")
            refresh_cache = False
        if refresh_cache:
            library_service.clear_cache(selected_tab)

        try:
            all_items = library_service.list_items(
                tab=selected_tab,
                spotify_client=spotify_client,
                access_token=session["spotify_access_token"],
                prefer_cached=use_cache_only and not refresh_cache,
                allow_stale=use_cache_only,
                cache_only=use_cache_only,
                force_refresh=refresh_cache,
            )
        except (PlaylistImportError, SpotifyClientError) as exc:
            all_items = library_service.get_cached_items(selected_tab)
            flash(str(exc), "danger")
            if all_items:
                flash("Mostramos una copia en cache de tu biblioteca para que puedas seguir trabajando.", "warning")

        items = library_service.filter_items(all_items, search_query)
        summary = library_service.build_summary(selected_tab, all_items)
        filtered_summary = library_service.build_summary(selected_tab, items)
        tab_copy = library_service.get_tab_copy(selected_tab)
        result = None

        if request.method == "POST":
            try:
                result = library_service.export_items_to_txt(selected_tab, items)
                flash("Biblioteca exportada correctamente a TXT.", "success")
            except PlaylistImportError as exc:
                flash(str(exc), "danger")

        return render_template(
            "personal_library.html",
            items=items,
            summary=summary,
            filtered_summary=filtered_summary,
            search_query=search_query,
            result=result,
            cache_mode=cache_mode,
            protection_state=protection_state,
            selected_tab=selected_tab,
            tab_copy=tab_copy,
            library_tabs=[
                {"id": "tracks", "label": "Canciones"},
                {"id": "albums", "label": "Albumes"},
                {"id": "artists", "label": "Artistas"},
            ],
        )

    @app.route("/recommendations")
    @login_required
    def recommendations() -> Any:
        try:
            ensure_spotify_session()
        except SpotifyAuthError as exc:
            flash(str(exc), "warning")
            return redirect(url_for("login"))
        recommender = Recommender(get_spotify_client(), user_id=str(session.get("spotify_user", {}).get("id", "")))
        artist = request.args.get("artist", "")
        raw_limit = request.args.get("limit", "6").strip()
        limit = int(raw_limit) if raw_limit.isdigit() else 6
        limit = max(1, min(limit, 12))
        result = None

        if artist.strip():
            try:
                result = recommender.build_artist_discovery_result(
                    access_token=session["spotify_access_token"],
                    artist_name=artist,
                    limit=limit,
                )
                if result["warning"]:
                    flash(result["warning"], "warning")
                if not result["similar_artists"] and not result["similar_tracks"]:
                    flash("No encontramos resultados relacionados para ese artista en este momento.", "info")
            except SpotifyClientError as exc:
                message = str(exc).strip()
                if message.lower() == "forbidden":
                    flash(
                        "Spotify no permite usar este endpoint con la sesion actual. Probaremos otras rutas cuando haya datos disponibles.",
                        "warning",
                    )
                else:
                    flash(message, "danger")

        return render_template(
            "recommendations.html",
            sample_artists=recommender.get_sample_artists(),
            result=result,
            selected_artist=artist,
            selected_limit=limit,
        )

    @app.route("/playlist-enhancer", methods=["GET", "POST"])
    @login_required
    def playlist_enhancer() -> Any:
        try:
            spotify_client = ensure_spotify_session()
        except SpotifyAuthError as exc:
            flash(str(exc), "warning")
            return redirect(url_for("login"))

        manager = PlaylistManager(
            Path(app.config["UPLOAD_FOLDER"]),
            Path(app.config["EXPORT_FOLDER"]),
            user_id=str(session.get("spotify_user", {}).get("id", "")),
        )
        enhancer = PlaylistEnhancer(get_spotify_client(), user_id=str(session.get("spotify_user", {}).get("id", "")))
        current_user_id = str(session.get("spotify_user", {}).get("id", ""))
        cache_mode = get_playlist_cache_mode()
        refresh_cache = request.method == "GET" and request.args.get("refresh_cache", "").strip() == "1"
        protection_state = build_spotify_protection_state()
        use_cache_only = cache_mode == "cache_only" or protection_state["cache_only_active"]
        search_query = request.form.get("playlist_query", "") if request.method == "POST" else ""
        selected_playlist_id = request.form.get("playlist_id", "") if request.method == "POST" else ""

        if refresh_cache and protection_state["cache_only_active"]:
            flash(protection_state["message"], "warning")
            refresh_cache = False
        if refresh_cache:
            manager.clear_exportable_playlists_cache()

        try:
            all_playlists = manager.list_exportable_playlists(
                spotify_client=spotify_client,
                access_token=session["spotify_access_token"],
                current_user_id=current_user_id,
                prefer_cached=use_cache_only and not refresh_cache,
                allow_stale=use_cache_only,
            )
        except SpotifyClientError as exc:
            all_playlists = manager.get_cached_exportable_playlists(current_user_id)
            flash(str(exc), "danger")
            if all_playlists:
                flash("Mostramos una copia en cache de tus playlists para que el selector no se vacie.", "warning")

        if search_query.strip():
            playlists = [
                playlist for playlist in all_playlists if search_query.strip().lower() in playlist.get("search_text", "")
            ]
        else:
            playlists = all_playlists

        report = None
        if request.method == "POST" and selected_playlist_id:
            selected_playlist = next(
                (playlist for playlist in all_playlists if playlist.get("id") == selected_playlist_id),
                None,
            )
            if not selected_playlist:
                flash("Selecciona una playlist valida para analizar.", "danger")
            else:
                try:
                    report = enhancer.build_playlist_report_with_mode(
                        access_token=session["spotify_access_token"],
                        playlist=selected_playlist,
                        cache_only=use_cache_only,
                    )
                    flash("Playlist analizada correctamente.", "success")
                except SpotifyClientError as exc:
                    flash(str(exc), "danger")

        return render_template(
            "playlist_enhancer.html",
            playlists=playlists,
            report=report,
            search_query=search_query,
            selected_playlist_id=selected_playlist_id,
            cache_mode=cache_mode,
            protection_state=protection_state,
        )

    @app.route("/prompt-generator", methods=["GET", "POST"])
    def prompt_generator() -> Any:
        generator = PromptGenerator()
        result = None

        if request.method == "POST":
            try:
                result = generator.build_prompt(
                    prompt_type=request.form.get("prompt_type", ""),
                    genre=request.form.get("genre", ""),
                    mood=request.form.get("mood", ""),
                    references=request.form.get("references", ""),
                    goal=request.form.get("goal", ""),
                    constraints=request.form.get("constraints", ""),
                    output_language=request.form.get("output_language", "espanol"),
                )
                flash("Prompt generado correctamente. Ya puedes copiarlo y pegarlo donde quieras.", "success")
            except PromptGeneratorError as exc:
                flash(str(exc), "danger")

        return render_template(
            "prompt_generator.html",
            prompt_types=generator.list_prompt_types(),
            result=result,
        )

    @app.route("/dashboard")
    @login_required
    def dashboard() -> Any:
        try:
            ensure_spotify_session()
        except SpotifyAuthError as exc:
            flash(str(exc), "warning")
            return redirect(url_for("login"))

        selected_range = request.args.get("range", "short_term").strip()
        if selected_range not in TIME_RANGE_LABELS:
            selected_range = "short_term"
        cache_mode = get_playlist_cache_mode()
        refresh_cache = request.args.get("refresh_cache", "").strip() == "1"
        protection_state = build_spotify_protection_state()
        use_cache_only = cache_mode == "cache_only" or protection_state["cache_only_active"]

        if refresh_cache and protection_state["cache_only_active"]:
            flash(protection_state["message"], "warning")
            refresh_cache = False
        elif refresh_cache:
            refresh_allowed, next_allowed_refresh_at, refresh_count = can_force_dashboard_refresh()
            if not refresh_allowed:
                flash(
                    "Has superado la cuota temporal de recargas forzadas del dashboard. "
                    f"Llevas {refresh_count} refresh en 24h. Proximo refresh permitido: {format_utc_label(next_allowed_refresh_at)}.",
                    "warning",
                )
                refresh_cache = False

        stats_service = StatsService(
            get_spotify_client(),
            user_id=str(session.get("spotify_user", {}).get("id", "")),
            monitoring_service=monitoring_service,
        )
        operation_token = start_dashboard_operation() if refresh_cache else None
        operation_success = False
        try:
            snapshot = stats_service.build_snapshot_with_mode(
                session["spotify_access_token"],
                cache_only=use_cache_only and not refresh_cache,
                force_refresh=refresh_cache,
            )
            operation_success = refresh_cache
        except SpotifyClientError as exc:
            flash(str(exc), "warning")
            return redirect(url_for("index"))
        finally:
            if operation_token is not None:
                monitoring_service.finish_operation(operation_token, send_dashboard_summary=operation_success)
        return render_template(
            "dashboard.html",
            snapshot=snapshot,
            time_range_labels=TIME_RANGE_LABELS,
            selected_range=selected_range,
            preview_limit=DASHBOARD_PREVIEW_ITEMS_LIMIT,
            cache_mode=cache_mode,
            protection_state=protection_state,
        )

    @app.route("/dashboard/top/<item_type>")
    @login_required
    def dashboard_top_list(item_type: str) -> Any:
        try:
            ensure_spotify_session()
        except SpotifyAuthError as exc:
            flash(str(exc), "warning")
            return redirect(url_for("login"))

        selected_range = request.args.get("range", "short_term").strip()
        if selected_range not in TIME_RANGE_LABELS:
            selected_range = "short_term"
        if item_type not in {"tracks", "artists"}:
            flash("La lista solicitada no existe.", "warning")
            return redirect(url_for("dashboard", range=selected_range))
        cache_mode = get_playlist_cache_mode()
        refresh_cache = request.args.get("refresh_cache", "").strip() == "1"
        protection_state = build_spotify_protection_state()
        use_cache_only = cache_mode == "cache_only" or protection_state["cache_only_active"]

        if refresh_cache and protection_state["cache_only_active"]:
            flash(protection_state["message"], "warning")
            refresh_cache = False
        elif refresh_cache:
            refresh_allowed, next_allowed_refresh_at, refresh_count = can_force_dashboard_refresh()
            if not refresh_allowed:
                flash(
                    "Has superado la cuota temporal de recargas forzadas del dashboard. "
                    f"Llevas {refresh_count} refresh en 24h. Proximo refresh permitido: {format_utc_label(next_allowed_refresh_at)}.",
                    "warning",
                )
                refresh_cache = False

        stats_service = StatsService(
            get_spotify_client(),
            user_id=str(session.get("spotify_user", {}).get("id", "")),
            monitoring_service=monitoring_service,
        )
        operation_token = start_dashboard_operation() if refresh_cache else None
        operation_success = False
        try:
            snapshot = stats_service.build_snapshot_with_mode(
                session["spotify_access_token"],
                cache_only=use_cache_only and not refresh_cache,
                force_refresh=refresh_cache,
            )
            operation_success = refresh_cache
        except SpotifyClientError as exc:
            flash(str(exc), "warning")
            return redirect(url_for("dashboard", range=selected_range))
        finally:
            if operation_token is not None:
                monitoring_service.finish_operation(operation_token, send_dashboard_summary=operation_success)

        items = stats_service.get_top_items(item_type=item_type, time_range=selected_range)
        if not items:
            items = snapshot.top_tracks[selected_range] if item_type == "tracks" else snapshot.top_artists[selected_range]

        return render_template(
            "dashboard_top_list.html",
            snapshot=snapshot,
            items=items,
            item_type=item_type,
            selected_range=selected_range,
            time_range_labels=TIME_RANGE_LABELS,
            cache_mode=cache_mode,
            protection_state=protection_state,
        )

    @app.route("/dashboard/export")
    @login_required
    def dashboard_export() -> Any:
        try:
            ensure_spotify_session()
        except SpotifyAuthError as exc:
            flash(str(exc), "warning")
            return redirect(url_for("login"))

        selected_range = request.args.get("range", "short_term").strip()
        if selected_range not in TIME_RANGE_LABELS:
            selected_range = "short_term"
        cache_mode = get_playlist_cache_mode()
        refresh_cache = request.args.get("refresh_cache", "").strip() == "1"
        protection_state = build_spotify_protection_state()
        use_cache_only = cache_mode == "cache_only" or protection_state["cache_only_active"]

        if refresh_cache and protection_state["cache_only_active"]:
            flash(protection_state["message"], "warning")
            refresh_cache = False
        elif refresh_cache:
            refresh_allowed, next_allowed_refresh_at, refresh_count = can_force_dashboard_refresh()
            if not refresh_allowed:
                flash(
                    "Has superado la cuota temporal de recargas forzadas del dashboard. "
                    f"Llevas {refresh_count} refresh en 24h. Proximo refresh permitido: {format_utc_label(next_allowed_refresh_at)}.",
                    "warning",
                )
                refresh_cache = False

        stats_service = StatsService(
            get_spotify_client(),
            user_id=str(session.get("spotify_user", {}).get("id", "")),
            monitoring_service=monitoring_service,
        )
        operation_token = start_dashboard_operation() if refresh_cache else None
        operation_success = False
        try:
            snapshot = stats_service.build_snapshot_with_mode(
                session["spotify_access_token"],
                cache_only=use_cache_only and not refresh_cache,
                force_refresh=refresh_cache,
            )
            operation_success = refresh_cache
        except SpotifyClientError as exc:
            flash(str(exc), "warning")
            return redirect(url_for("dashboard", range=selected_range))
        finally:
            if operation_token is not None:
                monitoring_service.finish_operation(operation_token, send_dashboard_summary=operation_success)
        builder = DashboardReportBuilder(Path(app.config["EXPORT_FOLDER"]))
        output_path = builder.build(snapshot, selected_range)
        return send_file(output_path, as_attachment=True, download_name=output_path.name)

    @app.route("/profile", methods=["GET", "POST"])
    def profile() -> Any:
        developer_form = {
            "username": "",
            "email": "",
        }
        cache_mode = get_playlist_cache_mode()

        if request.method == "POST":
            form_action = request.form.get("form_action", "developer_request").strip()
            if form_action == "cache_preferences":
                cache_mode = request.form.get("cache_mode", cache_mode).strip().lower()
                if cache_mode not in {"cache_only", "normal"}:
                    cache_mode = "cache_only"
                session["playlist_cache_mode"] = cache_mode
                flash("Preferencia de cache actualizada.", "success")
                return redirect(url_for("profile"))
            else:
                developer_form["username"] = request.form.get("developer_username", "").strip()
                developer_form["email"] = request.form.get("developer_email", "").strip()

                if not developer_form["username"]:
                    flash("Escribe tu usuario de Developers antes de enviar la solicitud.", "danger")
                elif not developer_form["email"] or "@" not in developer_form["email"]:
                    flash("Introduce un correo valido para enviar la solicitud.", "danger")
                else:
                    try:
                        send_developer_request(
                            username=developer_form["username"],
                            email=developer_form["email"],
                        )
                        flash("Solicitud enviada a Discord correctamente.", "success")
                        return redirect(url_for("profile"))
                    except RuntimeError as exc:
                        flash(str(exc), "danger")

        env_values = load_env_values()
        token_status = get_token_status()
        setup_steps = [
            "Entra en Spotify for Developers y crea una app nueva desde tu dashboard.",
            "En la configuracion de la app, anade exactamente la Redirect URI publica de Nozomi o `http://127.0.0.1:8888/callback` para local.",
            "En local guarda `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REDIRECT_URI` y `FLASK_SECRET_KEY` en `.env`.",
            "En Railway abre tu servicio, ve a Variables y configura esas mismas claves para produccion.",
            "Reinicia en local o redeploya en Railway y vuelve a Nozomi Music para iniciar sesion con Spotify.",
            "Si Spotify responde y el token es valido, ya puedes usar crear, exportar y recomendar.",
        ]

        return render_template(
            "profile.html",
            developer_form=developer_form,
            env_values=env_values,
            token_status=token_status,
            setup_steps=setup_steps,
            cache_mode=cache_mode,
        )

    @app.errorhandler(413)
    def file_too_large(_: Any) -> Any:
        flash("El archivo supera el limite permitido de 2 MB.", "danger")
        return redirect(request.referrer or url_for("index"))

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "8888")), debug=True)
