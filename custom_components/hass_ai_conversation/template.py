"""Template functions for AI Conversation."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant

from .const import DEFAULT_WORKING_DIRECTORY, DOMAIN
from .helpers import get_exposed_entities

if TYPE_CHECKING:
    from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)

DATA_TEMPLATE_MANAGER = "template_manager"

TEMPLATE_AI_CONVERSATION = "ai_conversation"
TEMPLATE_GET_ENTITIES = "exposed_entities"
TEMPLATE_WORKING_DIRECTORY = "working_directory"

# HassData key holding the existing template environment globals dict.
_TEMPLATE_ENVIRONMENT_KEY = "template.environment"


async def async_setup_templates(hass: HomeAssistant) -> bool:
    """Set up template functions for AI Conversation.

    Injects the ``ai_conversation`` global into the shared template environment
    globals dict (the same mechanism Home Assistant itself uses for built-in
    template helpers). This avoids monkey-patching ``TemplateEnvironment.__init__``,
    which previously affected every template render across all integrations and
    risked breaking on unload when multiple instances were present.
    """
    hass.data.setdefault(DOMAIN, {})
    if hass.data[DOMAIN].get(DATA_TEMPLATE_MANAGER):
        return True

    manager = AIConversationTemplateManager(hass)
    hass.data[DOMAIN][DATA_TEMPLATE_MANAGER] = manager
    await manager.async_setup()
    return True


async def async_unload_templates(hass: HomeAssistant) -> bool:
    """Unload template functions for AI Conversation."""
    if len(hass.config_entries.async_entries(DOMAIN)) == 1:
        manager = hass.data.get(DOMAIN, {}).get(DATA_TEMPLATE_MANAGER)
        if manager:
            await manager.async_on_unload()
            hass.data[DOMAIN].pop(DATA_TEMPLATE_MANAGER, None)
    return True


class AIConversationTemplateManager:
    """Manages template functions."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the template manager."""
        self.hass = hass
        self._ai_conversation = {
            TEMPLATE_GET_ENTITIES: self._get_exposed_entities,
            TEMPLATE_WORKING_DIRECTORY: self._get_working_directory,
        }

    def _get_exposed_entities(self) -> list[dict[str, Any]]:
        return get_exposed_entities(self.hass)

    def _get_working_directory(self) -> str:
        """Get the absolute working directory path."""
        working_dir = DEFAULT_WORKING_DIRECTORY
        if Path(working_dir).is_absolute():
            return str(Path(working_dir))
        return str(Path(self.hass.config.config_dir) / working_dir)

    async def async_setup(self) -> None:
        """Inject the ai_conversation global into the shared template environment."""
        _LOGGER.debug("Setting up AI Conversation template functions")
        env_globals = self.hass.data.get(_TEMPLATE_ENVIRONMENT_KEY)
        if env_globals is not None and hasattr(env_globals, "globals"):
            env_globals.globals[TEMPLATE_AI_CONVERSATION] = self._ai_conversation
        else:
            _LOGGER.debug(
                "Template environment not yet initialized; "
                "ai_conversation global will be unavailable until it is"
            )

    async def async_on_unload(self) -> None:
        """Remove the ai_conversation global from the shared template environment."""
        _LOGGER.debug("Tearing down AI Conversation template functions")
        env_globals = self.hass.data.get(_TEMPLATE_ENVIRONMENT_KEY)
        if env_globals is not None and hasattr(env_globals, "globals"):
            env_globals.globals.pop(TEMPLATE_AI_CONVERSATION, None)
