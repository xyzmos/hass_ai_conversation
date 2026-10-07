"""Config flow for OpenAI Conversation integration."""

from __future__ import annotations

import logging
import types
from typing import Any

from openai._exceptions import APIConnectionError, AuthenticationError
import voluptuous as vol
import yaml

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_API_KEY, CONF_LLM_HASS_API, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TemplateSelector,
    TextSelector,
    TextSelectorConfig,
)

from .const import (
    API_PROVIDERS,
    CONF_ADVANCED_OPTIONS,
    CONF_API_PROVIDER,
    CONF_API_VERSION,
    CONF_BASE_URL,
    CONF_CHAT_MODEL,
    CONF_CONTEXT_ROLLING_WINDOW_SIZE,
    CONF_CONTEXT_THRESHOLD,
    CONF_CONTEXT_TRUNCATE_STRATEGY,
    CONF_FUNCTION_TOOLS,
    CONF_IMAGE_MODEL,
    CONF_MAX_FUNCTION_CALLS_PER_CONVERSATION,
    CONF_MAX_TOKENS,
    CONF_ORGANIZATION,
    CONF_PROMPT,
    CONF_REASONING_EFFORT,
    CONF_SERVICE_TIER,
    CONF_SHORTEN_TOOL_CALL_ID,
    CONF_SKIP_AUTHENTICATION,
    CONF_TEMPERATURE,
    CONF_TOP_P,
    CONTEXT_TRUNCATE_STRATEGIES,
    DEFAULT_ADVANCED_OPTIONS,
    DEFAULT_AI_TASK_NAME,
    DEFAULT_AI_TASK_OPTIONS,
    DEFAULT_API_PROVIDER,
    DEFAULT_CHAT_MODEL,
    DEFAULT_CONF_BASE_URL,
    DEFAULT_CONF_FUNCTION_TOOLS,
    DEFAULT_CONTEXT_ROLLING_WINDOW_SIZE,
    DEFAULT_CONTEXT_THRESHOLD,
    DEFAULT_CONTEXT_TRUNCATE_STRATEGY,
    DEFAULT_CONVERSATION_NAME,
    DEFAULT_MAX_FUNCTION_CALLS_PER_CONVERSATION,
    DEFAULT_MAX_TOKENS,
    DEFAULT_NAME,
    DEFAULT_PROMPT,
    DEFAULT_REASONING_EFFORT,
    DEFAULT_SERVICE_TIER,
    DEFAULT_SHORTEN_TOOL_CALL_ID,
    DEFAULT_SKIP_AUTHENTICATION,
    DEFAULT_TEMPERATURE,
    DEFAULT_TOP_P,
    DOMAIN,
    REASONING_EFFORT_OPTIONS,
    SERVICE_TIER_OPTIONS,
)
from .helpers import get_authenticated_client, get_model_config

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_NAME, default="ChatGPT"): str,
        vol.Required(CONF_API_KEY): str,
        vol.Optional(CONF_BASE_URL, default=DEFAULT_CONF_BASE_URL): str,
        vol.Optional(CONF_API_VERSION): str,
        vol.Optional(CONF_ORGANIZATION): str,
        vol.Optional(
            CONF_SKIP_AUTHENTICATION, default=DEFAULT_SKIP_AUTHENTICATION
        ): bool,
        vol.Optional(CONF_API_PROVIDER, default=DEFAULT_API_PROVIDER): SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(
                        value=api_provider["key"], label=api_provider["label"]
                    )
                    for api_provider in API_PROVIDERS
                ],
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
    }
)

DEFAULT_CONF_FUNCTION_TOOLS_STR = yaml.dump(
    DEFAULT_CONF_FUNCTION_TOOLS, sort_keys=False, allow_unicode=True
)

