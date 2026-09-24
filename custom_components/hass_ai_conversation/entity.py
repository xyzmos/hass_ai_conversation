"""Base entity for AI Conversation."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncGenerator
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any, Callable

from openai import AsyncClient, AsyncStream
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionChunk,
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)
import orjson
import voluptuous as vol
from voluptuous_openapi import UNSUPPORTED, convert

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigSubentry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, llm
from homeassistant.helpers.entity import Entity
from homeassistant.util import slugify

from .const import (
    CONF_CHAT_MODEL,
    CONF_CONTEXT_ROLLING_WINDOW_SIZE,
    CONF_CONTEXT_THRESHOLD,
    CONF_CONTEXT_TRUNCATE_STRATEGY,
    CONF_MAX_FUNCTION_CALLS_PER_CONVERSATION,
    CONF_MAX_TOKENS,
    CONF_REASONING_EFFORT,
    CONF_SERVICE_TIER,
    CONF_SHORTEN_TOOL_CALL_ID,
    CONF_TEMPERATURE,
    CONF_TOP_P,
    DEFAULT_CHAT_MODEL,
    DEFAULT_CONTEXT_ROLLING_WINDOW_SIZE,
    DEFAULT_CONTEXT_THRESHOLD,
    DEFAULT_CONTEXT_TRUNCATE_STRATEGY,
    DEFAULT_MAX_FUNCTION_CALLS_PER_CONVERSATION,
    DEFAULT_MAX_TOKENS,
    DEFAULT_REASONING_EFFORT,
    DEFAULT_SERVICE_TIER,
    DEFAULT_SHORTEN_TOOL_CALL_ID,
    DEFAULT_TEMPERATURE,
    DEFAULT_TOP_P,
    DOMAIN,
)
from .exceptions import (
    ContentFilterError,
    FunctionNotFound,
    ParseArgumentsFailed,
    TokenLengthExceededError,
)
from .functions import get_function
from .helpers import get_model_config

if TYPE_CHECKING:
    from . import ExtendedOpenAIConfigEntry

_LOGGER = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 20


def _shorten_tool_call_id(tool_call_id: str) -> str:
    """Shorten tool call ID to exactly 9 alphanumeric characters as Mistral requires."""
    return hashlib.sha256(tool_call_id.encode()).hexdigest()[:9]


def _adjust_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Adjust the schema to be compatible with OpenAI API.

    Returns a deep-copied and modified version of the schema to avoid
    mutating the original schema object.
    """
    schema = copy.deepcopy(schema)
    if schema["type"] == "object":
        schema.setdefault("strict", True)
        schema.setdefault("additionalProperties", False)
        if "properties" not in schema:
            return schema

        if "required" not in schema:
            schema["required"] = []

        for prop, prop_info in schema["properties"].items():
            prop_info = _adjust_schema(prop_info)
            schema["properties"][prop] = prop_info
            if prop not in schema["required"]:
                prop_info["type"] = [prop_info["type"], "null"]
                schema["required"].append(prop)

    elif schema["type"] == "array":
        if "items" not in schema:
            return schema

        schema["items"] = _adjust_schema(schema["items"])

    return schema


def _normalize_custom_serializer(
    custom_serializer: Callable[..., Any] | None,
) -> Callable[..., Any] | None:
    """Wrap an external custom serializer to translate foreign markers.

    voluptuous-openapi recognises only its own UNSUPPORTED sentinel, while
    Home Assistant's llm.selector_serializer returns its own _Unsupported
    marker. Without this adapter, such markers would leak into the generated
    JSON schema and break serialization to the LLM API.
    """

    if custom_serializer is None:
        return None

    def wrapped(schema: Any) -> Any:
        result = custom_serializer(schema)
        if isinstance(result, dict):
            return result
        return UNSUPPORTED

    return wrapped


def _format_structured_output(
    schema: vol.Schema, llm_api: llm.APIInstance | None
) -> dict[str, Any]:
    """Format the schema to be compatible with OpenAI API."""
    result: dict[str, Any] = convert(
        schema,
        custom_serializer=(
            _normalize_custom_serializer(llm_api.custom_serializer)
            if llm_api
            else _normalize_custom_serializer(llm.selector_serializer)
        ),
    )

    result = _adjust_schema(result)

    return result


def _format_llm_tool(
    tool: llm.Tool, custom_serializer: Any | None
) -> ChatCompletionToolParam:
    """Format an LLM tool to OpenAI function tool format."""
    return ChatCompletionToolParam(
        type="function",
        function={
            "name": tool.name,
            "description": tool.description or "",
            "parameters": convert(
                tool.parameters,
                custom_serializer=_normalize_custom_serializer(custom_serializer),
            ),
        },
    )


