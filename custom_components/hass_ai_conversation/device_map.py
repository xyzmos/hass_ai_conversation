"""Device alias mapping system for Chinese device name resolution."""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util.hass_dict import HassKey

from .helpers import get_or_create_exposed_entities_cache

DATA_DEVICE_MAP: HassKey[DeviceAliasMap] = HassKey("hass_ai_conversation_device_map")

_LOGGER = logging.getLogger(__name__)

# Debounce delay (seconds) for map rebuilds triggered by registry changes.
# Multiple rapid changes within this window collapse into a single rebuild.
_REBUILD_DEBOUNCE_SECONDS = 30


class DeviceAliasMap:
    """Manages device alias to entity_id mapping with O(1) lookup.

    Supports:
    - Chinese device names to entity_id resolution
    - Fuzzy matching with pinyin normalization
    - Dynamic updates when entities change (consumes ExposedEntitiesCache)

    This map no longer maintains its own state_changed listener or traverses
    ``hass.states.async_all()``. It derives its data from the shared
    ``ExposedEntitiesCache``, which is the single source of truth for exposed
    entities. The alias index is rebuilt lazily on access after the cache
    reports changes (debounced).
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._alias_to_entity: dict[str, str] = {}
        self._entity_to_aliases: dict[str, list[str]] = {}
        self._entity_info: dict[str, dict[str, Any]] = {}
        self._domain_index: dict[str, list[str]] = {}
        self._area_index: dict[str, list[str]] = {}
        self._initialized = False
        self._rebuild_cancel: Callable[[], None] | None = None
        self._listener: Callable[[], None] | None = None
        self._listener_registry: Callable[[], None] | None = None

    @callback
    def async_start_listener(self) -> None:
        """Register listeners that trigger a debounced alias-index rebuild.

        The alias index only depends on entity names/aliases/domain/area, NOT
        on state values. So we listen to:
        - ``state_changed``: only when an exposed entity's *name* changes or it
          is added/removed (state-value updates are ignored).
        - ``entity_registry_updated``: renames, area changes, alias changes.
        """
        self._listener = self._hass.bus.async_listen(
            "state_changed", self._async_on_state_changed
        )
        self._listener_registry = self._hass.bus.async_listen(
            "entity_registry_updated", self._async_on_registry_updated
        )

    @callback
    def async_stop_listener(self) -> None:
        """Cancel any pending rebuild and remove listeners."""
        if self._listener:
            self._listener()
            self._listener = None
        if self._listener_registry:
            self._listener_registry()
            self._listener_registry = None
        if self._rebuild_cancel is not None:
            self._rebuild_cancel()
            self._rebuild_cancel = None

    @callback
    def _async_on_state_changed(self, event: Event) -> None:
        """Schedule a rebuild when an exposed entity's name changes or it is added/removed.

        The device map only depends on entity names/aliases/domain/area, NOT on
        state values (temperature, brightness, etc.). So we only rebuild when the
        name changes or an entity is added/removed, avoiding rebuilds on every
        sensor value update. A debounce prevents rapid rebuilds.
        """
        entity_id = event.data.get("entity_id", "")
        if not entity_id:
            return

        # Only care about conversation-exposed entities.
        if not async_should_expose(self._hass, conversation.DOMAIN, entity_id):
            return

        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")

        # Entity added or removed entirely.
        if old_state is None or new_state is None:
            self._schedule_rebuild()
            return

        # Only rebuild when the name actually changed, not on every value update.
        if old_state.name != new_state.name:
            self._schedule_rebuild()

    @callback
    def _async_on_registry_updated(self, event: Event) -> None:
        """Rebuild when the registry changes a name/area/alias of an exposed entity."""
        entity_id = event.data.get("entity_id", "")
        if not entity_id:
            return
        if not async_should_expose(self._hass, conversation.DOMAIN, entity_id):
            return
        self._schedule_rebuild()

    @callback
    def schedule_rebuild(self) -> None:
        """Schedule a debounced rebuild of the map (public)."""
        self._schedule_rebuild()

    @callback
    def _schedule_rebuild(self) -> None:
        """Schedule a debounced rebuild of the map.

        Cancels any pending rebuild and starts a new timer, so that many rapid
        changes collapse into a single rebuild.
        """
        if self._rebuild_cancel is not None:
            self._rebuild_cancel()
        self._rebuild_cancel = async_call_later(
            self._hass, _REBUILD_DEBOUNCE_SECONDS, self._async_do_rebuild
        )

    @callback
    def _async_do_rebuild(self, _now: Any) -> None:
        """Perform the actual rebuild after the debounce delay."""
        self._rebuild_cancel = None
        _LOGGER.debug("Rebuilding device alias map due to entity changes")
        self.async_build_map()

    @callback
    def async_build_map(self) -> None:
        """Build the alias mapping from the shared exposed-entities cache."""
        cache = get_or_create_exposed_entities_cache(self._hass)
        records = cache.get_records()

        self._alias_to_entity.clear()
        self._entity_to_aliases.clear()
        self._entity_info.clear()
        self._domain_index.clear()
        self._area_index.clear()

        for entity_id, record in records.items():
            domain = record.get("domain", entity_id.split(".")[0])
            name = record.get("name", "")
            aliases: list[str] = record.get("aliases", []) or []
            area_id = record.get("area_id")
            all_names = [name] + aliases

            self._entity_to_aliases[entity_id] = all_names
            self._entity_info[entity_id] = {
                "entity_id": entity_id,
                "name": name,
                "state": record.get("state"),
                "domain": domain,
                "area_id": area_id,
                "aliases": aliases,
            }

            for n in all_names:
                normalized = self._normalize_alias(n)
                if normalized and normalized not in self._alias_to_entity:
                    self._alias_to_entity[normalized] = entity_id

                short_alias = self._generate_short_alias(n, domain)
                if short_alias and short_alias not in self._alias_to_entity:
                    self._alias_to_entity[short_alias] = entity_id

            self._domain_index.setdefault(domain, []).append(entity_id)
            if area_id:
                self._area_index.setdefault(area_id, []).append(entity_id)

        self._initialized = True
        _LOGGER.debug(
            "Device alias map built: %d entities, %d aliases",
            len(self._entity_info),
            len(self._alias_to_entity),
        )

    def _ensure_built(self) -> None:
        """Build the map lazily if not yet initialized."""
        if not self._initialized:
            self.async_build_map()

    def resolve(self, name: str) -> str | None:
        """Resolve a device name to entity_id with O(1) lookup.

        Tries exact match first, then normalized forms.
        """
        self._ensure_built()

        if name in self._alias_to_entity:
            return self._alias_to_entity[name]

        normalized = self._normalize_alias(name)
        if normalized and normalized in self._alias_to_entity:
            return self._alias_to_entity[normalized]

        short = self._generate_short_alias(name, "")
        if short and short in self._alias_to_entity:
            return self._alias_to_entity[short]

        return None

    def resolve_fuzzy(self, name: str) -> list[tuple[str, float]]:
        """Fuzzy resolve a device name, returning candidates with confidence scores."""
        self._ensure_built()

        results: list[tuple[str, float]] = []
        normalized = self._normalize_alias(name)

        for alias, entity_id in self._alias_to_entity.items():
            score = self._similarity(normalized, alias)
            if score > 0.4:
                results.append((entity_id, score))

        results.sort(key=lambda x: x[1], reverse=True)
        seen: set[str] = set()
        unique: list[tuple[str, float]] = []
        for entity_id, score in results:
            if entity_id not in seen:
                seen.add(entity_id)
                unique.append((entity_id, score))

        return unique[:5]

    def get_entity_info(self, entity_id: str) -> dict[str, Any] | None:
        """Get entity info by entity_id."""
        self._ensure_built()
        return self._entity_info.get(entity_id)

    def get_entities_by_domain(self, domain: str) -> list[str]:
        """Get all entity_ids for a domain."""
        self._ensure_built()
        return self._domain_index.get(domain, [])

    def get_entities_by_area(self, area_id: str) -> list[str]:
        """Get all entity_ids in an area."""
        self._ensure_built()
        return self._area_index.get(area_id, [])

    def get_all_entities(self) -> list[dict[str, Any]]:
        """Get all entity info dicts."""
        self._ensure_built()
        return list(self._entity_info.values())

    def get_total_aliases(self) -> int:
        """Total number of alias entries (sum of per-entity alias lists)."""
        self._ensure_built()
        return sum(len(v) for v in self._entity_to_aliases.values())

    @staticmethod
    def _normalize_alias(alias: str) -> str:
        """Normalize an alias for matching: lowercase, strip whitespace and special chars."""
        normalized = alias.strip().lower()
        normalized = re.sub(r"[\s\-_]+", "", normalized)
        return normalized

    @staticmethod
    def _generate_short_alias(name: str, domain: str) -> str | None:
        """Generate a short alias from a Chinese device name.

        Examples:
        - '主卧氛围场景' -> '主卧氛围'
        - '最新水费金额' -> '水费'
        """
        suffixes_to_strip = [
            "场景", "模式", "金额", "数量", "状态", "开关",
            "传感器", "检测", "监测", "控制器",
        ]
        short = name
        for suffix in suffixes_to_strip:
            if short.endswith(suffix) and len(short) > len(suffix) + 1:
                short = short[: -len(suffix)]
                break

        if short != name:
            return short.lower()
        return None

    @staticmethod
    def _similarity(s1: str, s2: str) -> float:
        """Compute similarity between two strings.

        Uses a combination of character-bigram overlap (Sørensen–Dice) and
        length-based normalization, which is far more discriminative for
        Chinese strings than plain character-set Jaccard.
        """
        if not s1 or not s2:
            return 0.0
        if s1 == s2:
            return 1.0

        # Substring containment gives a strong signal (e.g. "卧室灯" in "主卧室灯").
        if s1 in s2 or s2 in s1:
            shorter = min(len(s1), len(s2))
            longer = max(len(s1), len(s2))
            return shorter / longer

        # Character bigram Sørensen–Dice coefficient.
        def bigrams(s: str) -> set[str]:
            return {s[i : i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}

        b1 = bigrams(s1)
        b2 = bigrams(s2)
        if not b1 or not b2:
            # Fall back to single-character Jaccard for very short strings.
            set1, set2 = set(s1), set(s2)
            union = set1 | set2
            return len(set1 & set2) / len(union) if union else 0.0

        intersection = len(b1 & b2)
        return (2.0 * intersection) / (len(b1) + len(b2))


@callback
def async_get_device_map(hass: HomeAssistant) -> DeviceAliasMap:
    """Get or create the device alias map singleton.

    The map derives its data from the shared ExposedEntitiesCache and rebuilds
    lazily; no separate state_changed listener is registered here.
    """
    if DATA_DEVICE_MAP not in hass.data:
        device_map = DeviceAliasMap(hass)
        hass.data[DATA_DEVICE_MAP] = device_map
        device_map.async_build_map()  # Build on first creation
        device_map.async_start_listener()  # Auto-update on relevant changes
    return hass.data[DATA_DEVICE_MAP]
