"""Helper functions for AI Conversation component."""

from __future__ import annotations

from functools import lru_cache
import logging
import re
from typing import Any, Callable

from openai import AsyncAzureOpenAI, AsyncClient, AsyncOpenAI

from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.util.hass_dict import HassKey

from .const import (
    DEFAULT_MODEL_CONFIG,
    DEFAULT_TOKEN_PARAM,
    DOMAIN,
    MODEL_CONFIG_PATTERNS,
    MODEL_TOKEN_PARAMETER_SUPPORT,
)

_LOGGER = logging.getLogger(__name__)


AZURE_DOMAIN_PATTERN = r"\.(openai\.azure\.com|azure-api\.net|services\.ai\.azure\.com)"

DATA_EXPOSED_ENTITIES: HassKey[ExposedEntitiesCache] = HassKey(
    "hass_ai_conversation_exposed_entities"
)


class ExposedEntitiesCache:
    """Cache for exposed entities with fine-grained incremental updates.

    Instead of invalidating the whole cache on every state change, this cache
    updates only the affected entity record. Metadata (name/aliases) is kept
    stable across state-value changes (temperature, brightness, etc.), so the
    cache only rebuilds a single entry when an exposed entity is added,
    removed, or renamed.

    The cache also serves as the single source of truth that ``device_map``
    consumes, avoiding a second full traversal of states + entity registry.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        # entity_id -> record dict (entity_id, name, state, aliases, domain, area_id)
        self._records: dict[str, dict[str, Any]] = {}
        self._listener: Callable[[], None] | None = None
        self._listener_registry: Callable[[], None] | None = None
        self._built = False

    @callback
    def async_start(self) -> None:
        """Register state change + entity registry listeners."""
        self._listener = self._hass.bus.async_listen(
            "state_changed", self._async_on_state_changed
        )
        # Entity registry changes (rename, area change, add/remove) require
        # metadata refresh. state_changed does not always fire for those.
        self._listener_registry = self._hass.bus.async_listen(
            "entity_registry_updated", self._async_on_registry_updated
        )

    @callback
    def async_stop(self) -> None:
        """Remove listeners."""
        if self._listener:
            self._listener()
            self._listener = None
        if self._listener_registry:
            self._listener_registry()
            self._listener_registry = None
        self._records.clear()
        self._built = False

    def _ensure_built(self) -> None:
        """Build the cache lazily on first access."""
        if not self._built:
            self._rebuild_all()
            self._built = True

    def _rebuild_all(self) -> None:
        """Recompute every exposed entity record from scratch."""
        self._records.clear()
        states = [
            state
            for state in self._hass.states.async_all()
            if async_should_expose(self._hass, conversation.DOMAIN, state.entity_id)
        ]
        entity_registry = er.async_get(self._hass)
        for state in states:
            entity_id = state.entity_id
            entity = entity_registry.async_get(entity_id)
            aliases: list[str] = []
            if entity:
                aliases = er.async_get_entity_aliases(self._hass, entity)
            self._records[entity_id] = self._build_record(state, entity, aliases)

    @staticmethod
    def _build_record(
        state: Any, entity: er.RegistryEntry | None, aliases: list[str]
    ) -> dict[str, Any]:
        return {
            "entity_id": state.entity_id,
            "name": state.name,
            "state": state.state,
            "aliases": aliases,
            "domain": state.domain,
            "area_id": entity.area_id if entity else None,
        }

    @callback
    def _async_on_state_changed(self, event: Event) -> None:
        """Update only the affected entity record on state changes."""
        entity_id = event.data.get("entity_id", "")
        if not entity_id:
            return
        if not async_should_expose(self._hass, conversation.DOMAIN, entity_id):
            return

        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")

        # Entity removed.
        if new_state is None:
            self._records.pop(entity_id, None)
            return

        # Entity added or value/name changed: update the single record.
        record = self._records.get(entity_id)
        if record is None:
            # Newly exposed entity. Build its record.
            if not self._built:
                return  # Will be built lazily on first access.
            entity_registry = er.async_get(self._hass)
            entity = entity_registry.async_get(entity_id)
            aliases: list[str] = []
            if entity:
                aliases = er.async_get_entity_aliases(self._hass, entity)
            self._records[entity_id] = self._build_record(new_state, entity, aliases)
            return

        # In-place update of mutable fields. Name changes are rare; state changes
        # are frequent but only touch one key.
        record["state"] = new_state.state
        if old_state is None or old_state.name != new_state.name:
            record["name"] = new_state.name

    @callback
    def _async_on_registry_updated(self, event: Event) -> None:
        """Refresh metadata when entity registry changes (rename/area/aliases)."""
        entity_id = event.data.get("entity_id", "")
        if not entity_id:
            return
        if not async_should_expose(self._hass, conversation.DOMAIN, entity_id):
            # May have been un-exposed or never exposed.
            self._records.pop(entity_id, None)
            return
        state = self._hass.states.get(entity_id)
        if state is None:
            self._records.pop(entity_id, None)
            return
        if not self._built:
            return
        entity_registry = er.async_get(self._hass)
        entity = entity_registry.async_get(entity_id)
        aliases: list[str] = []
        if entity:
            aliases = er.async_get_entity_aliases(self._hass, entity)
        self._records[entity_id] = self._build_record(state, entity, aliases)

    def get(self) -> list[dict[str, Any]]:
        """Get cached exposed entities as a list of record copies.

        The cache self-manages via listeners; callers receive a snapshot list.
        """
        self._ensure_built()
        return list(self._records.values())

    def get_records(self) -> dict[str, dict[str, Any]]:
        """Return the live record map (entity_id -> record).

        Used by ``device_map`` to avoid a second full traversal. Callers must
        not mutate the returned mapping.
        """
        self._ensure_built()
        return self._records


def get_or_create_exposed_entities_cache(hass: HomeAssistant) -> ExposedEntitiesCache:
    """Get or create the exposed entities cache singleton."""
    if DATA_EXPOSED_ENTITIES not in hass.data:
        cache = ExposedEntitiesCache(hass)
        hass.data[DATA_EXPOSED_ENTITIES] = cache
        cache.async_start()
    return hass.data[DATA_EXPOSED_ENTITIES]


def get_exposed_entities(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Get exposed entities with caching.

    Uses ExposedEntitiesCache to avoid recomputing on every call.
    The cache is updated incrementally when exposed entity states change.
    """
    cache = get_or_create_exposed_entities_cache(hass)
    return cache.get()


