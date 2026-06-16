from __future__ import annotations

import os
import secrets
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, send_file, session, url_for

from models import AppSettings, SpotifyUser, db
from services.dashboard_report import DashboardReportBuilder
from services.playlist_manager import PlaylistImportError, PlaylistManager
from services.prompt_generator import PromptGenerator, PromptGeneratorError
from services.recommender import Recommender
from services.spotify_client import SpotifyAuthError, SpotifyClient, SpotifyClientError
from services.stats_service import StatsService, TIME_RANGE_LABELS


BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(BASE_DIR / ".env")


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(32))
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024
    app.config["UPLOAD_FOLDER"] = str(BASE_DIR / "uploads")
    app.config["EXPORT_FOLDER"] = str(BASE_DIR / "exports")
    database_url = os.getenv("DATABASE_URL", "").strip()
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url or f"sqlite:///{BASE_DIR / 'nozomi.db'}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SPOTIFY_CLIENT_ID"] = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    app.config["SPOTIFY_CLIENT_SECRET"] = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
    app.config["SPOTIFY_REDIRECT_URI"] = os.getenv("SPOTIFY_REDIRECT_URI", "").strip()
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

    def load_env_values() -> dict[str, str]:
        db_settings = AppSettings.query.get(1)
        values = {
            "FLASK_SECRET_KEY": (db_settings.flask_secret_key if db_settings else os.getenv("FLASK_SECRET_KEY", "")),
            "SPOTIFY_CLIENT_ID": (db_settings.spotify_client_id if db_settings else os.getenv("SPOTIFY_CLIENT_ID", "")),
            "SPOTIFY_CLIENT_SECRET": (db_settings.spotify_client_secret if db_settings else os.getenv("SPOTIFY_CLIENT_SECRET", "")),
            "SPOTIFY_REDIRECT_URI": (db_settings.spotify_redirect_uri if db_settings else os.getenv("SPOTIFY_REDIRECT_URI", "")),
        }
        return values

    def save_env_values(values: dict[str, str]) -> None:
        content = "\n".join(
            [
                f"FLASK_SECRET_KEY={values.get('FLASK_SECRET_KEY', '').strip()}",
                f"SPOTIFY_CLIENT_ID={values.get('SPOTIFY_CLIENT_ID', '').strip()}",
                f"SPOTIFY_CLIENT_SECRET={values.get('SPOTIFY_CLIENT_SECRET', '').strip()}",
                f"SPOTIFY_REDIRECT_URI={values.get('SPOTIFY_REDIRECT_URI', '').strip()}",
            ]
        ) + "\n"
        ENV_PATH.write_text(content, encoding="utf-8")

        for key, value in values.items():
            os.environ[key] = value.strip()

        settings = AppSettings.query.get(1) or AppSettings(id=1)
        settings.flask_secret_key = values.get("FLASK_SECRET_KEY", "").strip()
        settings.spotify_client_id = values.get("SPOTIFY_CLIENT_ID", "").strip()
        settings.spotify_client_secret = values.get("SPOTIFY_CLIENT_SECRET", "").strip()
        settings.spotify_redirect_uri = values.get("SPOTIFY_REDIRECT_URI", "").strip()
        db.session.add(settings)
        db.session.commit()

        secret_value = str(values.get("FLASK_SECRET_KEY", app.config["SECRET_KEY"]) or "").strip()
        app.config["SECRET_KEY"] = secret_value or app.config["SECRET_KEY"]
        app.config["SPOTIFY_CLIENT_ID"] = values.get("SPOTIFY_CLIENT_ID", "").strip()
        app.config["SPOTIFY_CLIENT_SECRET"] = values.get("SPOTIFY_CLIENT_SECRET", "").strip()
        app.config["SPOTIFY_REDIRECT_URI"] = values.get("SPOTIFY_REDIRECT_URI", "").strip()

    def get_spotify_client() -> SpotifyClient:
        return SpotifyClient(
            client_id=app.config["SPOTIFY_CLIENT_ID"],
            client_secret=app.config["SPOTIFY_CLIENT_SECRET"],
            redirect_uri=app.config["SPOTIFY_REDIRECT_URI"],
            scopes=app.config["SPOTIFY_SCOPES"],
        )

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

    @app.context_processor
    def inject_globals() -> dict[str, Any]:
        return {
            "spotify_user": session.get("spotify_user"),
            "current_year": datetime.now().year,
            "request_endpoint": request.endpoint,
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
                "Completa la configuracion de Spotify en Perfil antes de iniciar sesion.",
                "danger",
            )
            return render_template("login.html", auth_url=None)

        client = get_spotify_client()
        state = secrets.token_urlsafe(24)
        session["spotify_oauth_state"] = state
        auth_url = client.build_authorization_url(state)

        if request.args.get("start") == "1":
            return redirect(auth_url)

        return render_template("login.html", auth_url=auth_url)

    @app.route("/callback")
    def callback() -> Any:
        error = request.args.get("error")
        if error:
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

        client = get_spotify_client()
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
            stored_user = SpotifyUser(spotify_user_id=session["spotify_user"]["id"])
        stored_user.display_name = session["spotify_user"]["display_name"]
        stored_user.email = session["spotify_user"]["email"]
        stored_user.profile_url = session["spotify_user"]["profile_url"]
        stored_user.image_url = session["spotify_user"]["image_url"]
        stored_user.access_token = session.get("spotify_access_token", "")
        stored_user.refresh_token = session.get("spotify_refresh_token", "")
        stored_user.token_expires_at = int(session.get("spotify_expires_at", 0) or 0)
        stored_user.client_id = app.config["SPOTIFY_CLIENT_ID"]
        stored_user.client_secret = app.config["SPOTIFY_CLIENT_SECRET"]
        stored_user.redirect_uri = app.config["SPOTIFY_REDIRECT_URI"]
        db.session.add(stored_user)
        db.session.commit()
        session.pop("spotify_oauth_state", None)
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
        )
        current_user_id = str(session.get("spotify_user", {}).get("id", ""))
        search_query = request.form.get("playlist_query", "") if request.method == "POST" else ""
        selected_playlist_id = request.form.get("playlist_id", "") if request.method == "POST" else ""

        try:
            playlists = manager.find_exportable_playlists(
                spotify_client=spotify_client,
                access_token=session["spotify_access_token"],
                current_user_id=current_user_id,
                query=search_query,
            )
        except SpotifyClientError as exc:
            flash(str(exc), "danger")
            playlists = []

        result = None
        if request.method == "POST" and selected_playlist_id:
            selected_playlist = next(
                (playlist for playlist in playlists if playlist.get("id") == selected_playlist_id),
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
        recommender = Recommender(get_spotify_client())
        genre = request.args.get("genre", "")
        raw_limit = request.args.get("limit", "6").strip()
        limit = int(raw_limit) if raw_limit.isdigit() else 6
        limit = max(1, min(limit, 12))
        result = None

        if genre.strip():
            try:
                result = recommender.build_recommendation_result(
                    access_token=session["spotify_access_token"],
                    genre=genre,
                    limit=limit,
                )
                if result["warning"]:
                    flash(result["warning"], "warning")
                if not result["tracks"]:
                    flash("No encontramos recomendaciones para ese genero en este momento.", "info")
            except SpotifyClientError as exc:
                flash(str(exc), "danger")

        return render_template(
            "recommendations.html",
            sample_genres=recommender.get_sample_genres(),
            result=result,
            selected_genre=genre,
            selected_limit=limit,
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

        stats_service = StatsService(get_spotify_client())
        snapshot = stats_service.build_snapshot(session["spotify_access_token"])
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

        stats_service = StatsService(get_spotify_client())
        snapshot = stats_service.build_snapshot(session["spotify_access_token"])
        builder = DashboardReportBuilder(Path(app.config["EXPORT_FOLDER"]))
        output_path = builder.build(snapshot, selected_range)
        return send_file(output_path, as_attachment=True, download_name=output_path.name)

    @app.route("/profile", methods=["GET", "POST"])
    def profile() -> Any:
        if request.method == "POST":
            action = request.form.get("action", "save")
            values = {
                "FLASK_SECRET_KEY": request.form.get("flask_secret_key", "").strip(),
                "SPOTIFY_CLIENT_ID": request.form.get("spotify_client_id", "").strip(),
                "SPOTIFY_CLIENT_SECRET": request.form.get("spotify_client_secret", "").strip(),
                "SPOTIFY_REDIRECT_URI": request.form.get("spotify_redirect_uri", "").strip(),
            }
            save_env_values(values)
            if action == "test_config":
                try:
                    get_spotify_client().verify_configuration()
                    flash("Configuracion Spotify valida. Credenciales y Redirect URI listas para usarse.", "success")
                except (SpotifyAuthError, SpotifyClientError) as exc:
                    flash(str(exc), "danger")
            else:
                flash("Configuracion guardada en .env correctamente.", "success")

        env_values = load_env_values()
        token_status = get_token_status()
        setup_steps = [
            "Entra en Spotify for Developers y crea una app nueva desde tu dashboard.",
            "Pon un nombre a la app y acepta los terminos del portal de desarrolladores.",
            "Copia tu Client ID y Client Secret en el formulario de esta pagina.",
            "En la configuracion de la app, anade exactamente la misma Redirect URI que uses aqui.",
            "Guarda los cambios, vuelve a Nozomi Music y pulsa iniciar sesion con Spotify.",
            "Si Spotify responde y el token es valido, ya puedes usar crear, exportar y recomendar.",
        ]

        return render_template(
            "profile.html",
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
    app.run(debug=True)