DEFAULT_OPTIONS = types.MappingProxyType(
    {
        CONF_PROMPT: DEFAULT_PROMPT,
        CONF_CHAT_MODEL: DEFAULT_CHAT_MODEL,
        CONF_MAX_TOKENS: DEFAULT_MAX_TOKENS,
        CONF_MAX_FUNCTION_CALLS_PER_CONVERSATION: DEFAULT_MAX_FUNCTION_CALLS_PER_CONVERSATION,
        CONF_TOP_P: DEFAULT_TOP_P,
        CONF_TEMPERATURE: DEFAULT_TEMPERATURE,
        CONF_FUNCTION_TOOLS: DEFAULT_CONF_FUNCTION_TOOLS_STR,
        CONF_CONTEXT_THRESHOLD: DEFAULT_CONTEXT_THRESHOLD,
        CONF_CONTEXT_TRUNCATE_STRATEGY: DEFAULT_CONTEXT_TRUNCATE_STRATEGY,
        CONF_CONTEXT_ROLLING_WINDOW_SIZE: DEFAULT_CONTEXT_ROLLING_WINDOW_SIZE,
        CONF_SHORTEN_TOOL_CALL_ID: DEFAULT_SHORTEN_TOOL_CALL_ID,
        CONF_ADVANCED_OPTIONS: DEFAULT_ADVANCED_OPTIONS,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> None:
    """Validate the user input allows us to connect."""
    api_key = data[CONF_API_KEY]
    base_url = data.get(CONF_BASE_URL)
    api_version = data.get(CONF_API_VERSION)
    organization = data.get(CONF_ORGANIZATION)
    skip_authentication = data.get(CONF_SKIP_AUTHENTICATION, False)
    api_provider = data.get(CONF_API_PROVIDER)

    if base_url == DEFAULT_CONF_BASE_URL:
        base_url = None
        data.pop(CONF_BASE_URL)

    if api_provider == "azure" and not base_url:
        raise HomeAssistantError("Azure OpenAI requires a custom base URL.")

    await get_authenticated_client(
        hass=hass,
        api_key=api_key,
        base_url=base_url,
        api_version=api_version,
        organization=organization,
        api_provider=api_provider,
        skip_authentication=skip_authentication,
    )


def build_advanced_schema(
    model_config: dict[str, bool],
) -> dict[Any, Any]:
    """Build the advanced options schema based on model capabilities.

    Shared between conversation and ai_task subentry flows.
    """
    schema: dict[Any, Any] = {}

    if model_config["supports_top_p"]:
        schema[
            vol.Optional(
                CONF_TOP_P,
                default=DEFAULT_TOP_P,
            )
        ] = NumberSelector(NumberSelectorConfig(min=0, max=1, step=0.05))

    if model_config["supports_temperature"]:
        schema[
            vol.Optional(
                CONF_TEMPERATURE,
                default=DEFAULT_TEMPERATURE,
            )
        ] = NumberSelector(NumberSelectorConfig(min=0, max=2, step=0.05))

    if model_config.get("supports_reasoning_effort"):
        schema[
            vol.Optional(
                CONF_REASONING_EFFORT,
                default=DEFAULT_REASONING_EFFORT,
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=opt, label=opt.capitalize())
                    for opt in REASONING_EFFORT_OPTIONS
                ],
                mode=SelectSelectorMode.DROPDOWN,
            )
        )

    if model_config.get("supports_service_tier"):
        schema[
            vol.Optional(
                CONF_SERVICE_TIER,
                default=DEFAULT_SERVICE_TIER,
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=opt, label=opt.capitalize())
                    for opt in SERVICE_TIER_OPTIONS
                ],
                mode=SelectSelectorMode.DROPDOWN,
            )
        )

    schema[
        vol.Optional(
            CONF_SHORTEN_TOOL_CALL_ID,
            default=DEFAULT_SHORTEN_TOOL_CALL_ID,
        )
    ] = BooleanSelector()

    return schema


class ExtendedOpenAIConversationConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OpenAI Conversation."""

    VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        if user_input is None:
            return self.async_show_form(
                step_id="user", data_schema=STEP_USER_DATA_SCHEMA
            )

        errors = {}

        try:
            await validate_input(self.hass, user_input)
        except APIConnectionError:
            errors["base"] = "cannot_connect"
        except AuthenticationError:
            errors["base"] = "invalid_auth"
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"
        else:
            return self.async_create_entry(
                title=user_input.get(CONF_NAME, DEFAULT_NAME),
                data=user_input,
                subentries=[
                    {
                        "subentry_type": "conversation",
                        "data": dict(DEFAULT_OPTIONS),
                        "title": DEFAULT_CONVERSATION_NAME,
                        "unique_id": None,
                    },
                    {
                        "subentry_type": "ai_task_data",
                        "data": dict(DEFAULT_AI_TASK_OPTIONS),
                        "title": DEFAULT_AI_TASK_NAME,
                        "unique_id": None,
                    },
                ],
            )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return subentries supported by this integration."""
        return {
            "conversation": ExtendedOpenAISubentryFlowHandler,
            "ai_task_data": ExtendedOpenAIAITaskSubentryFlowHandler,
        }


class ExtendedOpenAISubentryFlowHandler(ConfigSubentryFlow):
    """Flow for managing OpenAI subentries."""

    options: dict[str, Any]
    _temp_data: dict[str, Any] | None = None

    @property
    def _is_new(self) -> bool:
        """Return if this is a new subentry."""
        return self.source == "user"

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a subentry."""
        self.options = dict(DEFAULT_OPTIONS)
        return await self.async_step_init()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle reconfiguration of a subentry."""
        self.options = dict(self._get_reconfigure_subentry().data)
        return await self.async_step_init()

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Manage the options."""
        if self._get_entry().state != ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")

        if user_input is not None:
            if user_input.get(CONF_ADVANCED_OPTIONS, False):
                self._temp_data = user_input
                return await self.async_step_advanced()

            if self._is_new:
                title = user_input.get(CONF_NAME, DEFAULT_NAME)
                if CONF_NAME in user_input:
                    del user_input[CONF_NAME]
                return self.async_create_entry(
                    title=title,
                    data=user_input,
                )
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                data=user_input,
            )

        schema = self.openai_config_option_schema(self.options)

        if self._is_new:
            schema = {
                vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
                **schema,
            }

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(schema), self.options
            ),
        )

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle advanced options step."""
        if user_input is not None:
            final_data = {**(self._temp_data or {}), **user_input}

            if self._is_new:
                title = final_data.get(CONF_NAME, DEFAULT_NAME)
                final_data.pop(CONF_NAME, None)
                return self.async_create_entry(
                    title=title,
                    data=final_data,
                )
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                data=final_data,
            )

        chat_model = (self._temp_data or {}).get(CONF_CHAT_MODEL, DEFAULT_CHAT_MODEL)
        model_config = get_model_config(chat_model)

        schema = build_advanced_schema(model_config)

        return self.async_show_form(
            step_id="advanced",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(schema), self.options
            ),
        )

    def openai_config_option_schema(
        self, options: dict[str, Any]
    ) -> dict:
        """Return a schema for OpenAI completion options."""
        apis = llm.async_get_apis(self.hass)
        api_options = [
            SelectOptionDict(value=api.id, label=api.name)
            for api in apis
        ]

        current_llm_api = options.get(CONF_LLM_HASS_API, [llm.LLM_API_ASSIST])

        schema: dict = {
            vol.Optional(
                CONF_PROMPT,
                default=DEFAULT_PROMPT,
            ): TemplateSelector(),
            vol.Optional(
                CONF_CHAT_MODEL,
                default=DEFAULT_CHAT_MODEL,
            ): str,
            vol.Optional(
                CONF_MAX_TOKENS,
                default=DEFAULT_MAX_TOKENS,
            ): int,
            vol.Optional(
                CONF_MAX_FUNCTION_CALLS_PER_CONVERSATION,
                default=DEFAULT_MAX_FUNCTION_CALLS_PER_CONVERSATION,
            ): int,
            vol.Optional(
                CONF_LLM_HASS_API,
                default=current_llm_api,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=api_options,
                    mode=SelectSelectorMode.DROPDOWN,
                    multiple=True,
                )
            ),
            vol.Optional(
                CONF_FUNCTION_TOOLS,
                default=DEFAULT_CONF_FUNCTION_TOOLS_STR,
            ): TextSelector(TextSelectorConfig(multiline=True)),
            vol.Optional(
                CONF_CONTEXT_THRESHOLD,
                default=DEFAULT_CONTEXT_THRESHOLD,
            ): int,
            vol.Optional(
                CONF_CONTEXT_TRUNCATE_STRATEGY,
                default=DEFAULT_CONTEXT_TRUNCATE_STRATEGY,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=strategy["key"], label=strategy["label"])
                        for strategy in CONTEXT_TRUNCATE_STRATEGIES
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_CONTEXT_ROLLING_WINDOW_SIZE,
                default=DEFAULT_CONTEXT_ROLLING_WINDOW_SIZE,
            ): NumberSelector(NumberSelectorConfig(min=2, max=50, step=1)),
            vol.Optional(
                CONF_ADVANCED_OPTIONS,
                default=DEFAULT_ADVANCED_OPTIONS,
            ): BooleanSelector(),
        }

        return schema