@lru_cache(maxsize=64)
def get_model_config(model: str) -> dict[str, bool]:
    """Get model-specific parameter configuration.

    Cached because the model name rarely changes within a subentry's lifetime
    and the regex match is repeated on every conversation turn.
    """
    for entry in MODEL_CONFIG_PATTERNS:
        pattern = str(entry["pattern"])
        entry_config = entry["config"]
        if re.match(pattern, model, re.IGNORECASE):
            return (
                dict(entry_config)
                if isinstance(entry_config, dict)
                else DEFAULT_MODEL_CONFIG
            )

    return DEFAULT_MODEL_CONFIG


def is_azure_url(base_url: str | None) -> bool:
    """Check if the base URL is an Azure OpenAI URL."""
    return bool(base_url and re.search(AZURE_DOMAIN_PATTERN, base_url))


@lru_cache(maxsize=64)
def get_token_param_for_model(model: str) -> str:
    """Return the token parameter name for a model."""
    model_lower = model.lower()
    for entry in MODEL_TOKEN_PARAMETER_SUPPORT:
        if re.search(entry["pattern"], model_lower):
            return entry["token_param"]
    return DEFAULT_TOKEN_PARAM


def convert_to_template(
    settings: Any,
    template_keys: list[str] | None = None,
    hass: HomeAssistant | None = None,
) -> None:
    if template_keys is None:
        template_keys = ["data", "event_data", "target", "service"]
    _convert_to_template(settings, template_keys, hass, [])


def _convert_to_template(
    settings: Any,
    template_keys: list[str],
    hass: HomeAssistant | None,
    parents: list[str],
) -> None:
    if isinstance(settings, dict):
        for key, value in settings.items():
            if isinstance(value, str) and (
                key in template_keys or set(parents).intersection(template_keys)
            ):
                from homeassistant.helpers.template import Template

                settings[key] = Template(value, hass)
            if isinstance(value, dict):
                parents.append(key)
                _convert_to_template(value, template_keys, hass, parents)
                parents.pop()
            if isinstance(value, list):
                parents.append(key)
                for item in value:
                    _convert_to_template(item, template_keys, hass, parents)
                parents.pop()
    if isinstance(settings, list):
        for setting in settings:
            _convert_to_template(setting, template_keys, hass, parents)


async def get_authenticated_client(
    hass: HomeAssistant,
    api_key: str,
    base_url: str | None,
    api_version: str | None,
    organization: str | None,
    api_provider: str | None,
    skip_authentication: bool = False,
) -> AsyncClient:
    """Validate OpenAI authentication."""

    client: AsyncClient
    if base_url and (is_azure_url(base_url) or api_provider == "azure"):
        client = AsyncAzureOpenAI(
            api_key=api_key,
            azure_endpoint=base_url,
            api_version=api_version,
            organization=organization,
            http_client=get_async_client(hass),
        )
    else:
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            organization=organization,
            http_client=get_async_client(hass),
        )

    if skip_authentication:
        return client

    # Validate credentials by listing models. Consume the first page only.
    # ``client.models.list`` is an async method: awaiting it returns an
    # ``AsyncPage``. Running it through an executor would return the bare
    # ``AsyncPaginator``, which is not synchronously iterable, so call it
    # directly on the event loop.
    response = await client.models.list(timeout=10)
    for _ in response.data:
        break
    return client
