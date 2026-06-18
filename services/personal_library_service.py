from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from services.playlist_manager import PlaylistImportError, sanitize_filename
from services.spotify_api_cache_service import SpotifyApiCacheService
from services.spotify_client import SpotifyClient


class PersonalLibraryService:
    SAVED_TRACKS_TTL_SECONDS = 21600
    SAVED_ALBUMS_TTL_SECONDS = 21600
    FOLLOWED_ARTISTS_TTL_SECONDS = 21600
    PREVIEW_PAGE_SIZE = 50

    SAVED_TRACKS_CACHE_KEY = "saved-tracks:v2"
    SAVED_ALBUMS_CACHE_KEY = "saved-albums:v2"
    FOLLOWED_ARTISTS_CACHE_KEY = "followed-artists:v1"

    TAB_TRACKS = "tracks"
    TAB_ALBUMS = "albums"
    TAB_ARTISTS = "artists"
    VALID_TABS = {TAB_TRACKS, TAB_ALBUMS, TAB_ARTISTS}

    def __init__(self, exports_dir: Path, user_id: str = "") -> None:
        self.exports_dir = exports_dir
        self.cache_service = SpotifyApiCacheService(cache_scope=f"user:{user_id or 'anonymous'}")

    def list_items(
        self,
        tab: str,
        spotify_client: SpotifyClient,
        access_token: str,
        prefer_cached: bool = False,
        allow_stale: bool = False,
        cache_only: bool = False,
        force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        tab = self.normalize_tab(tab)
        config = self._tab_config()[tab]
        cache_key = config["cache_key"]

        if prefer_cached or cache_only:
            cached_items = self.cache_service.get(cache_key, allow_stale=allow_stale or cache_only) or []
            if cached_items:
                return config["normalizer"](cached_items)
            if cache_only:
                raise PlaylistImportError(
                    "No hay una copia en cache de esta seccion de tu biblioteca todavia. Desactiva 'solo cache' o fuerza un refresco cuando Spotify lo permita."
                )

        items = self.cache_service.get_or_set(
            cache_key=cache_key,
            ttl_seconds=config["ttl_seconds"],
            source_endpoint=config["source_endpoint"],
            fetcher=lambda: config["fetcher"](spotify_client, access_token),
            force_refresh=force_refresh,
        )
        return config["normalizer"](items)

    def get_cached_items(self, tab: str) -> list[dict[str, Any]]:
        tab = self.normalize_tab(tab)
        config = self._tab_config()[tab]
        return config["normalizer"](self.cache_service.get(config["cache_key"], allow_stale=True) or [])

    def clear_cache(self, tab: str) -> None:
        tab = self.normalize_tab(tab)
        config = self._tab_config()[tab]
        self.cache_service.delete(config["cache_key"])
        if config.get("page_cache_prefix"):
            self.cache_service.delete_by_prefix(config["page_cache_prefix"])

    def list_items_incremental(
        self,
        tab: str,
        spotify_client: SpotifyClient,
        access_token: str,
        page: int = 1,
        prefer_cached: bool = False,
        allow_stale: bool = False,
        cache_only: bool = False,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        tab = self.normalize_tab(tab)
        config = self._tab_config()[tab]
        safe_page = max(page, 1)

        if not config.get("supports_incremental"):
            items = self.list_items(
                tab,
                spotify_client,
                access_token,
                prefer_cached=prefer_cached,
                allow_stale=allow_stale,
                cache_only=cache_only,
                force_refresh=force_refresh,
            )
            return {
                "items": items,
                "page": 1,
                "page_size": len(items),
                "loaded_count": len(items),
                "total_count": len(items),
                "has_more": False,
                "supports_incremental": False,
                "uses_partial_data": False,
            }

        page_size = int(config.get("page_size", self.PREVIEW_PAGE_SIZE) or self.PREVIEW_PAGE_SIZE)
        normalized_items: list[dict[str, Any]] = []
        total_count = 0
        has_more = False

        for current_page in range(1, safe_page + 1):
            offset = (current_page - 1) * page_size
            cache_key = self._build_page_cache_key(config["page_cache_prefix"], offset, page_size)
            payload = None if force_refresh else self.cache_service.get(cache_key, allow_stale=allow_stale or cache_only)
            if payload is None and cache_only:
                raise PlaylistImportError(
                    "No hay una copia en cache de este bloque de tu biblioteca todavia. Desactiva 'solo cache' o fuerza un refresco cuando Spotify lo permita."
                )
            if payload is None:
                payload = self.cache_service.get_or_set(
                    cache_key=cache_key,
                    ttl_seconds=config["ttl_seconds"],
                    source_endpoint=config["source_endpoint"],
                    fetcher=lambda offset=offset, page_size=page_size: config["page_fetcher"](
                        spotify_client,
                        access_token,
                        page_size,
                        offset,
                    ),
                    force_refresh=force_refresh,
                )
            total_count = max(int(payload.get("total", 0) or 0), total_count)
            has_more = bool(payload.get("has_more"))
            normalized_items.extend(config["normalizer"](payload.get("items", [])))
            if not has_more:
                break

        loaded_count = len(normalized_items)
        return {
            "items": normalized_items,
            "page": safe_page,
            "page_size": page_size,
            "loaded_count": loaded_count,
            "total_count": max(total_count, loaded_count),
            "has_more": has_more and loaded_count < max(total_count, loaded_count),
            "supports_incremental": True,
            "uses_partial_data": True,
        }

    def get_cache_status(self, tab: str) -> dict[str, Any]:
        tab = self.normalize_tab(tab)
        config = self._tab_config()[tab]
        metadata = self.cache_service.get_metadata(config["cache_key"])
        if metadata is None and config.get("page_cache_prefix"):
            metadata = self.cache_service.get_latest_metadata_by_prefix(config["page_cache_prefix"])
        if metadata is None:
            return {
                "has_cache": False,
                "is_stale": False,
                "fetched_at_label": "-",
                "expires_at_label": "-",
                "age_label": "Sin copia local",
            }

        return {
            "has_cache": True,
            "is_stale": bool(metadata["is_stale"]),
            "fetched_at_label": self._format_cache_datetime(metadata.get("fetched_at")),
            "expires_at_label": self._format_cache_datetime(metadata.get("expires_at")),
            "age_label": self._format_age_label(int(metadata.get("age_seconds", 0) or 0)),
        }

    def export_items_to_txt(
        self,
        tab: str,
        items: list[dict[str, Any]],
        file_stem: str = "biblioteca_personal",
    ) -> dict[str, Any]:
        tab = self.normalize_tab(tab)
        if tab == self.TAB_TRACKS:
            lines = [f"{index}. {item['title']} - {item['artist_names']}" for index, item in enumerate(items, start=1)]
        elif tab == self.TAB_ALBUMS:
            lines = [f"{index}. {item['title']} - {item['artist_names']}" for index, item in enumerate(items, start=1)]
        else:
            lines = [f"{index}. {item['title']}" for index, item in enumerate(items, start=1)]

        if not lines:
            raise PlaylistImportError("No hay elementos visibles para exportar en esta seccion.")

        filename = f"{sanitize_filename(file_stem)}_{tab}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        output_path = self.exports_dir / filename
        output_path.write_text("\n".join(lines), encoding="utf-8")
        return {
            "item_count": len(lines),
            "file_name": filename,
            "file_path": output_path,
            "tab": tab,
        }

    @classmethod
    def normalize_tab(cls, tab: str) -> str:
        clean = str(tab or cls.TAB_TRACKS).strip().lower()
        return clean if clean in cls.VALID_TABS else cls.TAB_TRACKS

    @staticmethod
    def filter_items(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
        query_key = query.strip().lower()
        if not query_key:
            return items
        return [item for item in items if query_key in item.get("search_text", "")]

    @classmethod
    def build_summary(cls, tab: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        tab = cls.normalize_tab(tab)
        if tab == cls.TAB_TRACKS:
            unique_artists = sorted(
                {artist.strip() for item in items for artist in item.get("artist_list", []) if artist.strip()}
            )
            albums = sorted({item.get("album", "").strip() for item in items if item.get("album", "").strip()})
            durations = [int(item.get("duration_ms", 0) or 0) for item in items]
            total_duration_minutes = round(sum(durations) / 60000) if durations else 0
            hours, minutes = divmod(total_duration_minutes, 60)
            duration_label = f"{hours} h {minutes:02d} min" if hours else f"{minutes} min"
            latest_added_at = max((item.get("added_at") or "" for item in items), default="")
            return {
                "primary_count": len(items),
                "secondary_count": len(unique_artists),
                "tertiary_count": len(albums),
                "duration_label": duration_label,
                "latest_added_at": latest_added_at,
                "primary_label": "tracks",
                "secondary_label": "artists",
                "tertiary_label": "albums",
            }

        if tab == cls.TAB_ALBUMS:
            unique_artists = sorted(
                {artist.strip() for item in items for artist in item.get("artist_list", []) if artist.strip()}
            )
            total_tracks = sum(int(item.get("track_total", 0) or 0) for item in items)
            latest_added_at = max((item.get("added_at") or "" for item in items), default="")
            return {
                "primary_count": len(items),
                "secondary_count": len(unique_artists),
                "tertiary_count": total_tracks,
                "duration_label": "-",
                "latest_added_at": latest_added_at,
                "primary_label": "albums",
                "secondary_label": "artists",
                "tertiary_label": "tracks",
            }

        genres = sorted({genre.strip() for item in items for genre in item.get("genres", []) if genre.strip()})
        followers = sum(int(item.get("followers", 0) or 0) for item in items)
        return {
            "primary_count": len(items),
            "secondary_count": len(genres),
            "tertiary_count": followers,
            "duration_label": "-",
            "latest_added_at": "",
            "primary_label": "artists",
            "secondary_label": "genres",
            "tertiary_label": "followers",
        }

    @classmethod
    def get_tab_copy(cls, tab: str) -> dict[str, str]:
        tab = cls.normalize_tab(tab)
        mapping = {
            cls.TAB_TRACKS: {
                "title": "Tus canciones guardadas",
                "subtitle": "Liked Songs completas, filtrables y exportables.",
                "search_label": "Buscar en tus canciones",
                "search_hint": "Buscamos sobre track, artista y album al mismo tiempo.",
                "search_placeholder": "Ej. Radiohead, Blonde, nights",
                "preview_title": "Canciones visibles",
            },
            cls.TAB_ALBUMS: {
                "title": "Tus albumes guardados",
                "subtitle": "Albumes guardados con artistas, numero de tracks y exportacion a TXT.",
                "search_label": "Buscar en tus albumes",
                "search_hint": "Buscamos sobre album, artista y fecha de guardado.",
                "search_placeholder": "Ej. In Rainbows, SZA, Currents",
                "preview_title": "Albumes visibles",
            },
            cls.TAB_ARTISTS: {
                "title": "Tus artistas seguidos",
                "subtitle": "Artistas seguidos con generos, seguidores y acceso rapido a Spotify.",
                "search_label": "Buscar en tus artistas",
                "search_hint": "Buscamos sobre nombre del artista y generos principales.",
                "search_placeholder": "Ej. Tame Impala, indie, electronic",
                "preview_title": "Artistas visibles",
            },
        }
        return mapping[tab]

    @classmethod
    def _tab_config(cls) -> dict[str, dict[str, Any]]:
        return {
            cls.TAB_TRACKS: {
                "cache_key": cls.SAVED_TRACKS_CACHE_KEY,
                "page_cache_prefix": f"{cls.SAVED_TRACKS_CACHE_KEY}:page:",
                "ttl_seconds": cls.SAVED_TRACKS_TTL_SECONDS,
                "source_endpoint": "get_saved_tracks",
                "fetcher": lambda client, token: client.get_saved_tracks(token),
                "page_fetcher": lambda client, token, limit, offset: client.get_saved_tracks_page(token, limit, offset),
                "normalizer": cls._normalize_saved_tracks,
                "supports_incremental": True,
                "page_size": cls.PREVIEW_PAGE_SIZE,
            },
            cls.TAB_ALBUMS: {
                "cache_key": cls.SAVED_ALBUMS_CACHE_KEY,
                "page_cache_prefix": f"{cls.SAVED_ALBUMS_CACHE_KEY}:page:",
                "ttl_seconds": cls.SAVED_ALBUMS_TTL_SECONDS,
                "source_endpoint": "get_saved_albums",
                "fetcher": lambda client, token: client.get_saved_albums(token),
                "page_fetcher": lambda client, token, limit, offset: client.get_saved_albums_page(token, limit, offset),
                "normalizer": cls._normalize_saved_albums,
                "supports_incremental": True,
                "page_size": cls.PREVIEW_PAGE_SIZE,
            },
            cls.TAB_ARTISTS: {
                "cache_key": cls.FOLLOWED_ARTISTS_CACHE_KEY,
                "page_cache_prefix": "",
                "ttl_seconds": cls.FOLLOWED_ARTISTS_TTL_SECONDS,
                "source_endpoint": "get_followed_artists",
                "fetcher": lambda client, token: client.get_followed_artists(token),
                "normalizer": cls._normalize_followed_artists,
                "supports_incremental": False,
                "page_size": cls.PREVIEW_PAGE_SIZE,
            },
        }

    @staticmethod
    def _build_page_cache_key(prefix: str, offset: int, limit: int) -> str:
        return f"{prefix}{offset}:{limit}"

    @staticmethod
    def _format_cache_datetime(value: datetime | None) -> str:
        if value is None:
            return "-"
        return value.strftime("%Y-%m-%d %H:%M:%S UTC")

    @staticmethod
    def _format_age_label(age_seconds: int) -> str:
        if age_seconds < 60:
            return f"Hace {age_seconds} s"
        if age_seconds < 3600:
            return f"Hace {round(age_seconds / 60)} min"
        if age_seconds < 86400:
            return f"Hace {round(age_seconds / 3600)} h"
        return f"Hace {round(age_seconds / 86400)} d"

    @staticmethod
    def _normalize_saved_tracks(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in items:
            track = item.get("track") or item.get("item") or {}
            if not track or track.get("type") == "episode" or track.get("is_local"):
                continue
            artists = track.get("artists") or []
            artist_list = [str(artist.get("name", "")).strip() for artist in artists if str(artist.get("name", "")).strip()]
            title = str(track.get("name", "Cancion sin titulo")).strip() or "Cancion sin titulo"
            album = str((track.get("album") or {}).get("name", "")).strip()
            added_at = str(item.get("added_at", "")).strip()
            normalized.append(
                {
                    "id": str(track.get("id", "")).strip(),
                    "title": title,
                    "artist_names": ", ".join(artist_list),
                    "artist_list": artist_list,
                    "album": album,
                    "spotify_url": (track.get("external_urls") or {}).get("spotify", ""),
                    "duration_ms": int(track.get("duration_ms", 0) or 0),
                    "added_at": added_at,
                    "search_text": f"{title} {' '.join(artist_list)} {album}".lower().strip(),
                }
            )
        return normalized

    @staticmethod
    def _normalize_saved_albums(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in items:
            album = item.get("album") or item.get("item") or {}
            if not album:
                continue
            artists = album.get("artists") or []
            artist_list = [str(artist.get("name", "")).strip() for artist in artists if str(artist.get("name", "")).strip()]
            title = str(album.get("name", "Album sin titulo")).strip() or "Album sin titulo"
            normalized.append(
                {
                    "id": str(album.get("id", "")).strip(),
                    "title": title,
                    "artist_names": ", ".join(artist_list),
                    "artist_list": artist_list,
                    "track_total": int(album.get("total_tracks", 0) or 0),
                    "release_date": str(album.get("release_date", "")).strip(),
                    "added_at": str(item.get("added_at", "")).strip(),
                    "spotify_url": (album.get("external_urls") or {}).get("spotify", ""),
                    "search_text": f"{title} {' '.join(artist_list)} {album.get('release_date', '')}".lower().strip(),
                }
            )
        return normalized

    @staticmethod
    def _normalize_followed_artists(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for artist in items:
            if not artist:
                continue
            title = str(artist.get("name", "Artista sin nombre")).strip() or "Artista sin nombre"
            genres = [str(genre).strip() for genre in artist.get("genres", []) if str(genre).strip()]
            normalized.append(
                {
                    "id": str(artist.get("id", "")).strip(),
                    "title": title,
                    "genres": genres,
                    "followers": int((artist.get("followers") or {}).get("total", 0) or 0),
                    "popularity": int(artist.get("popularity", 0) or 0),
                    "spotify_url": (artist.get("external_urls") or {}).get("spotify", ""),
                    "search_text": f"{title} {' '.join(genres)}".lower().strip(),
                }
            )
        return normalized
