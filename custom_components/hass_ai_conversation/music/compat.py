"""Version-compatibility shims for the Music Assistant LLM tools.

Single shim surface for every HA-version difference this package touches:

* ``_to_openapi``   - probatio.to_openapi (HA >= 2026.9) vs
                      voluptuous_openapi.convert (HA < 2026.9)
* ``field_supported`` / ``supported_fields`` - which fields a Music Assistant
                      service actually accepts on *this* HA version, probed from
                      the live registered service schema.
* ``match_preferences_for`` - device-area/floor match preferences; the ready-made
                      ``llm.async_get_match_preferences`` only exists on
                      HA >= 2026.10, older versions derive it manually.
* ``device_area_id`` - ``dr.async_get_effective_area_id`` (HA >= 2026.9) vs the
                      plain ``device.area_id`` fallback (2026.8).
* ``tool_result``   - return ``llm.ToolResult`` (HA >= 2026.10) vs a plain dict.
* ``annotate``      - attach ``title``/``annotations``/``integration`` Tool attrs
                      (harmless no-op on HA < 2026.10 where they don't exist).
"""

from __future__ import annotations

from typing import Any, Callable

try:
    # HA >= 2026.9: probatio replaced voluptuous + voluptuous-openapi.
    # Tool.parameters must be a probatio.Schema there — probatio.to_openapi
    # CANNOT serialize a plain voluptuous Schema (it is an unhashable dict key).
    import probatio as schema_lib

    def _to_openapi(schema: Any, custom_serializer: Callable[..., Any] | None = None) -> dict[str, Any]:
        """Serialize a schema to an OpenAPI dict (probatio path)."""
        return schema_lib.to_openapi(
            schema, custom_serializer=custom_serializer
        )

except ImportError:  # HA < 2026.9
    import voluptuous as schema_lib
    from voluptuous_openapi import convert as _va_convert

    def _to_openapi(schema: Any, custom_serializer: Callable[..., Any] | None = None) -> dict[str, Any]:
        """Serialize a schema to an OpenAPI dict (voluptuous-openapi path)."""
        return _va_convert(schema, custom_serializer=custom_serializer)


from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, intent, llm

from . import MUSIC_ASSISTANT_DOMAIN

# ---------------------------------------------------------------------------
# Service schema field probing
# ---------------------------------------------------------------------------

# Cache is (schema_object, fields): entries are invalidated automatically when
# the registered schema object changes (e.g. Music Assistant reloaded with a
# different version), not just when the service name re-registers.
_fields_cache: dict[tuple[str, str], tuple[Any, frozenset[str] | None]] = {}


def supported_fields(hass: HomeAssistant, service: str) -> frozenset[str] | None:
    """Return the field names a ``music_assistant`` service accepts.

    Probed from the live registered service schema and serialized with
    ``_to_openapi`` (works for plain ``Schema`` and ``All(...)``-wrapped
    schemas on both the probatio and voluptuous-openapi paths).

    ``None``  -> service not registered (Music Assistant not installed) or the
               schema could not be read; callers must treat every field as
               unsupported EXCEPT hard-required ones they must keep.
    ``frozenset`` -> the accepted top-level field names.
    """
    key = (MUSIC_ASSISTANT_DOMAIN, service)
    services = hass.services.async_services()
    svc = services.get(MUSIC_ASSISTANT_DOMAIN, {}).get(service)
    schema = getattr(svc, "schema", None) if svc is not None else None

    cached = _fields_cache.get(key)
    if cached is not None and cached[0] is schema and cached[0] is not None:
        return cached[1]

    result: frozenset[str] | None = None
    if schema is not None:
        try:
            openapi = _to_openapi(schema)
            props = openapi.get("properties")
            if isinstance(props, dict):
                result = frozenset(props.keys())
        except Exception:  # noqa: BLE001 - probing is best-effort
            result = None

    _fields_cache[key] = (schema, result)
    return result


def field_supported(hass: HomeAssistant, service: str, field: str) -> bool:
    """Return True if ``music_assistant.<service>`` accepts ``field``."""
    fields = supported_fields(hass, service)
    if fields is None:
        return False
    return field in fields


def trim_service_data(
    hass: HomeAssistant, service: str, data: dict[str, Any]
) -> dict[str, Any]:
    """Drop keys the target service does not accept on this HA version.

    Unknown -> return ``data`` unchanged (let the service schema validate).
    """
    fields = supported_fields(hass, service)
    if fields is None:
        return data
    return {k: v for k, v in data.items() if k in fields}


def clear_fields_cache() -> None:
    """Drop the cached field map (Music Assistant reloaded / upgraded)."""
    _fields_cache.clear()


# ---------------------------------------------------------------------------
# Target-match preferences (device area/floor)
# ---------------------------------------------------------------------------


def device_area_id(hass: HomeAssistant, device_id: str | None) -> str | None:
    """Return the effective area_id of the requesting device."""
    if not device_id:
        return None
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    if hasattr(dr, "async_get_effective_area_id"):  # HA >= 2026.9
        return dr.async_get_effective_area_id(hass, device)
    return device.area_id  # HA 2026.8


def match_preferences_for(hass: HomeAssistant, llm_context: llm.LLMContext) -> Any:
    """Return ``intent.MatchTargetsPreferences`` for the requesting device.

    HA >= 2026.10 provides ``llm.async_get_match_preferences`` which also
    resolves the floor. On 2026.8/2026.9 we derive area (and floor via the
    area registry) by hand.
    """
    if hasattr(llm, "async_get_match_preferences"):  # HA >= 2026.10
        return llm.async_get_match_preferences(hass, llm_context)

    area_id = device_area_id(hass, llm_context.device_id)
    floor_id: str | None = None
    if area_id:
        area = None
        try:
            from homeassistant.helpers import area_registry as ar

            area = ar.async_get(hass).async_get_area(area_id)
        except Exception:  # noqa: BLE001
            area = None
        if area is not None:
            floor_id = getattr(area, "floor_id", None)
    return intent.MatchTargetsPreferences(area_id=area_id, floor_id=floor_id)


# ---------------------------------------------------------------------------
# Tool result + tool metadata (HA >= 2026.10 contract)
# ---------------------------------------------------------------------------


def tool_result(data: dict[str, Any]) -> Any:
    """Wrap ``data`` in ``llm.ToolResult`` on HA >= 2026.10, else return dict."""
    if hasattr(llm, "ToolResult"):  # HA >= 2026.10
        return llm.ToolResult(data=data)
    return data


def annotate(
    tool: Any,
    *,
    title: str,
    read_only: bool,
    destructive: bool = False,
    idempotent: bool = False,
    integration: str,
) -> None:
    """Attach HA >= 2026.10 Tool metadata; harmless on older versions.

    Setting the attributes on HA < 2026.10 is a no-op (they simply don't exist
    on the base class yet), so this is safe to call unconditionally.
    """
    tool.title = title
    tool.integration = integration
    if hasattr(llm, "ToolAnnotations"):  # HA >= 2026.10
        tool.annotations = llm.ToolAnnotations(
            read_only=read_only,
            destructive=destructive,
            idempotent=idempotent,
            open_world=True,
        )
