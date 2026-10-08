"""Resolve a Music Assistant ``media_player`` from an LLM-provided name/area.

This module is the fix for the failure mode seen in the wild:

    LLM tool execution error: No entities supported the required features
        (given name '音箱 音箱', area '主卧', domains media_player, features 4194816)
    LLM tool execution error: No entities matched the name
        (given name '音箱', area '主卧', domains media_player, features 4194816)

Core's ``media_player`` LLM tool matched a speaker by name+area, but it wasn't a
Music Assistant player so it lacked the required SEARCH_MEDIA|PLAY_MEDIA
features; the tool then returned an error the model could not recover from, so
it retried with mangled names ('音箱 音箱' -> '音箱') in a blind loop.

Here we:

* only ever match among ``platform == 'music_assistant'`` media_player entities
  whose ``original_device_class == 'speaker'`` — mirroring the upstream
  ``entity_device_classes=[SPEAKER]`` gate on play_media / play_announcement /
  transfer_queue / get_queue, so MA dashboard display entities can never be
  selected (they would raise ``ServiceNotSupported`` at the service layer);
* a name that only exists on a non-MA speaker produces a helpful diagnostic
  instead of a cryptic feature error;
* normalise duplicated names the model produces ('音箱 音箱' -> '音箱',
  '音箱音箱' -> '音箱');
* fall back to area/floor-only matching, then to a single-available-player
  guess, and finally return the list of usable Music Assistant players so the
  model can correct itself or ask the user;
* respect Assist exposure (``constraints.assistant``).
"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er, intent, llm

from . import MEDIA_PLAYER_DOMAIN, MUSIC_ASSISTANT_DOMAIN
from .compat import device_area_id, match_preferences_for

_WS_RE = re.compile(r"\s+")

_ERROR_NO_PLAYER = "no_player"
_ERROR_AMBIGUOUS = "ambiguous"
_ERROR_NOT_MA = "not_music_assistant"
_ERROR_NO_MA = "no_music_assistant_players"
_ERROR_NOT_EXPOSED = "player_not_exposed"


def _should_expose(hass: HomeAssistant, assistant: str, entity_id: str) -> bool:
    """Proxy ``async_should_expose`` without pinning its import location."""
    try:
        from homeassistant.components.homeassistant.exposed_entities import (
            async_should_expose,
        )
    except ImportError:  # pragma: no cover - extremely old cores
        return True
    return async_should_expose(hass, assistant, entity_id)


def _normalize(name: str | None) -> str | None:
    """Collapse whitespace and deduplicate a doubled player name.

    Handles '音箱 音箱' -> '音箱' and STT doublings like '音箱音箱' -> '音箱'.
    Returns None when nothing usable remains.
    """
    if not name:
        return None
    name = _WS_RE.sub(" ", name).strip()
    if not name:
        return None
    # 'a a a' -> 'a'
    parts = name.split(" ")
    deduped: list[str] = []
    for part in parts:
        if not deduped or deduped[-1] != part:
            deduped.append(part)
    name = " ".join(deduped)
    # 'xx' -> 'x' for a full-string doubling with no separator.
    if len(name) % 2 == 0:
        half = len(name) // 2
        if half and name[:half] == name[half:]:
            name = name[:half]
    return name or None


def _player_meta(
    hass: HomeAssistant, state: State, assistant: str | None = None
) -> dict[str, Any]:
    """Compact description of one player for error payloads / results."""
    entity_reg = er.async_get(hass)
    entry = entity_reg.async_get(state.entity_id)
    area_id = entry.area_id if entry else None
    if area_id is None and entry is not None and entry.device_id:
        area_id = device_area_id(hass, entry.device_id)
    meta: dict[str, Any] = {
        "entity_id": state.entity_id,
        "name": state.name,
        "state": state.state,
    }
    if area_id:
        meta["area_id"] = area_id
    if assistant:
        meta["exposed"] = _should_expose(hass, assistant, state.entity_id)
    return meta


def _is_speaker(entry: er.RegistryEntry) -> bool:
    """Mirror the upstream ``entity_device_classes=[SPEAKER]`` gate.

    The Music Assistant entity services (play_media, play_announcement,
    transfer_queue, get_queue) only accept ``MediaPlayerDeviceClass.SPEAKER``
    entities. A dashboard display entity is also a
    ``platform == 'music_assistant'`` media_player but has no device class, so
    the service raises ``ServiceNotSupported`` ('does not support action …')
    when addressed. ``original_device_class`` is a StrEnum / str / None.
    """
    dc = entry.original_device_class
    return dc == "speaker" or getattr(dc, "value", None) == "speaker"


def _ma_players(hass: HomeAssistant) -> tuple[list[State], er.EntityRegistry]:
    """Return (playable MA speaker states, entity registry)."""
    entity_reg = er.async_get(hass)
    ma_ids = {
        e.entity_id
        for e in entity_reg.entities.values()
        if e.platform == MUSIC_ASSISTANT_DOMAIN
        and e.domain == MEDIA_PLAYER_DOMAIN
        and _is_speaker(e)
    }
    states = [
        s
        for s in hass.states.async_all(MEDIA_PLAYER_DOMAIN)
        if s.entity_id in ma_ids
    ]
    return states, entity_reg


def _match(
    hass: HomeAssistant,
    states: list[State],
    llm_context: llm.LLMContext,
    *,
    name: str | None,
    area_name: str | None,
    floor_name: str | None,
    single_target: bool,
    check_exposure: bool = True,
) -> intent.MatchTargetsResult:
    constraints = intent.MatchTargetsConstraints(
        name=name,
        area_name=area_name,
        floor_name=floor_name,
        domains=[MEDIA_PLAYER_DOMAIN],
        assistant=llm_context.assistant if check_exposure else None,
        allow_duplicate_names=True,
        single_target=single_target,
    )
    preferences = match_preferences_for(hass, llm_context)
    return intent.async_match_targets(
        hass, constraints, preferences, states=list(states)
    )


def _best_entity(
    hass: HomeAssistant, result: intent.MatchTargetsResult, llm_context: llm.LLMContext
) -> State | None:
    """Pick a single state from a match result, preferring the requester's area."""
    states = list(result.states)
    if not states:
        return None
    if len(states) == 1:
        return states[0]
    # Disambiguate by the requesting device's area.
    area_id = device_area_id(hass, llm_context.device_id)
    if area_id:
        entity_reg = er.async_get(hass)
        in_area = []
        for s in states:
            entry = entity_reg.async_get(s.entity_id)
            e_area = entry.area_id if entry else None
            if e_area is None and entry is not None and entry.device_id:
                e_area = device_area_id(hass, entry.device_id)
            if e_area == area_id:
                in_area.append(s)
        if len(in_area) == 1:
            return in_area[0]
    return None


