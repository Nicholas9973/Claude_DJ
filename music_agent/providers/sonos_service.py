"""Search any music service linked in the Sonos app (Qobuz, Spotify, TIDAL, ...).

Searches go through the Sonos household's own link to the service (created
once with `python -m music_agent.cli link`), so no separate API key is needed.
Changing service is a config change: [music] service = "Spotify".
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from soco.music_services import MusicService
from soco.music_services.data_structures import MediaMetadata, get_class

from music_agent.models import KINDS, MediaItem
from music_agent.providers.base import MusicProvider, ProviderError

CATEGORY = {"track": "tracks", "album": "albums", "playlist": "playlists", "artist": "artists"}
RELINK_HINT = "If this keeps happening, re-link the account with `python -m music_agent.cli link`."


class SonosServiceProvider(MusicProvider):
    """Searches a music service through the Sonos household's link to it.

    Args:
        service_name: Service name as shown in the Sonos app, e.g. ``"Qobuz"``.
        device_fn: Returns any speaker; called lazily so nothing touches the
            network until the first search.
    """

    def __init__(self, service_name: str, device_fn: Callable[[], Any]):
        self.service_name = service_name
        self.name = "".join(ch for ch in service_name.lower() if ch.isalnum())
        self._device_fn = device_fn
        self._service: MusicService | None = None
        self._categories: list[str] | None = None
        self._lock = threading.Lock()  # searches run in parallel worker threads
        self._native: OrderedDict[str, Any] = OrderedDict()  # ref -> SoCo item, for queueing

    # ------------------------------------------------------------ service setup
    @property
    def service(self) -> MusicService:
        """The SoCo music service, opened on first use.

        Raises:
            ProviderError: The service can't be opened (not linked, speakers offline).
        """
        with self._lock:
            if self._service is None:
                try:
                    self._service = MusicService(self.service_name, device=self._device_fn())
                except Exception as exc:
                    raise ProviderError(f"Cannot open music service '{self.service_name}': {exc}") from exc
            return self._service

    def categories(self) -> list[str]:
        """Search categories the service supports, loaded once.

        SoCo marks its category map as loaded *before* downloading it, so
        searches started at the same moment see an empty map ("does not
        support 'tracks'"), and a failed download leaves it empty for good.
        Loading it here, under a lock, avoids both.

        Returns:
            Category names, e.g. ``["tracks", "albums", ...]``.

        Raises:
            ProviderError: The map couldn't be loaded; the next call tries again.
        """
        service = self.service
        with self._lock:
            if self._categories:
                return self._categories
            try:
                categories = list(service.available_search_categories)
            except Exception as exc:
                categories = []
                error = exc
            else:
                error = None
            if not categories:
                service._search_prefix_map = None  # let SoCo download it again next time
                raise ProviderError(f"Could not load {self.service_name}'s search categories"
                                    f"{f': {error}' if error else ''}. Will retry on the next search.")
            self._categories = categories
            return categories

    # ------------------------------------------------------------ MusicProvider
    def search(self, query: str, kind: str = "track", limit: int = 10) -> list[MediaItem]:
        """Search the service; see ``MusicProvider.search``."""
        if kind not in KINDS:
            raise ProviderError(f"Unknown kind '{kind}'. Use one of {KINDS}.")
        category = CATEGORY[kind]
        available = self.categories()
        if category not in available:
            raise ProviderError(f"{self.service_name} cannot search {category} (supports {available}).")
        try:
            results = self.service.search(category=category, term=query, count=limit)
        except Exception as exc:
            raise ProviderError(f"{self.service_name} search failed: {exc}. {RELINK_HINT}") from exc

        items = []
        for raw in results:
            item = self._to_item(raw, kind)
            self._remember(item.ref, raw)
            items.append(item)
        return items

    def to_native(self, item: MediaItem) -> Any:
        """Return the SoCo object to queue: the cached search result, or one rebuilt from the id."""
        with self._lock:
            cached = self._native.get(item.ref)
        if cached is not None:
            return cached
        # Rebuild from the stored id, exactly as SoCo does when parsing a search.
        base = "MediaMetadata" if item.kind == "track" else "MediaCollection"
        cls = get_class(base + item.kind.title())
        return cls.from_music_service(self.service, {"id": item.id, "itemType": item.kind, "title": item.title})

    # ------------------------------------------------------------ account linking (CLI)
    def info(self) -> dict[str, Any]:
        """Return the service's name, sign-in type (``auth``) and Sonos service ``id``."""
        service = self.service
        return {"name": self.service_name, "auth": service.auth_type, "id": service.service_id}

    def begin_link(self) -> tuple[str, str | None]:
        """Start linking the service account to SoCo.

        Returns:
            ``(url to open in a browser, link code or None)``.
        """
        url = self.service.begin_authentication()
        return url, self.service.link_code

    def complete_link(self) -> None:
        """Finish linking after the person signed in at the URL from ``begin_link``."""
        self.service.complete_authentication()

    # ------------------------------------------------------------ internals
    def _to_item(self, raw: Any, requested_kind: str) -> MediaItem:
        """Convert a SoCo search result into a provider-neutral MediaItem."""
        md = getattr(raw, "metadata", {}) or {}
        track_md = md.get("track_metadata")
        track_md = getattr(track_md, "metadata", {}) if track_md is not None else {}
        kind = md.get("item_type") or requested_kind
        if kind not in KINDS:
            kind = "track" if isinstance(raw, MediaMetadata) else requested_kind
        duration = track_md.get("duration")
        return MediaItem(
            provider=self.name,
            kind=kind,
            id=str(md.get("id")),
            title=str(md.get("title", "")),
            artist=track_md.get("artist") or track_md.get("album_artist") or md.get("artist"),
            album=track_md.get("album"),
            duration_s=int(duration) if duration else None,
        )

    def _remember(self, ref: str, raw: Any, limit: int = 1000) -> None:
        """Cache the SoCo object for a ref (most recent ``limit`` kept) so queueing reuses it."""
        with self._lock:
            self._native[ref] = raw
            self._native.move_to_end(ref)
            while len(self._native) > limit:
                self._native.popitem(last=False)
