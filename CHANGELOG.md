# Changelog

## [3.3.0] - 2026-10-07

### Added

- **图片/PDF 附件支持（AI Task / Conversation）**: 聊天日志中用户消息携带的 `attachments` 现在会转换为 Chat Completions 多段 content（`image_url` / `file`）发送给模型，文件在 executor 中读取避免阻塞事件循环；`ai_task` 附件（如相机快照）由此可被视觉模型理解。参照 HA 核心 `conversation.Attachment` / `ai_task` 附件链路。
- **`ai_task.generate_image` 支持**: AI Task 实体新增 `GENERATE_IMAGE` 能力，通过 `client.images.generate` 实现（支持 `dall-e-3`、`gpt-image-1` 及兼容 OpenAI Images API 的端点）。AI Task 子项配置 `image_model` 即启用；`dall-e` 显式请求 `b64_json`，`gpt-image-1` 默认即返回 b64；端点仅返回 URL 时自动下载图片内容，生成结果记录到 chat log 便于 trace 回看。
- **推理内容流式输出**: 兼容端点（DeepSeek/Qwen/Kimi 等）在流式 delta 中输出的 `reasoning_content`/`reasoning` 映射为 `thinking_content` delta，进入聊天日志 trace 与前端思考流展示。
- **`image_model` 配置项**: AI Task 子项表单新增可选图片模型字段（全部 13 种语言翻译已同步）。

### Changed

- **schema 序列化迁移 probatio**: HA 2026.9 起核心以 `probatio`（内置依赖）取代 `voluptuous` + `voluptuous-openapi`，`llm.Tool.parameters` 与 `GenDataTask.structure` 均为 `probatio.Schema`。`_format_structured_output` / `_format_llm_tool` 优先使用 `probatio.to_openapi`（OpenAPI 3.1.0），HA < 2026.9 自动回退 `voluptuous_openapi.convert`，双版本兼容；外部 `custom_serializer` 的 UNSUPPORTED 哨兵转换逻辑相应适配。
- **中文翻译文件名标准化**: `translations/zh.json` 重命名为 `translations/zh-Hans.json`，使用 HA 标准语言码。
- **工具参数 schema 递归清洗**: 新增 `_strip_unsupported_keywords`，与核心 OpenAI 集成一致地移除 `oneOf`/`anyOf`/`allOf`/`enum`/`not` 等部分 OpenAI 兼容端点不接受的 JSON Schema 关键字。
- **LLM 工具错误结果格式对齐核心**: 工具执行抛 `HomeAssistantError` 时返回 `{"error": <异常类型>, "error_text": <详情>}`（此前为 `{"error": <详情>}`），与核心 `llm.ToolResult` 错误约定一致。
- **CONTROL 特性条件化**: `ConversationEntityFeature.CONTROL` 仅在子项配置了 `llm_hass_api`（Assist API）时声明，与核心 OpenAI 集成一致；自定义 function tools 不受影响。
- **`structure` 参数类型放宽**: `_async_handle_chat_log` 的 `structure` 形参改为 `Any`，同时接受 `vol.Schema`（< 2026.9）与 `probatio.Schema`（≥ 2026.9）。

### Fixed

- **openai 上限约束冲突**: `requirements` 由 `openai>=2.21.0,<3.0.0` 改为 `openai>=2.21.0`。此前一旦 Home Assistant 核心锁定 openai 3.x（例如 `openai==3.10.0`），pip 解析会报 `Because you require openai>=2.21.0,<3.0.0 and openai==3.10.0 ... your requirements are unsatisfiable`。现在只限制最低版本，由 HA 核心决定实际安装版本。
- **HA 2026.10 工具调用崩溃**: HA 2026.10 起 `ToolResultContent.tool_result` 改为 `result: llm.ToolResult`，旧字段降级为只读兼容属性，继续传 `tool_result=` 会抛 `TypeError: ToolResultContent.__init__() got an unexpected keyword argument 'tool_result'`，导致 Assist 意图识别失败。新增 `_tool_result_content()` 依据 `ToolResultContent` 的字段名选择构造方式：2026.10 及以上传 `result=llm.ToolResult(...)`（工具报错时带 `error=True`），更早版本仍传 `tool_result=`；同时读取工具结果时改用 `content.result.data`，避免触发 `tool_result` 属性的弃用上报。
- **历史附件重复发送**: `_async_apply_attachments` 此前取「最后一条带附件的 user content」，旧附件会随每次新提问重新发送给模型（浪费 token 且干扰推理）。现与核心一致：仅当聊天日志最后一条是携带附件的 user 消息时才应用。
- **图片 URL 下载分支健壮性**: 补 `raise_for_status()` 状态校验，`except Exception` 收窄为 `httpx.HTTPError`，`mime_type` 从响应 `Content-Type` 头取值（不再硬编码 `image/png`）。
- **清理重复序列化**: 移除 `_async_handle_chat_log` 中一处冗余的 `_convert_content_to_param` 调用（其结果原本就被后续调用覆盖）。
- **JSON 结尾换行**: `strings.json` 与全部 `translations/*.json` 补上缺失的文件末尾换行。

## [3.2.0] - 2026-09-24

### Changed