def _fail(
    code: str,
    text: str,
    hass: HomeAssistant,
    candidates: list[State] | None = None,
    llm_context: llm.LLMContext | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": code, "error_text": text}
    if candidates is None:
        candidates, _ = _ma_players(hass)
    if candidates:
        assistant = llm_context.assistant if llm_context else None
        payload["candidates"] = [
            _player_meta(hass, s, assistant) for s in candidates
        ]
        payload["hint"] = (
            "Target a Music Assistant media_player from 'candidates' by its exact "
            "entity_id or name. Entities with 'exposed': false are blocked from "
            "voice control until the user exposes them to this assistant. "
            "Ask the user if ambiguous."
        )
    return payload


def _exposed_fail(
    hass: HomeAssistant,
    llm_context: llm.LLMContext,
    ma_states: list[State],
    *,
    name: str | None,
    area_name: str | None,
    floor_name: str | None,
) -> dict[str, Any] | None:
    """Fail with the names of matching-but-unexposed players, if any.

    ``async_match_targets`` drops entities not exposed to the assistant as its
    LAST step, so a correct name/area can still end in MatchFailedReason.
    ASSISTANT. Re-run the same constraints without the assistant constraint to
    find which entities were blocked; if none match, the failure was not about
    exposure and we return None so the caller continues its normal fallbacks.
    """
    if not llm_context.assistant:
        return None
    result = _match(
        hass,
        ma_states,
        llm_context,
        name=name,
        area_name=area_name,
        floor_name=floor_name,
        single_target=False,
        check_exposure=False,
    )
    if not result.states:
        return None
    unexposed = [
        s
        for s in result.states
        if not _should_expose(hass, llm_context.assistant, s.entity_id)
    ]
    payload = _fail(
        _ERROR_NOT_EXPOSED,
        "These Music Assistant players match but are not exposed to this "
        "assistant, so they cannot be controlled by voice. Tell the user to "
        "expose them: Settings → Voice assistants → Expose entities (or the "
        "entity's settings → 'Expose to voice assistants').",
        hass,
        unexposed or list(result.states),
        llm_context,
    )
    # Only surface alternative *exposed* candidates to retry with.
    exposed = [
        s
        for s in ma_states
        if _should_expose(hass, llm_context.assistant, s.entity_id)
    ]
    payload["exposed_candidates"] = [
        _player_meta(hass, s, llm_context.assistant) for s in exposed
    ]
    return payload


def resolve_player(
    hass: HomeAssistant,
    llm_context: llm.LLMContext,
    player_name: str | None,
    area_name: str | None,
    floor_name: str | None = None,
    single_target: bool = True,
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve to ``(entity_id, None)`` or ``(None, error_payload)``.

    ``error_payload`` always has ``error``/``error_text`` and, when known, a
    ``candidates`` list of usable Music Assistant players.
    """
    ma_states, _ = _ma_players(hass)

    if not ma_states:
        return None, {
            "error": _ERROR_NO_MA,
            "error_text": (
                "No playable Music Assistant speaker entities are available. "
                "Ensure the Music Assistant integration is configured and its "
                "players are exposed to the assistant."
            ),
        }

    name = player_name.strip() if player_name else None

    # 1. Direct match on the name the model supplied.
    if name or area_name or floor_name:
        result = _match(
            hass,
            ma_states,
            llm_context,
            name=name,
            area_name=area_name,
            floor_name=floor_name,
            single_target=single_target,
        )
        best = _best_entity(hass, result, llm_context)
        if best is not None:
            return best.entity_id, None
        if result.states:
            # Matched (or MULTIPLE_TARGETS) but couldn't narrow to one -> ambiguous.
            return None, _fail(
                _ERROR_AMBIGUOUS,
                f"Multiple Music Assistant players match '{name or area_name}'. "
                "Ask the user which one, or pass a more specific name/area.",
                hass,
                result.states,
                llm_context,
            )
        if result.no_match_reason == intent.MatchFailedReason.ASSISTANT:
            # Entities matched but are not exposed to this assistant.
            if fail := _exposed_fail(
                hass,
                llm_context,
                ma_states,
                name=name,
                area_name=area_name,
                floor_name=floor_name,
            ):
                return None, fail
        if result.no_match_reason in (
            intent.MatchFailedReason.INVALID_AREA,
            intent.MatchFailedReason.INVALID_FLOOR,
        ):
            return None, _fail(
                _ERROR_NO_PLAYER,
                f"Unknown area/floor (given name {name!r}, area {area_name!r}, "
                f"floor {floor_name!r}).",
                hass,
            )

    # 2. Retry with a normalised name ('音箱 音箱' -> '音箱', '音箱音箱' -> '音箱').
    norm = _normalize(name)
    if norm and norm != name:
        result = _match(
            hass,
            ma_states,
            llm_context,
            name=norm,
            area_name=area_name,
            floor_name=floor_name,
            single_target=single_target,
        )
        best = _best_entity(hass, result, llm_context)
        if best is not None:
            return best.entity_id, None
        if result.states:
            return None, _fail(
                _ERROR_AMBIGUOUS,
                f"Multiple Music Assistant players match '{norm}'. "
                "Ask the user which one, or pass a more specific name/area.",
                hass,
                result.states,
                llm_context,
            )
        if result.no_match_reason == intent.MatchFailedReason.ASSISTANT:
            if fail := _exposed_fail(
                hass,
                llm_context,
                ma_states,
                name=norm,
                area_name=area_name,
                floor_name=floor_name,
            ):
                return None, fail

    # 3. Area/floor-only match (name didn't resolve but a location did).
    if area_name or floor_name:
        result = _match(
            hass,
            ma_states,
            llm_context,
            name=None,
            area_name=area_name,
            floor_name=floor_name,
            single_target=single_target,
        )
        best = _best_entity(hass, result, llm_context)
        if best is not None:
            return best.entity_id, None
        if result.states:
            return None, _fail(
                _ERROR_AMBIGUOUS,
                f"Multiple Music Assistant players in {area_name or floor_name}. "
                "Ask the user which one.",
                hass,
                result.states,
                llm_context,
            )
        if result.no_match_reason == intent.MatchFailedReason.ASSISTANT:
            if fail := _exposed_fail(
                hass,
                llm_context,
                ma_states,
                name=None,
                area_name=area_name,
                floor_name=floor_name,
            ):
                return None, fail

    # 4. Distinguish 'not an MA player' from 'no such player at all'.
    if name and not (area_name or floor_name):
        all_players = hass.states.async_all(MEDIA_PLAYER_DOMAIN)
        result = _match(
            hass,
            all_players,
            llm_context,
            name=name,
            area_name=None,
            floor_name=None,
            single_target=False,
        )
        if result.is_match and result.states:
            return None, _fail(
                _ERROR_NOT_MA,
                f"'{name}' matched media_player(s) that are NOT provided by the "
                "Music Assistant integration and cannot play via Music Assistant. "
                "Use the built-in media_player.play_media tool for those, or pick "
                "a Music Assistant player from 'candidates'.",
                hass,
                ma_states,
                llm_context,
            )

    # 5. Single available player -> use it when nothing was specified.
    if not name and not area_name and not floor_name:
        if len(ma_states) == 1:
            return ma_states[0].entity_id, None
        return None, _fail(
            _ERROR_AMBIGUOUS,
            "No player specified and multiple Music Assistant players exist. "
            "Ask the user which player to use.",
            hass,
            ma_states,
            llm_context,
        )

    return None, _fail(
        _ERROR_NO_PLAYER,
        f"No Music Assistant player matched "
        f"(name {name!r}, area {area_name!r}, floor {floor_name!r}).",
        hass,
        ma_states,
        llm_context,
    )
