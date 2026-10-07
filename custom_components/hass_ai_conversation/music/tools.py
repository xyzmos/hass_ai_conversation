"""Music Assistant LLM tools.

Five tools exposed to the Assist LLM API, implemented against the
``music_assistant`` services (never the ``music_assistant_client`` package, so
this module is safe to import even without Music Assistant installed).

Tool names use the ``<domain>__`` prefix required by HA >= 2027.3
(``hass_ai_conversation__...``) so they are namespaced and never collide with
core tools.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, llm

from ..const import DOMAIN
from . import client, compat
from .compat import schema_lib as vol
from .target import resolve_player

_MEDIA_TYPES = [
    "artist",
    "album",
    "audiobook",
    "playlist",
    "podcast",
    "track",
    "radio",
]
# play_media additionally accepts 'folder'.
_PLAY_MEDIA_TYPES = _MEDIA_TYPES + ["folder"]
_ENQUEUE = ["play", "replace", "next", "replace_next", "add"]
_ORDER_BY = [
    "name",
    "name_desc",
    "sort_name",
    "sort_name_desc",
    "timestamp_added",
    "timestamp_added_desc",
    "last_played",
    "last_played_desc",
    "play_count",
    "play_count_desc",
    "year",
    "year_desc",
    "position",
    "position_desc",
    "artist_name",
    "artist_name_desc",
    "random",
    "random_play_count",
]


def _list_or_str(values: list[str]) -> Any:
    """Accept a single string or a list of strings."""
    return vol.All(cv.ensure_list, [vol.In(values)])


def _target_schema(extra: dict[Any, Any]) -> vol.Schema:
    """Common player-targeting fields: name / area / floor."""
    return vol.Schema(
        {
            vol.Optional("player_name"): cv.string,
            vol.Optional("area"): cv.string,
            vol.Optional("floor"): cv.string,
            **extra,
        }
    )


class _MusicTool(llm.Tool):
    """Base: resolve player + convert errors into model-recoverable payloads."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def _resolve(
        self, args: dict[str, Any], llm_context: llm.LLMContext
    ) -> tuple[str | None, dict[str, Any] | None]:
        return resolve_player(
            self.hass,
            llm_context,
            args.get("player_name"),
            args.get("area"),
            args.get("floor"),
        )

    @staticmethod
    def _clean(args: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in args.items() if v is not None}


class MusicSearchTool(_MusicTool):
    """Search Music Assistant (library + streaming providers)."""

    name = "hass_ai_conversation__music_search"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass)
        schema: dict[Any, Any] = {
            vol.Required("name"): cv.string,
            vol.Optional("media_type"): _list_or_str(_MEDIA_TYPES),
            vol.Optional("artist"): cv.string,
            vol.Optional("album"): cv.string,
            vol.Optional("limit"): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
            vol.Optional("library_only"): cv.boolean,
        }
        if compat.field_supported(hass, client._SEARCH, "username"):
            schema[vol.Optional("username")] = cv.string
        self.parameters = vol.Schema(schema)
        compat.annotate(
            self,
            title="Search Music",
            read_only=True,
            idempotent=True,
            integration=DOMAIN,
        )

    description = (
        "Search Music Assistant for music. Returns lists keyed by plural media "
        "type: 'artists', 'albums', 'tracks', 'playlists', 'podcasts', "
        "'audiobooks', 'radio'. Each item has 'uri', 'name' and 'media_type'; "
        "feed 'uri' (+ 'media_type') to hass_ai_conversation__music_play to "
        "play a result. Searches both the local library and streaming "
        "providers unless library_only is true."
    )

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> Any:
        args = self._clean(tool_input.tool_args)
        limit = args.get("limit")
        data = await client.search(
            hass,
            llm_context.context,
            name=args["name"],
            media_type=args.get("media_type"),
            artist=args.get("artist"),
            album=args.get("album"),
            limit=limit,
            library_only=args.get("library_only"),
            username=args.get("username"),
        )
        return compat.tool_result(client.compact_results(data, limit))


