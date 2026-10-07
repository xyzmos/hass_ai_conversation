"""AI Task integration for AI Conversation."""

from __future__ import annotations

import base64
from json import JSONDecodeError
import logging
from typing import TYPE_CHECKING, Any

import httpx
from openai import OpenAIError

from homeassistant.components import ai_task, conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.httpx_client import get_async_client
from homeassistant.util.json import json_loads

from .const import CONF_IMAGE_MODEL, DEFAULT_IMAGE_MODEL, DOMAIN
from .entity import ExtendedOpenAIBaseLLMEntity

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigSubentry

    from . import ExtendedOpenAIConfigEntry

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up AI Task entities."""
    for subentry in config_entry.subentries.values():
        if subentry.subentry_type != "ai_task_data":
            continue

        async_add_entities(
            [ExtendedOpenAITaskEntity(config_entry, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


class ExtendedOpenAITaskEntity(
    ai_task.AITaskEntity,
    ExtendedOpenAIBaseLLMEntity,
):
    """Extended OpenAI AI Task entity."""

    def __init__(
        self, entry: ExtendedOpenAIConfigEntry, subentry: ConfigSubentry
    ) -> None:
        """Initialize the entity."""
        super().__init__(entry, subentry)
        self._attr_supported_features = (
            ai_task.AITaskEntityFeature.GENERATE_DATA
            | ai_task.AITaskEntityFeature.SUPPORT_ATTACHMENTS
        )
        # 配置了图片模型时声明 GENERATE_IMAGE；底层走 images.generate，
        # 兼容 OpenAI dall-e/gpt-image 系列与兼容 OpenAI Images API 的服务。
        if self.subentry.data.get(CONF_IMAGE_MODEL):
            self._attr_supported_features |= ai_task.AITaskEntityFeature.GENERATE_IMAGE

    async def _async_generate_data(
        self,
        task: ai_task.GenDataTask,
        chat_log: conversation.ChatLog,
    ) -> ai_task.GenDataTaskResult:
        """Handle a generate data task."""
        await self._async_handle_chat_log(
            chat_log,
            function_tools=[],
            exposed_entities=[],
            llm_context=None,
            structure_name=task.name,
            structure=task.structure,
        )

        if not isinstance(chat_log.content[-1], conversation.AssistantContent):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="invalid_chat_log_content",
            )

        text = chat_log.content[-1].content or ""

        if not task.structure:
            return ai_task.GenDataTaskResult(
                conversation_id=chat_log.conversation_id,
                data=text,
            )

        try:
            data = json_loads(text)
        except JSONDecodeError as err:
            _LOGGER.error(
                "Failed to parse JSON response: %s. Response: %s",
                err,
                text,
            )
            raise HomeAssistantError("Structured response parse error") from err

        return ai_task.GenDataTaskResult(
            conversation_id=chat_log.conversation_id,
            data=data,
        )

    async def _async_generate_image(
        self,
        task: ai_task.GenImageTask,
        chat_log: conversation.ChatLog,
    ) -> ai_task.GenImageTaskResult:
        """Handle a generate image task via the Images API.

        Chat Completions 本身不出图，核心 OpenAI 集成走 Responses API
        的 image_generation 工具；Chat-Completions 兼容链路改为调用
        ``client.images.generate``，支持 dall-e-3/gpt-image-1 及兼容端点。
        注意：``task.attachments`` 在此处被忽略——Images API 的编辑入口是
        ``images.edit``，与 ``images.generate`` 是不同接口，本实现不做参考图编辑。
        """
        image_model = self.subentry.data.get(CONF_IMAGE_MODEL, DEFAULT_IMAGE_MODEL)
        # gpt-image-1 不接受 response_format（始终返回 b64_json），
        # dall-e 系列默认返回 url，仅对 dall-e 显式请求 b64。
        generate_kwargs: dict[str, Any] = {
            "model": image_model,
            "prompt": task.instructions,
            "n": 1,
        }
        if image_model.startswith("dall-e"):
            generate_kwargs["response_format"] = "b64_json"
        try:
            response = await self._client.images.generate(**generate_kwargs)
        except OpenAIError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="image_generation_error",
            ) from err

        image = response.data[0]
        image_data: bytes
        mime_type = "image/png"
        if image.b64_json:
            image_data = base64.b64decode(image.b64_json)
        elif image.url:
            # 端点仅返回图片 URL 时下载内容（gpt-image-1 默认 b64，
            # 该分支主要覆盖 url-only 的兼容服务）。
            try:
                http = get_async_client(self.hass)
                image_response = await http.get(image.url)
                image_response.raise_for_status()
                image_data = image_response.content
                content_type = image_response.headers.get("content-type", "")
                if content_type.startswith("image/"):
                    mime_type = content_type.split(";")[0].strip()
            except httpx.HTTPError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="image_generation_error",
                ) from err
        else:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="image_generation_error",
            )

        # 让聊天日志记录本次生成结果，便于 trace/调试回看。
        chat_log.async_add_assistant_content_without_tools(
            conversation.AssistantContent(
                agent_id=self.entity_id,
                content=f"[image generated by {image_model}]",
                native=image,
            )
        )

        revised_prompt = getattr(image, "revised_prompt", None)
        return ai_task.GenImageTaskResult(
            image_data=image_data,
            conversation_id=chat_log.conversation_id,
            mime_type=mime_type,
            model=image_model,
            revised_prompt=revised_prompt,
        )
