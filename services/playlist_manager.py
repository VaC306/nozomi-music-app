from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from werkzeug.datastructures import FileStorage

from services.spotify_api_cache_service import SpotifyApiCacheService
from services.spotify_client import SpotifyClient


class PlaylistImportError(Exception):
    """Raised when a TXT playlist import cannot be completed."""


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    normalized = normalized.encode("ascii", "ignore").decode("ascii")
    normalized = normalized.lower().strip()
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub(r"[^a-z0-9 ]", "", normalized)
    return normalized


def parse_song_line(line: str) -> tuple[str, str]:
    cleaned = line.strip()
    if not cleaned:
        raise ValueError("La linea esta vacia.")
    parts = cleaned.split(" - ", 1)
    if len(parts) != 2:
        raise ValueError("Formato invalido. Usa 'Titulo - Artista'.")
    title, artist = parts[0].strip(), parts[1].strip()
    if not title or not artist:
        raise ValueError("Titulo o artista vacio.")
    return title, artist


def sanitize_filename(value: str) -> str:
    base = normalize_text(value).replace(" ", "_").strip("_")
    return base or "playlist"


class PlaylistManager:
    PLAYLISTS_TTL_SECONDS = 900
    PLAYLIST_TRACKS_TTL_SECONDS = 1800
    TRACK_SEARCH_TTL_SECONDS = 7776000

    def __init__(self, uploads_dir: Path, exports_dir: Path, user_id: str = "") -> None:
        self.uploads_dir = uploads_dir
        self.exports_dir = exports_dir
        self.cache_service = SpotifyApiCacheService(cache_scope=f"user:{user_id or 'anonymous'}")

    @staticmethod
    def get_upload_help() -> list[str]:
        return [
            "Una cancion por linea.",
            "Usa el formato exacto: Titulo - Artista.",
            "Ejemplo: Bohemian Rhapsody - Queen.",
            "Las lineas con formato invalido se marcaran por separado.",
        ]

    def save_uploaded_txt(self, upload: FileStorage | None) -> Path:
        if not upload or not upload.filename:
            raise PlaylistImportError("Selecciona un archivo TXT antes de continuar.")

        filename = upload.filename.strip()
        if not filename.lower().endswith(".txt"):
            raise PlaylistImportError("El archivo debe tener extension .txt.")

        safe_stem = sanitize_filename(Path(filename).stem)
        destination = self.uploads_dir / f"{safe_stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        upload.save(destination)
        return destination

    def create_playlist_from_txt(
        self,
        spotify_client: SpotifyClient,
        access_token: str,
        playlist_name: str,
        txt_path: Path,
    ) -> dict[str, Any]:
        cleaned_name = playlist_name.strip()
        if not cleaned_name:
            raise PlaylistImportError("Indica un nombre para la playlist.")

        if not txt_path.exists() or not txt_path.is_file():
            raise PlaylistImportError("No se encontro el archivo TXT subido.")

        try:
            lines = txt_path.read_text(encoding="utf-8-sig").splitlines()
        except OSError as exc:
            raise PlaylistImportError("No se pudo leer el archivo TXT.") from exc

        raw_lines = [line for line in lines if line.strip()]
        if not raw_lines:
            raise PlaylistImportError("El archivo TXT esta vacio.")

        found_tracks: list[dict[str, Any]] = []
        not_found: list[str] = []
        invalid_lines: list[str] = []

        for line in raw_lines:
            try:
                title, artist = parse_song_line(line)
            except ValueError:
                invalid_lines.append(line)
                continue

            best_match = self.cache_service.get_or_set(
                cache_key=self._build_track_search_cache_key(title, artist),
                ttl_seconds=self.TRACK_SEARCH_TTL_SECONDS,
                source_endpoint="search_track_match",
                fetcher=lambda title=title, artist=artist: self._search_best_track_match(
                    spotify_client,
                    access_token,
                    title,
                    artist,
                ),
            )
            if best_match is None:
                not_found.append(line)
                continue
            found_tracks.append(best_match)

        playlist = spotify_client.create_playlist(
            access_token,
            name=cleaned_name,
            description="Playlist creada desde Nozomi Music.",
        )

        track_uris = [track["uri"] for track in found_tracks if track.get("uri")]
        if track_uris:
            spotify_client.add_tracks_to_playlist(access_token, playlist["id"], track_uris)

        self.cache_service.delete("user-playlists:v1")
        self.cache_service.delete("dashboard-snapshot:v1")

        return {
            "playlist_name": playlist.get("name", cleaned_name),
            "playlist_id": playlist.get("id", ""),
            "playlist_url": playlist.get("external_urls", {}).get("spotify", ""),
            "source_file": txt_path.name,
            "lines_read": len(raw_lines),
            "found_count": len(found_tracks),
            "not_found_count": len(not_found),
            "invalid_count": len(invalid_lines),
            "tracks_added": len(track_uris),
            "not_found": not_found,
            "invalid_lines": invalid_lines,
        }

    def list_exportable_playlists(
        self,
        spotify_client: SpotifyClient,
        access_token: str,
        current_user_id: str,
    ) -> list[dict[str, Any]]:
        playlists = self.cache_service.get_or_set(
            cache_key="user-playlists:v1",
            ttl_seconds=self.PLAYLISTS_TTL_SECONDS,
            source_endpoint="get_user_playlists",
            fetcher=lambda: spotify_client.get_user_playlists(access_token),
        )
        exportable = [
            self._normalize_playlist_summary(spotify_client, access_token, playlist)
            for playlist in playlists
            if self._can_export_playlist(playlist, current_user_id)
        ]
        return sorted(exportable, key=lambda item: item.get("name", "").lower())

    def find_exportable_playlists(
        self,
        spotify_client: SpotifyClient,
        access_token: str,
        current_user_id: str,
        query: str,
    ) -> list[dict[str, Any]]:
        query_key = query.strip().lower()
        if not query_key:
            return self.list_exportable_playlists(spotify_client, access_token, current_user_id)

        playlists = self.list_exportable_playlists(spotify_client, access_token, current_user_id)
        exact_matches: list[dict[str, Any]] = []
        partial_matches: list[dict[str, Any]] = []

        for playlist in playlists:
            playlist_name = playlist.get("name", "")
            owner = playlist.get("owner") or {}
            owner_name = owner.get("display_name") or owner.get("id", "")
            playlist_key = f"{playlist_name} {owner_name}".lower().strip()
            if playlist_key == query_key:
                exact_matches.append(playlist)
            elif query_key in playlist_key:
                partial_matches.append(playlist)

        return exact_matches or partial_matches

    def export_playlist_to_txt(
        self,
        spotify_client: SpotifyClient,
        access_token: str,
        playlist: dict[str, Any],
    ) -> dict[str, Any]:
        playlist_id = str(playlist.get("id", "")).strip()
        tracks = self.cache_service.get_or_set(
            cache_key=f"playlist-tracks:v1:{playlist_id}",
            ttl_seconds=self.PLAYLIST_TRACKS_TTL_SECONDS,
            source_endpoint="get_playlist_tracks",
            fetcher=lambda: spotify_client.get_playlist_tracks(access_token, playlist_id),
        )
        if not tracks:
            raise PlaylistImportError("La playlist no tiene canciones para exportar.")

        lines: list[str] = []
        index = 1
        for item in tracks:
            content_item = item.get("item") or item.get("track") or {}
            if not content_item or content_item.get("type") == "episode":
                continue
            title = content_item.get("name", "Cancion sin titulo")
            artist = ", ".join(artist.get("name", "") for artist in content_item.get("artists", []))
            lines.append(f"{index}. {title} - {artist}")
            index += 1

        if not lines:
            raise PlaylistImportError("No se pudieron leer canciones validas de la playlist.")

        filename = f"{sanitize_filename(playlist.get('name', 'playlist'))}.txt"
        output_path = self.exports_dir / filename
        output_path.write_text("\n".join(lines), encoding="utf-8")

        return {
            "playlist_name": playlist.get("name", "Playlist"),
            "playlist_id": playlist.get("id", ""),
            "playlist_url": playlist.get("external_urls", {}).get("spotify", ""),
            "owner_name": playlist.get("owner_name") or playlist.get("owner", {}).get("display_name") or playlist.get("owner", {}).get("id", "Spotify"),
            "track_count": len(lines),
            "file_name": filename,
            "file_path": output_path,
        }

    @staticmethod
    def _normalize_playlist_summary(
        spotify_client: SpotifyClient,
        access_token: str,
        playlist: dict[str, Any],
    ) -> dict[str, Any]:
        owner = playlist.get("owner") or {}
        tracks = playlist.get("tracks") or {}
        owner_name = owner.get("display_name") or owner.get("id", "Spotify")
        track_total = tracks.get("total")
        if not isinstance(track_total, int):
            playlist_id = str(playlist.get("id", "")).strip()
            if playlist_id:
                try:
                    playlist_details = spotify_client.get_playlist(access_token, playlist_id)
                    if isinstance(playlist_details.get("owner"), dict):
                        owner = playlist_details.get("owner") or owner
                        owner_name = owner.get("display_name") or owner.get("id", owner_name)
                    playlist = {**playlist, **playlist_details}
                    track_total = spotify_client.get_playlist_track_count(access_token, playlist_id)
                except Exception:  # noqa: BLE001
                    track_total = 0
        if not isinstance(track_total, int):
            track_total = 0
        return {
            **playlist,
            "owner": owner,
            "owner_name": owner_name,
            "track_total": track_total,
            "search_text": f"{playlist.get('name', '')} {owner_name}".lower().strip(),
        }

    @staticmethod
    def _can_export_playlist(playlist: dict[str, Any], current_user_id: str) -> bool:
        owner_id = playlist.get("owner", {}).get("id", "")
        collaborative = bool(playlist.get("collaborative", False))
        return owner_id == current_user_id or collaborative

    @staticmethod
    def _build_track_search_cache_key(title: str, artist: str) -> str:
        normalized = f"{normalize_text(title)}::{normalize_text(artist)}"
        return f"search-track:v1:{hashlib.sha1(normalized.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _search_best_track_match(
        spotify_client: SpotifyClient,
        access_token: str,
        title: str,
        artist: str,
    ) -> dict[str, Any] | None:
        candidates = spotify_client.search_track(access_token, title, artist)
        return spotify_client.choose_best_track_match(title, artist, candidates)