class MusicLibraryTool(_MusicTool):
    """Browse the local Music Assistant library."""

    name = "hass_ai_conversation__music_library"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass)
        schema: dict[Any, Any] = {
            vol.Required("media_type"): vol.In(_MEDIA_TYPES),
            vol.Optional("favorite"): cv.boolean,
            vol.Optional("search"): cv.string,
            vol.Optional("limit"): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=500)
            ),
            vol.Optional("offset"): vol.All(vol.Coerce(int), vol.Range(min=0)),
            vol.Optional("order_by"): vol.In(_ORDER_BY),
            # NOTE: 'album_type' is intentionally not exposed. Upstream declares
            # it as list[MediaType] (should be list[AlbumType]); MediaType's
            # _missing_ coercion silently maps single/ep/compilation to
            # 'unknown', so passing it filters incorrectly.
            vol.Optional("album_artists_only"): cv.boolean,
        }
        if compat.field_supported(hass, client._GET_LIBRARY, "username"):
            schema[vol.Optional("username")] = cv.string
        self.parameters = vol.Schema(schema)
        compat.annotate(
            self,
            title="Browse Music Library",
            read_only=True,
            idempotent=True,
            integration=DOMAIN,
        )

    description = (
        "Browse the Music Assistant *local library* (favourites / added items). "
        "Filter by media_type (required), favorite, or a free-text 'search'. "
        "Returns items with 'uri' + 'media_type' usable by "
        "hass_ai_conversation__music_play. Use for 'play my playlist' or "
        "'what albums do I have'; use music_search to find new music."
    )

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> Any:
        args = self._clean(tool_input.tool_args)
        data = await client.get_library(
            hass,
            llm_context.context,
            media_type=args["media_type"],
            favorite=args.get("favorite"),
            search=args.get("search"),
            limit=args.get("limit"),
            offset=args.get("offset"),
            order_by=args.get("order_by"),
            album_artists_only=args.get("album_artists_only"),
            username=args.get("username"),
        )
        return compat.tool_result(client.compact_results(data, args.get("limit")))


class MusicPlayTool(_MusicTool):
    """Play media on a Music Assistant player."""

    name = "hass_ai_conversation__music_play"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass)
        extra: dict[Any, Any] = {
            vol.Required("media_id"): vol.All(cv.ensure_list, [cv.string]),
            vol.Optional("media_type"): vol.In(_PLAY_MEDIA_TYPES),
            vol.Optional("enqueue"): vol.In(_ENQUEUE),
            vol.Optional("radio_mode"): cv.boolean,
        }
        if compat.field_supported(hass, client._PLAY_MEDIA, "artist"):
            extra[vol.Optional("artist")] = cv.string
            extra[vol.Optional("album")] = cv.string
        if compat.field_supported(hass, client._PLAY_MEDIA, "start_item"):
            extra[vol.Optional("start_item")] = cv.string
        if compat.field_supported(hass, client._PLAY_MEDIA, "username"):
            extra[vol.Optional("username")] = cv.string
        self.parameters = _target_schema(extra)
        compat.annotate(
            self,
            title="Play Music",
            read_only=False,
            integration=DOMAIN,
        )

    description = (
        "Play media on a Music Assistant player. 'media_id' is a URI or list of "
        "URIs obtained from music_search/music_library (e.g. 'spotify://track/…' "
        "or 'library://track/123'). Identify the player with player_name, area, "
        "or floor; omit them to use the requesting device's player. 'enqueue' "
        "controls how it is queued (default 'play'). Use radio_mode to start a "
        "radio based on a track/artist."
    )

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> Any:
        args = self._clean(tool_input.tool_args)
        entity_id, err = self._resolve(args, llm_context)
        if err is not None:
            return compat.tool_result(err)
        await client.play_media(
            hass,
            llm_context.context,
            entity_id,
            media_id=args["media_id"],
            media_type=args.get("media_type"),
            artist=args.get("artist"),
            album=args.get("album"),
            enqueue=args.get("enqueue"),
            radio_mode=args.get("radio_mode"),
            start_item=args.get("start_item"),
            username=args.get("username"),
        )
        return compat.tool_result(
            {"success": True, "entity_id": entity_id, "media_id": args["media_id"]}
        )


class MusicQueueTool(_MusicTool):
    """Read or transfer a Music Assistant player's queue."""

    name = "hass_ai_conversation__music_queue"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass)
        extra: dict[Any, Any] = {
            vol.Required("action"): vol.In(["get", "transfer"]),
            vol.Optional("source_player"): cv.string,
            vol.Optional("auto_play"): cv.boolean,
        }
        self.parameters = _target_schema(extra)
        compat.annotate(
            self,
            title="Music Queue",
            read_only=False,
            integration=DOMAIN,
        )

    description = (
        "Inspect or move a Music Assistant player's queue. action='get' returns "
        "the active queue (current/next item, count, shuffle/repeat). "
        "action='transfer' moves the queue from 'source_player' to the target "
        "player (or the other way, depending on which you target). Use to "
        "answer 'what's playing' on a player or to move playback to another room."
    )

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> Any:
        args = self._clean(tool_input.tool_args)
        entity_id, err = self._resolve(args, llm_context)
        if err is not None:
            return compat.tool_result(err)
        if args["action"] == "get":
            try:
                data = await client.get_queue(hass, llm_context.context, entity_id)
            except HomeAssistantError as exc:
                return compat.tool_result(
                    {"error": "no_queue", "error_text": str(exc)}
                )
            return compat.tool_result(
                {"entity_id": entity_id, "queue": client.compact_queue(data)}
            )
        # transfer
        source = args.get("source_player")
        if source:
            src_id, src_err = resolve_player(
                self.hass, llm_context, source, None, None
            )
            if src_err is not None:
                return compat.tool_result(src_err)
            source = src_id
        await client.transfer_queue(
            hass,
            llm_context.context,
            entity_id,
            source_player=source,
            auto_play=args.get("auto_play"),
        )
        return compat.tool_result(
            {"success": True, "entity_id": entity_id, "transferred_from": source}
        )