def _convert_content_to_param(
    chat_content: list[conversation.Content],
    shorten_tool_call_id: bool = False,
) -> list[ChatCompletionMessageParam]:
    """Convert chat log content to OpenAI message format."""
    messages: list[ChatCompletionMessageParam] = []

    for content in chat_content:
        if content.role == "system":
            messages.append({"role": "system", "content": content.content})
        elif content.role == "user":
            messages.append({"role": "user", "content": content.content})
        elif content.role == "assistant":
            msg: ChatCompletionAssistantMessageParam = {"role": "assistant"}
            if content.content:
                msg["content"] = content.content
            if content.tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": _shorten_tool_call_id(tool_call.id)
                        if shorten_tool_call_id
                        else tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.tool_name,
                            "arguments": json.dumps(tool_call.tool_args),
                        },
                    }
                    for tool_call in content.tool_calls
                ]
            if msg.get("tool_calls") == []:
                msg.pop("tool_calls", None)
            messages.append(msg)
        elif content.role == "tool_result":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": _shorten_tool_call_id(content.tool_call_id)
                    if shorten_tool_call_id
                    else content.tool_call_id,
                    "content": orjson.dumps(content.tool_result).decode(),
                }
            )

    return messages


class ExtendedOpenAIBaseLLMEntity(Entity):
    """Extended OpenAI base entity."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(
        self, entry: ExtendedOpenAIConfigEntry, subentry: ConfigSubentry
    ) -> None:
        """Initialize the entity."""
        self.entry = entry
        self.subentry = subentry
        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer="OpenAI",
            model=subentry.data.get(CONF_CHAT_MODEL, DEFAULT_CHAT_MODEL),
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    @property
    def _client(self) -> AsyncClient:
        """Return the OpenAI client."""
        return self.entry.runtime_data

    async def _async_handle_chat_log(
        self,
        chat_log: conversation.ChatLog,
        function_tools: list[dict[str, Any]],
        exposed_entities: list[dict[str, Any]],
        llm_context: llm.LLMContext | None = None,
        structure_name: str | None = None,
        structure: vol.Schema | None = None,
    ) -> None:
        """Generate an answer for the chat log with streaming support."""
        options = self.subentry.data
        model = options.get(CONF_CHAT_MODEL, DEFAULT_CHAT_MODEL)
        max_function_calls = options.get(
            CONF_MAX_FUNCTION_CALLS_PER_CONVERSATION,
            DEFAULT_MAX_FUNCTION_CALLS_PER_CONVERSATION,
        )
        shorten_tool_call_id = options.get(
            CONF_SHORTEN_TOOL_CALL_ID,
            DEFAULT_SHORTEN_TOOL_CALL_ID,
        )

        model_config = get_model_config(model)

        messages = _convert_content_to_param(chat_log.content, shorten_tool_call_id)

        tools: list[ChatCompletionToolParam] = []

        if chat_log.llm_api:
            tools.extend(
                _format_llm_tool(tool, chat_log.llm_api.custom_serializer)
                for tool in chat_log.llm_api.tools
            )

        tools.extend(
            ChatCompletionToolParam(
                type="function",
                function=func_spec["spec"],
            )
            for func_spec in function_tools
        )

        api_kwargs: dict[str, Any] = {
            "model": model,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        max_tokens = options.get(CONF_MAX_TOKENS, DEFAULT_MAX_TOKENS)
        if model_config["supports_max_completion_tokens"]:
            api_kwargs["max_completion_tokens"] = max_tokens
        elif model_config["supports_max_tokens"]:
            api_kwargs["max_tokens"] = max_tokens

        if model_config["supports_top_p"]:
            api_kwargs["top_p"] = options.get(CONF_TOP_P, DEFAULT_TOP_P)

        if model_config["supports_temperature"]:
            api_kwargs["temperature"] = options.get(
                CONF_TEMPERATURE, DEFAULT_TEMPERATURE
            )

        if model_config.get("supports_reasoning_effort"):
            api_kwargs["reasoning_effort"] = options.get(
                CONF_REASONING_EFFORT, DEFAULT_REASONING_EFFORT
            )

        if model_config.get("supports_service_tier"):
            api_kwargs["service_tier"] = options.get(
                CONF_SERVICE_TIER, DEFAULT_SERVICE_TIER
            )

        if structure is not None:
            api_kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": slugify(structure_name),
                    "strict": True,
                    "schema": _format_structured_output(structure, chat_log.llm_api),
                },
            }

        tool_kwargs: dict[str, Any] = {}
        if tools:
            tool_kwargs["tools"] = tools
            tool_kwargs["tool_choice"] = "auto"

        # Track how many messages we have already converted to OpenAI format,
        # so we only append the newly produced ones each iteration instead of
        # re-serializing the whole chat log every turn (O(n^2) otherwise).
        messages = _convert_content_to_param(chat_log.content, shorten_tool_call_id)
        converted_len = len(messages)

        for n_requests in range(MAX_TOOL_ITERATIONS):
            if tools and 0 <= max_function_calls <= n_requests:
                # Tool budget exhausted: ask the model to stop calling tools.
                tool_kwargs["tool_choice"] = "none"

            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "Prompt for %s: %d messages, estimated ~%d chars",
                    model,
                    len(messages),
                    sum(len(json.dumps(m)) for m in messages),
                )

            stream = await self._async_call_with_retry(
                self._client.chat.completions.create,
                messages=messages,
                **api_kwargs,
                **tool_kwargs,
            )

            pending_tool_calls: list[llm.ToolInput] = []

            async for content in chat_log.async_add_delta_content_stream(
                self.entity_id, self._transform_stream(chat_log, stream)
            ):
                if (
                    isinstance(content, conversation.AssistantContent)
                    and content.tool_calls
                ):
                    pending_tool_calls.extend(content.tool_calls)

            if pending_tool_calls:
                _LOGGER.info("Response Tool Calls %s", pending_tool_calls)

            # If the tool budget is exhausted, do not execute any tool calls the
            # model emits despite tool_choice=none; instead stop the loop so we
            # don't waste further API requests.
            if tools and 0 <= max_function_calls <= n_requests and pending_tool_calls:
                _LOGGER.warning(
                    "Model emitted tool calls after tool budget exhausted; stopping"
                )
                break

            for tool_input in pending_tool_calls:
                llm_tool_result = await self._execute_tool(
                    tool_input,
                    chat_log,
                    function_tools,
                    llm_context,
                    exposed_entities,
                )

                if llm_tool_result is not None:
                    chat_log.async_add_assistant_content_without_tools(
                        llm_tool_result
                    )

            # Incrementally append only the newly added chat-log entries.
            new_messages = _convert_content_to_param(
                chat_log.content, shorten_tool_call_id
            )
            if len(new_messages) > converted_len:
                messages.extend(new_messages[converted_len:])
            else:
                # Chat log was truncated (e.g. context threshold); rebuild fully.
                messages = new_messages
            converted_len = len(messages)

            if not chat_log.unresponded_tool_results:
                break

    async def _execute_tool(
        self,
        tool_input: llm.ToolInput,
        chat_log: conversation.ChatLog,
        function_tools: list[dict[str, Any]],
        llm_context: llm.LLMContext | None,
        exposed_entities: list[dict[str, Any]],
    ) -> conversation.ToolResultContent | None:
        """Execute a tool call - either LLM API tool or custom function tool."""
        if chat_log.llm_api:
            for tool in chat_log.llm_api.tools:
                if tool.name == tool_input.tool_name:
                    try:
                        result = await chat_log.llm_api.async_call_tool(tool_input)
                        return conversation.ToolResultContent(
                            agent_id=self.entity_id,
                            tool_call_id=tool_input.id,
                            tool_name=tool_input.tool_name,
                            tool_result=result,
                        )
                    except HomeAssistantError as err:
                        _LOGGER.error("LLM tool execution error: %s", err)
                        return conversation.ToolResultContent(
                            agent_id=self.entity_id,
                            tool_call_id=tool_input.id,
                            tool_name=tool_input.tool_name,
                            tool_result={"error": str(err)},
                        )

        function_tool = next(
            (
                f
                for f in function_tools
                if f["spec"]["name"] == tool_input.tool_name
            ),
            None,
        )

        if function_tool is None:
            raise FunctionNotFound(tool_input.tool_name)

        tool_result_content = await self._execute_function_tool(
            function_tool,
            tool_input,
            llm_context,
            exposed_entities,
        )

        return tool_result_content

    async def _execute_function_tool(
        self,
        function_tool: dict[str, Any],
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext | None,
        exposed_entities: list[dict[str, Any]],
    ) -> conversation.ToolResultContent:
        """Execute a custom function."""
        arguments: dict[str, Any] = tool_input.tool_args
        function_config = function_tool["function"]
        function = get_function(function_config["type"])

        if self.should_run_in_background(arguments):
            function_config = self.get_delayed_function_config(
                function_config, arguments
            )
            function = get_function(function_config["type"])
            self.entry.async_create_task(
                self.hass,
                function.execute(
                    self.hass,
                    function_config,
                    arguments,
                    llm_context,
                    exposed_entities,
                ),
            )
            result = "已安排后台执行"
        else:
            result = await function.execute(
                self.hass, function_config, arguments, llm_context, exposed_entities
            )

        return conversation.ToolResultContent(
            agent_id=self.entity_id,
            tool_call_id=tool_input.id,
            tool_name=tool_input.tool_name,
            tool_result={"result": str(result)},
        )

    def should_run_in_background(self, arguments: dict[str, Any]) -> bool:
        """Check if function needs delay."""
        return isinstance(arguments, dict) and arguments.get("delay") is not None

    def get_delayed_function_config(
        self, function_config: dict[str, Any], arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute function with delay."""
        return {
            "type": "composite",
            "sequence": [
                {
                    "type": "script",
                    "sequence": [{"delay": arguments["delay"]}],
                },
                function_config,
            ],
        }

    async def _transform_stream(
        self,
        chat_log: conversation.ChatLog,
        result: AsyncStream[ChatCompletionChunk],
    ) -> AsyncGenerator[
        conversation.AssistantContentDeltaDict | conversation.ToolResultContentDeltaDict
    ]:
        """Transform OpenAI stream to Home Assistant format."""
        current_tool_calls: dict[int, dict[str, Any]] = {}
        first_chunk = True

        async for chunk in result:
            _LOGGER.debug("Received chunk: %s", chunk)

            if first_chunk:
                yield {"role": "assistant"}
                first_chunk = False

            if not chunk.choices:
                if chunk.usage:
                    chat_log.async_trace(
                        {
                            "stats": {
                                "input_tokens": chunk.usage.prompt_tokens,
                                "output_tokens": chunk.usage.completion_tokens,
                            }
                        }
                    )
                    if chunk.usage.total_tokens > self.subentry.data.get(
                        CONF_CONTEXT_THRESHOLD, DEFAULT_CONTEXT_THRESHOLD
                    ):
                        await self._truncate_message_history(chat_log)
                continue

            choice = chunk.choices[0]
            delta = choice.delta

            if delta.content:
                content_value = delta.content
                if not isinstance(content_value, str):
                    _LOGGER.warning(
                        "Received non-string content from API: %s (type: %s)",
                        content_value,
                        type(content_value),
                    )
                    content_value = str(content_value) if content_value else ""
                if content_value:
                    yield {"content": content_value}

            if delta.tool_calls:
                for tool_call_delta in delta.tool_calls:
                    idx = tool_call_delta.index
                    if idx not in current_tool_calls:
                        current_tool_calls[idx] = {
                            "id": tool_call_delta.id or "",
                            "name": "",
                            "arguments": "",
                        }

                    if tool_call_delta.function:
                        if tool_call_delta.function.name:
                            current_tool_calls[idx]["name"] = (
                                tool_call_delta.function.name
                            )
                        if tool_call_delta.function.arguments:
                            current_tool_calls[idx]["arguments"] += (
                                tool_call_delta.function.arguments
                            )

            if current_tool_calls and (choice.finish_reason in {"tool_calls", "stop"}):
                tool_calls_list = []
                for idx in sorted(current_tool_calls.keys()):
                    tool_call = current_tool_calls[idx]
                    try:
                        args = json.loads(tool_call["arguments"])
                    except json.JSONDecodeError as err:
                        raise ParseArgumentsFailed(tool_call["arguments"]) from err
                    tool_calls_list.append(
                        llm.ToolInput(
                            id=tool_call["id"],
                            tool_name=tool_call["name"],
                            tool_args=args,
                            external=True,
                        )
                    )
                if tool_calls_list:
                    yield {"tool_calls": tool_calls_list}
                current_tool_calls.clear()
            if choice.finish_reason == "length":
                raise TokenLengthExceededError(
                    self.subentry.data.get(CONF_MAX_TOKENS, DEFAULT_MAX_TOKENS)
                )

            if choice.finish_reason == "content_filter":
                raise ContentFilterError()

            if choice.finish_reason == "stop":
                break

    async def _truncate_message_history(self, chat_log: conversation.ChatLog) -> None:
        """Truncate message history based on strategy."""
        options = self.subentry.data
        strategy = options.get(
            CONF_CONTEXT_TRUNCATE_STRATEGY, DEFAULT_CONTEXT_TRUNCATE_STRATEGY
        )

        messages = chat_log.content
        _LOGGER.info("Context threshold exceeded, using strategy: %s", strategy)

        if strategy == "clear":
            # Keep the leading system block(s) + the most recent user message.
            # Find the end of the contiguous leading system block.
            system_end = 0
            for i, msg in enumerate(messages):
                if msg.role == "system":
                    system_end = i + 1
                else:
                    break

            last_user_message_index = None
            for i in reversed(range(len(messages))):
                if messages[i].role == "user":
                    last_user_message_index = i
                    break

            if last_user_message_index is not None and last_user_message_index > system_end:
                del messages[system_end:last_user_message_index]

        elif strategy == "rolling_window":
            # Keep system prompt + last N pairs of messages (user+assistant+tool_result)
            # Default window: keep the last 2 pairs (4 messages)
            window_size = options.get(
                CONF_CONTEXT_ROLLING_WINDOW_SIZE, DEFAULT_CONTEXT_ROLLING_WINDOW_SIZE
            )

            # Find indices of system block end and last user message.
            system_end = 0
            for i, msg in enumerate(messages):
                if msg.role == "system":
                    system_end = i + 1
                else:
                    break

            last_user_idx = None
            for i, msg in enumerate(messages):
                if msg.role == "user":
                    last_user_idx = i

            if last_user_idx is not None:
                # Keep: system block + messages from (last_user_idx - window_size) onward
                keep_from = max(system_end, last_user_idx - window_size)
                if keep_from > system_end:
                    del messages[system_end:keep_from]

        elif strategy == "selective":
            # Remove pure conversation messages, keep tool call results.
            # Tool results are more important for context continuity.
            # Find the index of the last assistant message (the one to keep).
            last_assistant_idx = None
            for i in reversed(range(len(messages))):
                if messages[i].role == "assistant":
                    last_assistant_idx = i
                    break

            to_delete = []
            for i, msg in enumerate(messages):
                # Always keep system and user messages
                if msg.role == "system":
                    continue
                if msg.role == "user":
                    continue
                # Keep tool calls and tool results
                if msg.role == "assistant" and msg.tool_calls:
                    continue
                if msg.role == "tool_result":
                    continue
                # Mark pure assistant text (no tool calls) for deletion,
                # but keep the most recent assistant message.
                if msg.role == "assistant" and not msg.tool_calls:
                    if i != last_assistant_idx:
                        to_delete.append(i)

            for i in reversed(to_delete):
                del messages[i]

    async def _async_call_with_retry(
        self,
        api_call: Callable[..., Any],
        *,
        max_retries: int = 3,
        initial_delay: float = 1.0,
        **kwargs: Any,
    ) -> Any:
        """Call OpenAI API with exponential backoff retry on transient errors.

        Retries on rate limit (429), server errors (5xx), connection errors,
        and timeouts. Does not retry on authentication errors or invalid requests.
        """
        from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

        last_exception: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                return await api_call(**kwargs)
            except RateLimitError as err:
                last_exception = err
                if attempt < max_retries:
                    delay = initial_delay * (2 ** attempt)
                    _LOGGER.warning(
                        "Rate limit hit, retrying in %.1fs (attempt %d/%d)",
                        delay,
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(delay)
            except APITimeoutError as err:
                last_exception = err
                if attempt < max_retries:
                    delay = initial_delay * (2 ** attempt)
                    _LOGGER.warning(
                        "Request timed out, retrying in %.1fs (attempt %d/%d)",
                        delay,
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(delay)
                else:
                    raise
            except APIConnectionError as err:
                last_exception = err
                if attempt < max_retries:
                    delay = initial_delay * (2 ** attempt)
                    _LOGGER.warning(
                        "Connection error, retrying in %.1fs (attempt %d/%d): %s",
                        delay,
                        attempt + 1,
                        max_retries,
                        err,
                    )
                    await asyncio.sleep(delay)
                else:
                    raise
            except APIStatusError as err:
                # Retry on server errors (5xx), not on client errors (4xx except 429)
                if err.status_code >= 500 and attempt < max_retries:
                    last_exception = err
                    delay = initial_delay * (2 ** attempt)
                    _LOGGER.warning(
                        "Server error %d, retrying in %.1fs (attempt %d/%d)",
                        err.status_code,
                        delay,
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(delay)
                else:
                    raise
            except asyncio.TimeoutError as err:
                last_exception = err
                if attempt < max_retries:
                    delay = initial_delay * (2 ** attempt)
                    _LOGGER.warning(
                        "Async timeout, retrying in %.1fs (attempt %d/%d)",
                        delay,
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(delay)
                else:
                    raise

        # All retries exhausted
        if last_exception:
            raise last_exception
