"""Base entity for AI Conversation."""

from __future__ import annotations

import asyncio
import base64
import copy
from collections.abc import AsyncGenerator
from dataclasses import fields
import hashlib
import json
import logging
from mimetypes import guess_file_type
from typing import TYPE_CHECKING, Any, Callable

from openai import AsyncClient, AsyncStream
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionChunk,
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)
import orjson

try:
    # HA 2026.9 起核心以 probatio 取代 voluptuous + voluptuous-openapi；
    # probatio.to_openapi 可直接序列化 probatio.Schema / vol.Schema（shim）。
    import probatio

    _to_openapi = probatio.to_openapi
    _UNSUPPORTED = probatio.UNSUPPORTED
except ImportError:  # HA < 2026.9
    from voluptuous_openapi import UNSUPPORTED as _UNSUPPORTED
    from voluptuous_openapi import convert as _to_openapi  # type: ignore[no-redef]

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
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

# HA 2026.10 起 ToolResultContent 的 tool_result 字段改为 result: llm.ToolResult，
# tool_result 降级为只读兼容属性，不能再作为构造参数传入。
_TOOL_RESULT_USES_LLM_TOOL_RESULT = "result" in {
    content_field.name for content_field in fields(conversation.ToolResultContent)
}


def _tool_result_content(
    agent_id: str,
    tool_input: llm.ToolInput,
    result: dict[str, Any] | llm.ToolResult,
    error: bool = False,
) -> conversation.ToolResultContent:
    """Build tool result content for both the legacy and the new HA API."""
    if not _TOOL_RESULT_USES_LLM_TOOL_RESULT:
        return conversation.ToolResultContent(
            agent_id=agent_id,
            tool_call_id=tool_input.id,
            tool_name=tool_input.tool_name,
            tool_result=result,
        )

    return conversation.ToolResultContent(
        agent_id=agent_id,
        tool_call_id=tool_input.id,
        tool_name=tool_input.tool_name,
        result=(
            result
            if isinstance(result, llm.ToolResult)
            else llm.ToolResult(data=result, error=error)
        ),
    )


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


def _strip_unsupported_keywords(schema: dict[str, Any]) -> dict[str, Any]:
    """Remove JSON schema keywords some LLM endpoints reject.

    Same keyword set as homeassistant.components.openai_conversation
    (oneOf/anyOf/allOf/enum/not), but stripped recursively instead of
    top-level only, since Chat Completions endpoints are stricter than
    the Responses API.
    """
    unsupported = {"oneOf", "anyOf", "allOf", "enum", "not"}
    if not isinstance(schema, dict):
        return schema
    result = {
        key: value for key, value in schema.items() if key not in unsupported
    }
    for key, value in result.items():
        if isinstance(value, dict):
            result[key] = _strip_unsupported_keywords(value)
        elif isinstance(value, list):
            result[key] = [
                _strip_unsupported_keywords(item) if isinstance(item, dict) else item
                for item in value
            ]
    return result


def _normalize_custom_serializer(
    custom_serializer: Callable[..., Any] | None,
) -> Callable[..., Any] | None:
    """Wrap an external custom serializer to translate foreign markers.

    Both voluptuous-openapi (HA < 2026.9) and probatio (HA >= 2026.9)
    recognise only their own UNSUPPORTED sentinel, while Home Assistant's
    llm.selector_serializer may return another marker. Without this adapter
    such markers would leak into the generated JSON schema and break
    serialization to the LLM API.
    """

    if custom_serializer is None:
        return None

    def wrapped(schema: Any) -> Any:
        result = custom_serializer(schema)
        if isinstance(result, dict):
            return result
        return _UNSUPPORTED

    return wrapped


def _schema_to_openapi(
    schema: Any, custom_serializer: Callable[..., Any] | None
) -> dict[str, Any]:
    """Serialize a voluptuous/probatio schema to an OpenAPI dict.

    probatio.to_openapi accepts ``openapi_version`` since 0.13; the legacy
    voluptuous-openapi ``convert`` does not. Both accept ``custom_serializer``
    returning either a dict or the UNSUPPORTED sentinel.
    """
    try:
        return _to_openapi(
            schema,
            custom_serializer=custom_serializer,
            openapi_version="3.1.0",
        )
    except TypeError:
        return _to_openapi(schema, custom_serializer=custom_serializer)


