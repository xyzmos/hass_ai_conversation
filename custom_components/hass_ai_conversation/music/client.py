"""Thin Music Assistant service-call layer.

Everything goes through ``hass.services.async_call`` so this package never
imports ``music_assistant_client`` (see ``music/__init__.py``). Response-returning
services (``search``/``get_library``/``get_queue``) are called with
``blocking=True, return_response=True``; entity-level responses are unwrapped
from ``{entity_id: payload}``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from . import MUSIC_ASSISTANT_DOMAIN, compat

_SEARCH = "search"
_GET_LIBRARY = "get_library"
_GET_QUEUE = "get_queue"
_PLAY_MEDIA = "play_media"
_PLAY_ANNOUNCEMENT = "play_announcement"
_TRANSFER_QUEUE = "transfer_queue"


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    """Drop None values (LLMs may pass explicit null for optional fields)."""
    return {k: v for k, v in data.items() if v is not None}


def _coerce_enums(value: Any) -> Any:
    """Convert Enum members to their value for JSON-safe tool results."""
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, dict):
        return {k: _coerce_enums(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce_enums(v) for v in value]
    return value


def config_entry_id(hass: HomeAssistant) -> str:
    """Return the entry_id of a loaded Music Assistant config entry.

    With several loaded entries the first one is used — config-entry-level
    services (search/get_library) aggregate across providers anyway, so this
    only matters for edge setups.
    """
    entries = hass.config_entries.async_entries(MUSIC_ASSISTANT_DOMAIN)
    for entry in entries:
        if entry.state == ConfigEntryState.LOADED:
            return entry.entry_id
    raise HomeAssistantError(
        "Music Assistant is not configured. "
        "Set up the Music Assistant integration first."
    )


async def call_action(
    hass: HomeAssistant,
    service: str,
    data: dict[str, Any],
    context: Context,
) -> None:
    """Call a Music Assistant service that returns no response."""
    await hass.services.async_call(
        MUSIC_ASSISTANT_DOMAIN,
        service,
        compat.trim_service_data(hass, service, _clean(data)),
        blocking=True,
        context=context,
    )


async def call_query(
    hass: HomeAssistant,
    service: str,
    data: dict[str, Any],
    context: Context,
    entity_id: str | None = None,
) -> Any:
    """Call a ``SupportsResponse.ONLY`` Music Assistant service.

    For entity-level services (``get_queue``) the response is keyed by
    entity_id; pass ``entity_id`` to unwrap it.
    """
    response = await hass.services.async_call(
        MUSIC_ASSISTANT_DOMAIN,
        service,
        compat.trim_service_data(hass, service, _clean(data)),
        blocking=True,
        return_response=True,
        context=context,
    )
    if entity_id is not None and isinstance(response, dict):
        return _coerce_enums(response.get(entity_id))
    return _coerce_enums(response)


async def search(
    hass: HomeAssistant,
    context: Context,
    name: str,
    media_type: list[str] | None = None,
    artist: str | None = None,
    album: str | None = None,
    limit: int | None = None,
    library_only: bool | None = None,
    username: str | None = None,
) -> Any:
    """Search the Music Assistant library + providers. Returns media lists."""
    return await call_query(
        hass,
        _SEARCH,
        {
            "config_entry_id": config_entry_id(hass),
            "name": name,
            "media_type": media_type,
            "artist": artist,
            "album": album,
            "limit": limit,
            "library_only": library_only,
            "username": username,
        },
        context,
    )


async def get_library(
    hass: HomeAssistant,
    context: Context,
    media_type: str,
    favorite: bool | None = None,
    search: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    order_by: str | None = None,
    album_artists_only: bool | None = None,
    username: str | None = None,
) -> Any:
    """Browse the local Music Assistant library.

    ``album_type`` is deliberately omitted: upstream declares it
    ``list[MediaType]`` instead of ``list[AlbumType]`` (HA 2026.8+), so values
    like 'single'/'ep' coerce to MediaType.UNKNOWN and filter incorrectly.
    """
    return await call_query(
        hass,
        _GET_LIBRARY,
        {
            "config_entry_id": config_entry_id(hass),
            "media_type": media_type,
            "favorite": favorite,
            "search": search,
            "limit": limit,
            "offset": offset,
            "order_by": order_by,
            "album_artists_only": album_artists_only,
            "username": username,
        },
        context,
    )


async def play_media(
    hass: HomeAssistant,
    context: Context,
    entity_id: str,
    media_id: list[str],
    media_type: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    enqueue: str | None = None,
    radio_mode: bool | None = None,
    start_item: str | None = None,
    username: str | None = None,
) -> None:
    """Play media on a Music Assistant player."""
    await call_action(
        hass,
        _PLAY_MEDIA,
        {
            "entity_id": entity_id,
            "media_id": media_id,
            "media_type": media_type,
            "artist": artist,
            "album": album,
            "enqueue": enqueue,
            "radio_mode": radio_mode,
            "start_item": start_item,
            "username": username,
        },
        context,
    )


async def play_announcement(
    hass: HomeAssistant,
    context: Context,
    entity_id: str,
    url: str | None = None,
    message: str | None = None,
    tts_entity_id: str | None = None,
    use_pre_announce: bool | None = None,
    pre_announce_url: str | None = None,
    announce_volume: int | None = None,
) -> None:
    """Play an announcement (URL or TTS) on a Music Assistant player."""
    await call_action(
        hass,
        _PLAY_ANNOUNCEMENT,
        {
            "entity_id": entity_id,
            "url": url,
            "message": message,
            "tts_entity_id": tts_entity_id,
            "use_pre_announce": use_pre_announce,
            "pre_announce_url": pre_announce_url,
            "announce_volume": announce_volume,
        },
        context,
    )


async def get_queue(
    hass: HomeAssistant, context: Context, entity_id: str
) -> Any:
    """Return the active queue details for a Music Assistant player."""
    return await call_query(
        hass, _GET_QUEUE, {"entity_id": entity_id}, context, entity_id=entity_id
    )


async def transfer_queue(
    hass: HomeAssistant,
    context: Context,
    entity_id: str,
    source_player: str | None = None,
    auto_play: bool | None = None,
) -> None:
    """Transfer the queue to (or from) a Music Assistant player."""
    await call_action(
        hass,
        _TRANSFER_QUEUE,
        {
            "entity_id": entity_id,
            "source_player": source_player,
            "auto_play": auto_play,
        },
        context,
    )


def compact_item(item: Any) -> dict[str, Any]:
    """Reduce a Music Assistant ItemMapping to the fields an LLM needs."""
    if not isinstance(item, dict):
        return {"name": str(item)}
    out: dict[str, Any] = {}
    for key in ("media_type", "name", "uri"):
        if item.get(key) is not None:
            out[key] = _coerce_enums(item[key])
    artists = item.get("artists")
    if artists:
        out["artists"] = [
            a.get("name") if isinstance(a, dict) else str(a) for a in artists
        ]
    return out


def compact_results(data: Any, limit: int | None = None) -> Any:
    """Compact a search/get_library response: {media_type: [item, ...]}."""
    if not isinstance(data, dict):
        return data
    out: dict[str, Any] = {}
    for media_type, items in data.items():
        if isinstance(items, list):
            out[media_type] = [compact_item(i) for i in items][:limit]
        else:
            out[media_type] = items
    return out


def _compact_queue_item(item: Any) -> Any:
    """Strip a queue item to the fields an LLM needs to speak/act on."""
    if not isinstance(item, dict):
        return item
    out: dict[str, Any] = {}
    for key in ("queue_item_id", "name", "duration", "stream_title"):
        if item.get(key) is not None:
            out[key] = _coerce_enums(item[key])
    media_item = item.get("media_item")
    if isinstance(media_item, dict):
        compact_media = compact_item(media_item)
        if compact_media:
            out["media_item"] = compact_media
    return out


def compact_queue(data: Any, max_items: int = 30) -> Any:
    """Compact a ``music_assistant.get_queue`` response.

    Keeps queue metadata and a bounded, trimmed items list so a full playlist
    does not blow up the LLM context.
    """
    if not isinstance(data, dict):
        return data
    out = dict(data)
    items = data.get("items")
    if isinstance(items, list):
        total = len(items)
        out["items"] = [_compact_queue_item(i) for i in items[:max_items]]
        out["total_items"] = total
        if total > max_items:
            out["items_truncated"] = True
    for key in ("current_item", "next_item"):
        if key in out:
            out[key] = _compact_queue_item(out[key])
    return out
