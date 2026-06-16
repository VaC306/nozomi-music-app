from __future__ import annotations

import base64
import os
import threading
import time
from typing import Any
from urllib.parse import quote

import requests


class SpotifyClientError(Exception):
    """Raised when the Spotify API returns an error."""


class SpotifyAuthError(SpotifyClientError):
    """Raised when Spotify OAuth fails."""


class SpotifyRateLimitError(SpotifyClientError):
    """Raised when Spotify rate limiting is active."""

    def __init__(self, message: str, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class SpotifyClient:
    API_BASE_URL = "https://api.spotify.com/v1"
    AUTH_URL = "https://accounts.spotify.com/authorize"
    TOKEN_URL = "https://accounts.spotify.com/api/token"
    _MAX_CONCURRENT_REQUESTS = max(int(os.getenv("SPOTIFY_MAX_CONCURRENT_REQUESTS", "2") or 2), 1)
    _REQUEST_SEMAPHORE = threading.BoundedSemaphore(value=_MAX_CONCURRENT_REQUESTS)
    _RATE_LIMIT_LOCK = threading.Lock()
    _RATE_LIMITED_UNTIL = 0.0

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, scopes: list[str]) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scopes = scopes
        self.session = requests.Session()
        self.spotify_call_count = 0

    def build_authorization_url(self, state: str) -> str:
        scopes = quote(" ".join(self.scopes))
        redirect_uri = quote(self.redirect_uri)
        return (
            f"{self.AUTH_URL}?client_id={self.client_id}"
            f"&response_type=code&redirect_uri={redirect_uri}&scope={scopes}&state={state}"
        )

    def exchange_code_for_token(self, code: str) -> dict[str, Any]:
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
        }
        return self._request_token(payload)

    def refresh_access_token(self, refresh_token: str) -> dict[str, Any]:
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        token_data = self._request_token(payload)
        if not token_data.get("refresh_token"):
            token_data["refresh_token"] = refresh_token
        return token_data

    def get_client_credentials_token(self) -> dict[str, Any]:
        return self._request_token({"grant_type": "client_credentials"})

    def verify_configuration(self) -> dict[str, Any]:
        if not self.client_id or not self.client_secret or not self.redirect_uri:
            raise SpotifyAuthError("Faltan Client ID, Client Secret o Redirect URI para probar la configuracion.")

        token_data = self.get_client_credentials_token()
        access_token = token_data.get("access_token", "")
        if not access_token:
            raise SpotifyAuthError("Spotify no devolvio un token valido para la aplicacion.")

        self.request(access_token, "GET", "/recommendations/available-genre-seeds")
        return {"ok": True, "message": "Configuracion Spotify valida."}

    def get_current_user(self, access_token: str) -> dict[str, Any]:
        return self.request(access_token, "GET", "/me")

    def get_top_tracks(self, access_token: str, time_range: str, limit: int = 10) -> list[dict[str, Any]]:
        data = self.request(
            access_token,
            "GET",
            "/me/top/tracks",
            params={"time_range": time_range, "limit": limit},
        )
        return data.get("items", [])

    def get_top_artists(self, access_token: str, time_range: str, limit: int = 10) -> list[dict[str, Any]]:
        data = self.request(
            access_token,
            "GET",
            "/me/top/artists",
            params={"time_range": time_range, "limit": limit},
        )
        return data.get("items", [])

    def get_recently_played(self, access_token: str, limit: int = 15) -> list[dict[str, Any]]:
        data = self.request(
            access_token,
            "GET",
            "/me/player/recently-played",
            params={"limit": limit},
        )
        return data.get("items", [])

    def get_artist(self, access_token: str, artist_id: str) -> dict[str, Any]:
        return self.request(access_token, "GET", f"/artists/{artist_id}")

    def get_artists(self, access_token: str, artist_ids: list[str]) -> list[dict[str, Any]]:
        if not artist_ids:
            return []
        data = self.request(
            access_token,
            "GET",
            "/artists",
            params={"ids": ",".join(artist_ids[:50])},
        )
        return data.get("artists", [])

    def get_user_playlists(self, access_token: str) -> list[dict[str, Any]]:
        playlists: list[dict[str, Any]] = []
        endpoint = "/me/playlists"
        params: dict[str, Any] | None = {
            "limit": 50,
            "fields": "items(id,name,owner(display_name,id),tracks(total),collaborative,external_urls(spotify)),next",
        }

        while endpoint:
            data = self.request(access_token, "GET", endpoint, params=params)
            playlists.extend(data.get("items", []))
            next_url = data.get("next")
            if not next_url:
                break
            endpoint = next_url.replace(self.API_BASE_URL, "")
            params = None
        return playlists

    def get_playlist(self, access_token: str, playlist_id: str) -> dict[str, Any]:
        return self.request(
            access_token,
            "GET",
            f"/playlists/{playlist_id}",
            params={"fields": "id,name,owner(display_name,id),tracks(total),collaborative,external_urls(spotify)"},
        )

    def get_playlist_track_count(self, access_token: str, playlist_id: str) -> int:
        data = self.request(
            access_token,
            "GET",
            f"/playlists/{playlist_id}/items",
            params={"limit": 1, "offset": 0},
        )
        total = data.get("total", 0)
        return total if isinstance(total, int) else 0

    def get_playlist_tracks(self, access_token: str, playlist_id: str) -> list[dict[str, Any]]:
        tracks: list[dict[str, Any]] = []
        endpoint = f"/playlists/{playlist_id}/items"
        params: dict[str, Any] | None = {"limit": 100}

        while endpoint:
            data = self.request(access_token, "GET", endpoint, params=params)
            tracks.extend(data.get("items", []))
            next_url = data.get("next")
            if not next_url:
                break
            endpoint = next_url.replace(self.API_BASE_URL, "")
            params = None
        return tracks

    def search_track(self, access_token: str, title: str, artist: str, limit: int = 5) -> list[dict[str, Any]]:
        query = f'track:"{title}" artist:"{artist}"'
        data = self.request(
            access_token,
            "GET",
            "/search",
            params={"q": query, "type": "track", "limit": limit},
        )
        return data.get("tracks", {}).get("items", [])

    def create_playlist(self, access_token: str, name: str, description: str = "") -> dict[str, Any]:
        payload = {"name": name, "description": description, "public": False}
        return self.request(access_token, "POST", "/me/playlists", json_body=payload)

    def add_tracks_to_playlist(self, access_token: str, playlist_id: str, track_uris: list[str]) -> None:
        for start in range(0, len(track_uris), 100):
            chunk = track_uris[start : start + 100]
            self.request(access_token, "POST", f"/playlists/{playlist_id}/items", json_body={"uris": chunk})

    def get_available_genre_seeds(self, access_token: str) -> list[str]:
        data = self.request(access_token, "GET", "/recommendations/available-genre-seeds")
        return data.get("genres", [])

    def get_recommendations_by_genre(self, access_token: str, genre: str, limit: int = 8) -> list[dict[str, Any]]:
        data = self.request(
            access_token,
            "GET",
            "/recommendations",
            params={"seed_genres": genre, "limit": limit},
        )
        return data.get("tracks", [])

    def get_recommendations(
        self,
        access_token: str,
        seed_artists: list[str] | None = None,
        seed_genres: list[str] | None = None,
        seed_tracks: list[str] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if seed_artists:
            params["seed_artists"] = ",".join(seed_artists[:5])
        if seed_genres:
            params["seed_genres"] = ",".join(seed_genres[:5])
        if seed_tracks:
            params["seed_tracks"] = ",".join(seed_tracks[:5])
        if len(params) == 1:
            return []
        data = self.request(access_token, "GET", "/recommendations", params=params)
        return data.get("tracks", [])

    def search_artists_by_genre(self, access_token: str, genre: str, limit: int = 5) -> list[dict[str, Any]]:
        data = self.request(
            access_token,
            "GET",
            "/search",
            params={"q": f'genre:"{genre}"', "type": "artist", "limit": limit},
        )
        return data.get("artists", {}).get("items", [])

    def search_artists(self, access_token: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
        data = self.request(
            access_token,
            "GET",
            "/search",
            params={"q": query, "type": "artist", "limit": limit},
        )
        return data.get("artists", {}).get("items", [])

    def search_tracks_by_keyword(self, access_token: str, keyword: str, limit: int = 10) -> list[dict[str, Any]]:
        data = self.request(
            access_token,
            "GET",
            "/search",
            params={"q": keyword, "type": "track", "limit": limit},
        )
        return data.get("tracks", {}).get("items", [])

    def get_artist_top_tracks(self, access_token: str, artist_id: str, market: str = "US") -> list[dict[str, Any]]:
        data = self.request(
            access_token,
            "GET",
            f"/artists/{artist_id}/top-tracks",
            params={"market": market},
        )
        return data.get("tracks", [])

    def choose_best_track_match(
        self,
        title: str,
        artist: str,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        from services.playlist_manager import normalize_text

        best_candidate = None
        best_score = -1
        title_key = normalize_text(title)
        artist_key = normalize_text(artist)

        for candidate in candidates:
            candidate_title = normalize_text(candidate.get("name", ""))
            candidate_artists = [normalize_text(item.get("name", "")) for item in candidate.get("artists", [])]

            score = 0
            if candidate_title == title_key:
                score += 3
            elif title_key in candidate_title or candidate_title in title_key:
                score += 1

            if artist_key in candidate_artists:
                score += 3
            elif any(artist_key in item or item in artist_key for item in candidate_artists):
                score += 1

            if score > best_score:
                best_score = score
                best_candidate = candidate

        return best_candidate

    @staticmethod
    def is_token_expired(expires_at: int) -> bool:
        return int(expires_at or 0) <= int(time.time())

    def request(
        self,
        access_token: str,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._raise_if_rate_limited()
        url = endpoint if endpoint.startswith("http") else f"{self.API_BASE_URL}{endpoint}"
        headers = {"Authorization": f"Bearer {access_token}"}
        with self._REQUEST_SEMAPHORE:
            self._raise_if_rate_limited()
            self.spotify_call_count += 1
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=30,
                )
            except requests.RequestException as exc:
                raise SpotifyClientError("No se pudo conectar con Spotify.") from exc

        if response.status_code >= 400:
            self._raise_api_error(response)

        if not response.text:
            return {}

        try:
            return response.json()
        except ValueError as exc:
            raise SpotifyClientError("Spotify devolvio una respuesta invalida.") from exc

    def _request_token(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.session.post(
                self.TOKEN_URL,
                data=payload,
                headers={"Authorization": self._build_basic_auth_header()},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise SpotifyAuthError("No se pudo conectar con Spotify para autenticar la sesion.") from exc

        if response.status_code >= 400:
            self._raise_auth_error(response)

        try:
            token_data = response.json()
        except ValueError as exc:
            raise SpotifyAuthError("No se pudo interpretar la respuesta de autenticacion.") from exc

        token_data["expires_at"] = int(time.time()) + int(token_data.get("expires_in", 3600)) - 60
        return token_data

    def _build_basic_auth_header(self) -> str:
        raw = f"{self.client_id}:{self.client_secret}".encode("utf-8")
        return f"Basic {base64.b64encode(raw).decode('utf-8')}"

    @classmethod
    def _raise_api_error(cls, response: requests.Response) -> None:
        message = "Error al comunicarse con Spotify."
        try:
            data = response.json()
            error = data.get("error", {})
            if isinstance(error, dict):
                message = error.get("message", message)
            elif isinstance(error, str):
                message = error
        except ValueError:
            message = f"Spotify devolvio un error HTTP {response.status_code}."

        if response.status_code == 401:
            raise SpotifyAuthError("La sesion de Spotify ya no es valida. Vuelve a iniciar sesion.")
        if response.status_code == 429:
            retry_after = cls._parse_retry_after(response.headers.get("Retry-After", ""))
            cls._activate_rate_limit(retry_after)
            detail = (
                f" Intenta de nuevo en {retry_after} segundos."
                if isinstance(retry_after, int) and retry_after > 0
                else " Espera un momento antes de volver a intentarlo."
            )
            raise SpotifyRateLimitError(
                f"Spotify esta recibiendo demasiadas solicitudes ahora mismo y pausamos las llamadas para proteger la app.{detail}",
                retry_after_seconds=retry_after,
            )
        if response.status_code >= 500:
            raise SpotifyClientError("Spotify no esta respondiendo correctamente en este momento.")
        raise SpotifyClientError(message)

    @classmethod
    def _activate_rate_limit(cls, retry_after_seconds: int | None) -> None:
        wait_seconds = retry_after_seconds if isinstance(retry_after_seconds, int) and retry_after_seconds > 0 else 60
        with cls._RATE_LIMIT_LOCK:
            cls._RATE_LIMITED_UNTIL = max(cls._RATE_LIMITED_UNTIL, time.time() + wait_seconds)

    @classmethod
    def _raise_if_rate_limited(cls) -> None:
        with cls._RATE_LIMIT_LOCK:
            remaining_seconds = int(max(cls._RATE_LIMITED_UNTIL - time.time(), 0))
            if remaining_seconds <= 0:
                cls._RATE_LIMITED_UNTIL = 0.0
                return
        raise SpotifyRateLimitError(
            (
                "Spotify sigue aplicando rate limit. "
                f"Esperamos {remaining_seconds} segundos antes de volver a llamar para evitar mas bloqueos."
            ),
            retry_after_seconds=remaining_seconds,
        )

    @staticmethod
    def _parse_retry_after(value: str) -> int | None:
        raw_value = value.strip()
        if not raw_value.isdigit():
            return None
        return int(raw_value)

    @staticmethod
    def _raise_auth_error(response: requests.Response) -> None:
        message = "No se pudo autenticar con Spotify."
        try:
            data = response.json()
            message = data.get("error_description") or data.get("error") or message
        except ValueError:
            pass
        raise SpotifyAuthError(message)