def _format_structured_output(
    schema: Any, llm_api: llm.APIInstance | None
) -> dict[str, Any]:
    """Format the schema to be compatible with OpenAI API."""
    result: dict[str, Any] = _schema_to_openapi(
        schema,
        _normalize_custom_serializer(
            llm_api.custom_serializer if llm_api else llm.selector_serializer
        ),
    )

    result = _adjust_schema(result)

    return result


def _format_llm_tool(
    tool: llm.Tool, custom_serializer: Any | None
) -> ChatCompletionToolParam:
    """Format an LLM tool to OpenAI function tool format."""
    parameters = _schema_to_openapi(
        tool.parameters, _normalize_custom_serializer(custom_serializer)
    )
    parameters = _strip_unsupported_keywords(parameters)
    return ChatCompletionToolParam(
        type="function",
        function={
            "name": tool.name,
            "description": tool.description or "",
            "parameters": parameters,
        },
    )


def _read_attachment_file(
    attachment: conversation.Attachment,
) -> dict[str, Any]:
    """Read an attachment from disk and convert to an OpenAI content part.

    Only images and PDFs are forwarded to the LLM (same as HA core).
    Runs in the executor; raises HomeAssistantError on failure.
    """
    path = attachment.path
    mime_type = attachment.mime_type or guess_file_type(path)[0]
    if not path.exists():
        raise HomeAssistantError(f"`{path}` does not exist")
    if not mime_type or not mime_type.startswith(("image/", "application/pdf")):
        raise HomeAssistantError(
            "Only images and PDF attachments are supported,"
            f" `{path}` ({mime_type})"
        )

    encoded = base64.b64encode(path.read_bytes()).decode("utf-8")
    if mime_type.startswith("image/"):
        return {
            "type": "image_url",
            "image_url": {
                "url": f"data:{mime_type};base64,{encoded}",
                "detail": "auto",
            },
        }
    return {
        "type": "file",
        "file": {
            "filename": path.name,
            "file_data": f"data:{mime_type};base64,{encoded}",
        },
    }


async def _async_apply_attachments(
    hass: HomeAssistant,
    messages: list[ChatCompletionMessageParam],
    chat_content: list[conversation.Content],
) -> None:
    """Attach files to the last user message in place.

    Mirrors HA core: only when the current input (the last chat-log
    content) is a user message carrying attachments are the files read
    in the executor and the corresponding OpenAI message rewritten as
    a multi-part content list. Attachments on earlier turns were
    already consumed then and must not be re-sent.
    """
    if not chat_content:
        return
    last_content = chat_content[-1]
    attachments = getattr(last_content, "attachments", None)
    if last_content.role != "user" or not attachments:
        return

    parts: list[dict[str, Any]] = await hass.async_add_executor_job(
        lambda: [
            _read_attachment_file(attachment) for attachment in attachments
        ]
    )

    # Every converted role emits exactly one message, so the last
    # counted content maps to messages[-1].
    message = messages[-1]
    if message.get("role") != "user":
        return
    text = message.get("content")
    if not isinstance(text, str):
        return
    message["content"] = [
        {"type": "text", "text": text},
        *parts,
    ]


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
                    "content": orjson.dumps(
                        content.result.data
                        if _TOOL_RESULT_USES_LLM_TOOL_RESULT
                        else content.tool_result
                    ).decode(),
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
        structure: Any = None,
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
        # 用户消息可携带图片/PDF 附件（ai_task 附件），在 executor 中读取
        # 文件并改写为多段 content，避免阻塞事件循环。
        await _async_apply_attachments(self.hass, messages, chat_log.content)
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
                        return _tool_result_content(self.entity_id, tool_input, result)
                    except HomeAssistantError as err:
                        _LOGGER.error("LLM tool execution error: %s", err)
                        # 与 HA 核心默认代理一致：错误时返回
                        # {"error": 类型, "error_text": 详情}，模型可解释。
                        return _tool_result_content(
                            self.entity_id,
                            tool_input,
                            {
                                "error": type(err).__name__,
                                "error_text": str(err),
                            },
                            error=True,
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

        return _tool_result_content(self.entity_id, tool_input, {"result": str(result)})

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

            # DeepSeek/Qwen/Kimi 等 OpenAI 兼容端点在流式 delta 中输出
            # reasoning_content（或 reasoning），对应 HA 的 thinking_content，
            # 会进入 trace 与前端思考流显示。标准 openai 类型未声明该字段，
            # 故用 getattr 兜底。
            thinking = (
                getattr(delta, "reasoning_content", None)
                or getattr(delta, "reasoning", None)
            )
            if isinstance(thinking, str) and thinking:
                yield {"thinking_content": thinking}

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
