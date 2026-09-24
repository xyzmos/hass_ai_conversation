"""AI Conversation agent entity."""

from __future__ import annotations

import logging
from typing import Any, Literal

from openai import OpenAIError
import yaml

from homeassistant.components import conversation
from homeassistant.components.conversation import (
    ChatLog,
    ConversationEntity,
    ConversationEntityFeature,
    ConversationInput,
    ConversationResult,
    async_get_chat_log,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import CONF_LLM_HASS_API, MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import intent, llm
from homeassistant.helpers.chat_session import async_get_chat_session
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ExtendedOpenAIConfigEntry
from .const import (
    CONF_FUNCTION_TOOLS,
    CONF_PROMPT,
    DEFAULT_CONF_FUNCTION_TOOLS,
    DEFAULT_PROMPT,
    DOMAIN,
    EVENT_CONVERSATION_FINISHED,
)
from .device_map import async_get_device_map
from .entity import ExtendedOpenAIBaseLLMEntity
from .exceptions import FunctionLoadFailed, FunctionNotFound, InvalidFunction
from .functions import get_function
from .helpers import get_exposed_entities

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ExtendedOpenAIConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the OpenAI Conversation entities."""
    for subentry in config_entry.subentries.values():
        if subentry.subentry_type != "conversation":
            continue

        async_add_entities(
            [ExtendedOpenAIAgentEntity(config_entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


class ExtendedOpenAIAgentEntity(
    ConversationEntity,
    conversation.AbstractConversationAgent,
    ExtendedOpenAIBaseLLMEntity,
):
    """Extended OpenAI conversation agent."""

    _attr_supports_streaming = True
    _attr_supported_features = ConversationEntityFeature.CONTROL

    def __init__(
        self, entry: ExtendedOpenAIConfigEntry, subentry: ConfigSubentry
    ) -> None:
        """Initialize the entity."""
        super().__init__(entry, subentry)
        self._cached_function_tools: list[dict[str, Any]] | None = None
        self._cached_function_tools_config: str | None = None

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return a list of supported languages."""
        return MATCH_ALL

    async def async_added_to_hass(self) -> None:
        """When entity is added to Home Assistant."""
        await super().async_added_to_hass()
        conversation.async_set_agent(self.hass, self.entry, self)

        # Device map now auto-updates via state_changed listener.
        # The initial build is triggered by the listener on first state_changed event.
        device_map = async_get_device_map(self.hass)

    async def async_will_remove_from_hass(self) -> None:
        """When entity will be removed from Home Assistant."""
        conversation.async_unset_agent(self.hass, self.entry)
        await super().async_will_remove_from_hass()

    async def async_process(self, user_input: ConversationInput) -> ConversationResult:
        """Process a sentence."""
        with (
            async_get_chat_session(self.hass, user_input.conversation_id) as session,
            async_get_chat_log(self.hass, session, user_input) as chat_log,
        ):
            return await self._async_handle_message(user_input, chat_log)

    async def _async_handle_message(
        self,
        user_input: ConversationInput,
        chat_log: ChatLog,
    ) -> ConversationResult:
        """Call the API."""
        llm_context = user_input.as_llm_context(DOMAIN)

        options = self.subentry.data
        try:
            await chat_log.async_provide_llm_data(
                llm_context,
                options.get(CONF_LLM_HASS_API),
                options.get(CONF_PROMPT, DEFAULT_PROMPT),
                user_input.extra_system_prompt,
            )
        except conversation.ConverseError as err:
            return err.as_conversation_result()

        exposed_entities = self._get_exposed_entities()
        function_tools = self._get_function_tools()

        try:
            await self._async_handle_chat_log(
                chat_log,
                function_tools=function_tools,
                exposed_entities=exposed_entities,
                llm_context=llm_context,
            )
        except OpenAIError as err:
            _LOGGER.error("Communication error with AI service: %s", err)
            intent_response = intent.IntentResponse(language=user_input.language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                f"Error communicating with AI service: {err}",
            )
            return conversation.ConversationResult(
                response=intent_response, conversation_id=user_input.conversation_id
            )
        except HomeAssistantError as err:
            _LOGGER.error("Conversation processing error: %s", err, exc_info=True)
            intent_response = intent.IntentResponse(language=user_input.language)
            intent_response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                f"Error processing request: {err}",
            )
            return conversation.ConversationResult(
                response=intent_response, conversation_id=user_input.conversation_id
            )

        self.hass.bus.async_fire(
            EVENT_CONVERSATION_FINISHED,
            {
                "conversation_id": user_input.conversation_id,
                "agent_id": self.subentry.subentry_id,
            },
        )

        return conversation.async_get_result_from_chat_log(user_input, chat_log)

    def _get_exposed_entities(self) -> list[dict[str, Any]]:
        return get_exposed_entities(self.hass)

    def _get_function_tools(self) -> list[dict[str, Any]]:
        """Get custom functions configuration with caching."""
        try:
            function_tools_config = self.subentry.data.get(CONF_FUNCTION_TOOLS)
            # Use cached tools if config hasn't changed
            if function_tools_config == self._cached_function_tools_config and self._cached_function_tools is not None:
                return self._cached_function_tools

            function_tools: list[dict[str, Any]] | None = (
                yaml.safe_load(function_tools_config)
                if function_tools_config
                else DEFAULT_CONF_FUNCTION_TOOLS
            )
            if function_tools:
                for function_tool in function_tools:
                    if isinstance(function_tool, dict) and "function" in function_tool:
                        function_config = function_tool["function"]
                        if (
                            isinstance(function_config, dict)
                            and "type" in function_config
                        ):
                            function = get_function(function_config["type"])
                            function_tool["function"] = function.validate_schema(
                                function_config
                            )

            # Cache the parsed result
            self._cached_function_tools = function_tools or []
            self._cached_function_tools_config = function_tools_config

            return self._cached_function_tools
        except (InvalidFunction, FunctionNotFound):
            raise
        except Exception as e:
            raise FunctionLoadFailed() from e