class MusicAnnounceTool(_MusicTool):
    """Play a TTS/URL announcement on a Music Assistant player."""

    name = "hass_ai_conversation__music_announce"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass)
        extra: dict[Any, Any] = {
            vol.Optional("url"): cv.string,
            vol.Optional("use_pre_announce"): cv.boolean,
            vol.Optional("pre_announce_url"): cv.string,
            vol.Optional("announce_volume"): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=100)
            ),
        }
        # message + tts_entity_id only exist on HA >= 2026.10.
        if compat.field_supported(
            hass, client._PLAY_ANNOUNCEMENT, "message"
        ):
            extra[vol.Optional("message")] = cv.string
        if compat.field_supported(
            hass, client._PLAY_ANNOUNCEMENT, "tts_entity_id"
        ):
            extra[vol.Optional("tts_entity_id")] = cv.string
        self.parameters = _target_schema(extra)
        compat.annotate(
            self,
            title="Music Announcement",
            read_only=False,
            integration=DOMAIN,
        )

    description = (
        "Play an announcement on a Music Assistant player (interrupts playback "
        "then resumes it). Provide 'url' to an audio file, or 'message' (with an "
        "optional 'tts_entity_id') for text-to-speech. 'use_pre_announce' plays "
        "a chime first. Identify the player with player_name/area/floor."
    )

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> Any:
        args = self._clean(tool_input.tool_args)
        if not args.get("url") and not args.get("message"):
            return compat.tool_result(
                {
                    "error": "missing_content",
                    "error_text": "Provide either 'url' or 'message'.",
                }
            )
        if args.get("url") and args.get("message"):
            # Upstream enforces AtMostOne(url, message) — fail fast instead of
            # letting the service schema raise an opaque error.
            return compat.tool_result(
                {
                    "error": "invalid_args",
                    "error_text": "'url' and 'message' are mutually exclusive.",
                }
            )
        entity_id, err = self._resolve(args, llm_context)
        if err is not None:
            return compat.tool_result(err)
        await client.play_announcement(
            hass,
            llm_context.context,
            entity_id,
            url=args.get("url"),
            message=args.get("message"),
            tts_entity_id=args.get("tts_entity_id"),
            use_pre_announce=args.get("use_pre_announce"),
            pre_announce_url=args.get("pre_announce_url"),
            announce_volume=args.get("announce_volume"),
        )
        return compat.tool_result({"success": True, "entity_id": entity_id})


# ---------------------------------------------------------------------------
# Prompt fragment merged into the system prompt by the Assist API.
# ---------------------------------------------------------------------------

MUSIC_PROMPT = """## Music Assistant

You can play and search music through Music Assistant via these tools:
- `hass_ai_conversation__music_search` – search library + streaming providers, returns `uri`/`media_type`.
- `hass_ai_conversation__music_library` – browse the local library/favourites.
- `hass_ai_conversation__music_play` – play a `uri` on a player (`player_name`/`area`/`floor` selects the speaker; omit to use the player's own device).
- `hass_ai_conversation__music_queue` – view or transfer the queue.
- `hass_ai_conversation__music_announce` – TTS/URL announcement on a player.

Workflow: first call music_search (or music_library for favourites), then pass the result's `uri` + `media_type` to music_play.

When naming a speaker use the device name or the room/area. If a tool returns `error` with a `candidates` list, choose a Music Assistant player from that list or ask the user which one — do not retry with random name variants. Only Music Assistant players support these tools; if the user names a speaker that is not a Music Assistant player, use the built-in `media_player` tools instead."""


def build_tools(hass: HomeAssistant) -> list[llm.Tool]:
    """Instantiate all Music Assistant tools."""
    return [
        MusicSearchTool(hass),
        MusicLibraryTool(hass),
        MusicPlayTool(hass),
        MusicQueueTool(hass),
        MusicAnnounceTool(hass),
    ]