- **HACS repository layout**: the repository now follows the standard HACS integration structure. The integration lives in `custom_components/hass_ai_conversation/`, and the repository root provides `hacs.json`, `README.md`, `CHANGELOG.md`, `LICENSE` and `.gitignore`. The installed path inside Home Assistant (`config/custom_components/hass_ai_conversation/`) is unchanged.
- **Service descriptions moved to translations**: `services.yaml` now only contains field/selector definitions; the Chinese `name`/`description` of `rebuild_device_map` is provided by `strings.json` and `translations/*.json`, per the Home Assistant convention.
- **`strings.json` consistency**: all `[%key:common::...%]` references were replaced with their literal English values, because several referenced keys (`base_url`, `api_version`, `organization`, `skip_authentication`, `api_provider`) do not exist in Home Assistant core's `common` translations. `strings.json` is now identical to `translations/en.json`.

### Added

- `hacs.json`: HACS repository metadata (`name`, `render_readme`, minimum `homeassistant` version).
- `brand/icon.png` (256×256) and `brand/icon@2x.png` (512×512): local brand assets, supported by Home Assistant 2026.3+.
- `icons.json`: icons for the `query_image`, `change_config` and `rebuild_device_map` service actions.
- `.github/workflows/validate.yml`: runs [hassfest](https://github.com/home-assistant/actions#hassfest) and [HACS Action](https://github.com/hacs/action) on push, pull request and a daily schedule.
- `.github/workflows/release.yml`: verifies that the tag matches `manifest.json` and creates a GitHub release (HACS uses releases for versioning).
- `LICENSE` (MIT).

### Fixed

- **Translation placeholders**: `exceptions.function_not_found` and `exceptions.native_not_found` used single-quoted placeholders (`'{function}'`, `'{name}'`), which hassfest rejects. They now use backticks (`` `{function}` ``).
- **Leftover translation reference**: the `ai_task_data` step's `name` field kept the raw `[%key:common::config_flow::data::name%]` reference in all translations except `zh.json`. Since Home Assistant does not resolve references for custom integrations, the field label was rendered literally; it is now the localized literal value.

## [3.1.0] - 2026-08-07

### Changed

- **Rename integration**: domain changed from `extended_openai_conversation` to `hass_ai_conversation`; display name to "AI Conversation".
- **Repository moved**: documentation / issue tracker now point to https://github.com/xyzmos/hass_ai_conversation.
- **`iot_class`**: corrected from `cloud_polling` to `cloud_push` (this integration calls the LLM API on demand, it does not poll).
- **Template global variable**: renamed from `extended_openai` to `ai_conversation` (e.g. `ai_conversation.exposed_entities()`).
- **Default working directory**: renamed from `extended_openai_conversation/` to `hass_ai_conversation/`.
- **Event names**: `automation_registered_via_hass_ai_conversation`, `hass_ai_conversation.conversation.finished`.
- **`hass.data` keys**: `hass_ai_conversation_exposed_entities`, `hass_ai_conversation_device_map`.
- **i18n structure**: `strings.json` default values are now English (per HA convention); full Chinese translations moved to `translations/zh.json`.

### Fixed

- **openai dependency conflict**: relaxed `requirements` from `openai~=2.21.0` to `openai>=2.21.0,<3.0.0` so the pip resolver no longer fails against HA core's pinned `openai==2.45.0` (carried over from 3.0.1).
- **Authentication failure handling**: `AuthenticationError` during setup now raises `ConfigEntryAuthFailed` instead of returning `False`, so Home Assistant starts the reauth flow automatically; removed the redundant error log.
- **Config flow crash on model list**: `get_authenticated_client` called `client.models.list` through an executor and iterated the result synchronously, which raises `TypeError: 'AsyncPaginator' object is not iterable` on openai SDK 2.x (async paginator is not synchronously iterable). It now awaits `client.models.list` directly on the event loop and reads the first page via `response.data`, matching the SDK's documented async pagination pattern.
- **Config flow crash on functions field**: the `functions` config field used a `TemplateSelector`, which made Home Assistant compile the YAML text (produced by `yaml.dump`) as a Jinja2 template; YAML escape sequences (`\n`, line-continuation `\`, `\uXXXX`) then raised `TemplateSyntaxError: unexpected char '\\'`. The field now uses a multiline `TextSelector` (the value is YAML, not a Jinja2 template) and `yaml.dump` is called with `allow_unicode=True` so the default renders as readable Chinese instead of `\u` escapes.

### Added

- **Exception i18n**: the 10 custom exception classes now use `translation_domain` / `translation_key` / `translation_placeholders`; an `exceptions` section was added to `strings.json` and all translation files.
- **Additional error i18n**: user-facing errors in `services.py` (admin-only), `functions/native.py` (blocked service), `functions/sqlite.py` (SELECT-only) now also use the translation system (3 additional keys).
- **English logs**: log messages switched to English per HA convention; Chinese retained only for LLM-facing content (prompts, tool descriptions, device-alias data).
- **translations/zh.json**: new full Chinese translation file.

### Breaking

- **Domain changed**: Home Assistant treats this as a new integration. Existing config entries under `extended_openai_conversation` are NOT migrated — users must remove the old integration entry and re-add "AI Conversation".
- **Working directory renamed**: files under `<config>/extended_openai_conversation/` are no longer visible to the bash/file tools; rename the directory to `hass_ai_conversation/` to keep them.
- **Template variable renamed**: custom prompts using `extended_openai.*()` must be updated to `ai_conversation.*()`.

### Credits

This project is a fork of [extended_openai_conversation](https://github.com/jekalmin/extended_openai_conversation) by [@jekalmin](https://github.com/jekalmin). Many thanks to the original author and contributors.