class ExtendedOpenAIAITaskSubentryFlowHandler(ConfigSubentryFlow):
    """Flow for managing AI Task subentries."""

    options: dict[str, Any]
    _temp_data: dict[str, Any] | None = None

    @property
    def _is_new(self) -> bool:
        """Return if this is a new subentry."""
        return self.source == "user"

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a subentry."""
        self.options = dict(DEFAULT_AI_TASK_OPTIONS)
        return await self.async_step_init()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle reconfiguration of a subentry."""
        self.options = dict(self._get_reconfigure_subentry().data)
        return await self.async_step_init()

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Manage the options."""
        if self._get_entry().state != ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")

        if user_input is not None:
            if user_input.get(CONF_ADVANCED_OPTIONS, False):
                self._temp_data = user_input
                return await self.async_step_advanced()

            if self._is_new:
                title = user_input.get(CONF_NAME, DEFAULT_AI_TASK_NAME)
                if CONF_NAME in user_input:
                    del user_input[CONF_NAME]
                return self.async_create_entry(
                    title=title,
                    data=user_input,
                )
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                data=user_input,
            )

        schema: dict = {}

        if self._is_new:
            schema[vol.Optional(CONF_NAME, default=DEFAULT_AI_TASK_NAME)] = str

        schema.update(
            {
                vol.Optional(
                    CONF_CHAT_MODEL,
                    default=DEFAULT_CHAT_MODEL,
                ): str,
                vol.Optional(
                    CONF_IMAGE_MODEL,
                ): str,
                vol.Optional(
                    CONF_MAX_TOKENS,
                    default=DEFAULT_MAX_TOKENS,
                ): int,
                vol.Optional(
                    CONF_ADVANCED_OPTIONS,
                    default=DEFAULT_ADVANCED_OPTIONS,
                ): BooleanSelector(),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(schema), self.options
            ),
        )

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle advanced options step."""
        if user_input is not None:
            final_data = {**(self._temp_data or {}), **user_input}

            if self._is_new:
                title = final_data.get(CONF_NAME, DEFAULT_AI_TASK_NAME)
                final_data.pop(CONF_NAME, None)
                return self.async_create_entry(
                    title=title,
                    data=final_data,
                )
            return self.async_update_and_abort(
                self._get_entry(),
                self._get_reconfigure_subentry(),
                data=final_data,
            )

        chat_model = (self._temp_data or {}).get(CONF_CHAT_MODEL, DEFAULT_CHAT_MODEL)
        model_config = get_model_config(chat_model)

        schema = build_advanced_schema(model_config)

        return self.async_show_form(
            step_id="advanced",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(schema), self.options
            ),
        )
