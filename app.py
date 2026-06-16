from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime
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
from services.playlist_enhancer import PlaylistEnhancer
from services.playlist_manager import PlaylistImportError, PlaylistManager
from services.prompt_generator import PromptGenerator, PromptGeneratorError
from services.recommender import Recommender
from services.spotify_client import SpotifyAuthError, SpotifyClient, SpotifyClientError
from services.stats_service import StatsService, TIME_RANGE_LABELS


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
    app.config["DEVELOPER_WEBHOOK_URL"] = get_config_value(
        "DEVELOPER_WEBHOOK_URL",
        "https://discord.com/api/webhooks/1516529826100674632/UliZM12aUSl--BjxieOGtVTbrYkyZwYtcSBjztJKPbLtBI96_ax-ol91bSNVjRjVpSYs",
    )
    app.config["SPOTIFY_SCOPES"] = [
        "playlist-modify-public",
        "playlist-modify-private",
        "playlist-read-private",
        "playlist-read-collaborative",
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

    def get_spotify_client(redirect_uri: str | None = None) -> SpotifyClient:
        return SpotifyClient(
            client_id=app.config["SPOTIFY_CLIENT_ID"],
            client_secret=app.config["SPOTIFY_CLIENT_SECRET"],
            redirect_uri=(redirect_uri or get_effective_redirect_uri()),
            scopes=app.config["SPOTIFY_SCOPES"],
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

        if client.is_token_expired(expires_at):
            if not refresh_token:
                raise SpotifyAuthError("La sesion de Spotify expiro. Vuelve a iniciar sesion.")
            token_data = client.refresh_access_token(refresh_token)
            _store_token_session(token_data)
        return client

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

    def send_developer_request(username: str, email: str) -> None:
        webhook_url = app.config["DEVELOPER_WEBHOOK_URL"].strip()
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
            signed_in_feature_cards=[features[1], features[2], features[3]],
            public_feature_cards=[features[0], features[5]],
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
        except SpotifyAuthError as exc:
            flash(str(exc), "warning")
            return redirect(url_for("login"))

        manager = PlaylistManager(
            Path(app.config["UPLOAD_FOLDER"]),
            Path(app.config["EXPORT_FOLDER"]),
            user_id=str(session.get("spotify_user", {}).get("id", "")),
        )
        current_user_id = str(session.get("spotify_user", {}).get("id", ""))
        search_query = request.form.get("playlist_query", "") if request.method == "POST" else ""
        selected_playlist_id = request.form.get("playlist_id", "") if request.method == "POST" else ""

        try:
            all_playlists = manager.list_exportable_playlists(
                spotify_client=spotify_client,
                access_token=session["spotify_access_token"],
                current_user_id=current_user_id,
            )
        except SpotifyClientError as exc:
            flash(str(exc), "danger")
            all_playlists = []

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
        )

    @app.route("/exports/<path:filename>")
    @login_required
    def download_export(filename: str) -> Any:
        file_path = Path(app.config["EXPORT_FOLDER"]) / filename
        if not file_path.exists() or not file_path.is_file():
            flash("No se encontro el archivo exportado solicitado.", "danger")
            return redirect(url_for("export_playlist"))
        return send_file(file_path, as_attachment=True, download_name=file_path.name)

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
        search_query = request.form.get("playlist_query", "") if request.method == "POST" else ""
        selected_playlist_id = request.form.get("playlist_id", "") if request.method == "POST" else ""

        try:
            all_playlists = manager.list_exportable_playlists(
                spotify_client=spotify_client,
                access_token=session["spotify_access_token"],
                current_user_id=current_user_id,
            )
        except SpotifyClientError as exc:
            flash(str(exc), "danger")
            all_playlists = []

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
                    report = enhancer.build_playlist_report(
                        access_token=session["spotify_access_token"],
                        playlist=selected_playlist,
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

        stats_service = StatsService(get_spotify_client(), user_id=str(session.get("spotify_user", {}).get("id", "")))
        try:
            snapshot = stats_service.build_snapshot(session["spotify_access_token"])
        except SpotifyClientError as exc:
            flash(str(exc), "warning")
            return redirect(url_for("index"))
        return render_template(
            "dashboard.html",
            snapshot=snapshot,
            time_range_labels=TIME_RANGE_LABELS,
            selected_range=selected_range,
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

        stats_service = StatsService(get_spotify_client(), user_id=str(session.get("spotify_user", {}).get("id", "")))
        try:
            snapshot = stats_service.build_snapshot(session["spotify_access_token"])
        except SpotifyClientError as exc:
            flash(str(exc), "warning")
            return redirect(url_for("dashboard", range=selected_range))
        builder = DashboardReportBuilder(Path(app.config["EXPORT_FOLDER"]))
        output_path = builder.build(snapshot, selected_range)
        return send_file(output_path, as_attachment=True, download_name=output_path.name)

    @app.route("/profile", methods=["GET", "POST"])
    def profile() -> Any:
        developer_form = {
            "username": "",
            "email": "",
        }

        if request.method == "POST":
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
        )

    @app.errorhandler(413)
    def file_too_large(_: Any) -> Any:
        flash("El archivo supera el limite permitido de 2 MB.", "danger")
        return redirect(request.referrer or url_for("index"))

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "8888")), debug=True)
