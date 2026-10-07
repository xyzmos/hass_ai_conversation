# Music Assistant 接入可行性分析

> 分析对象：`hass_ai_conversation` v3.3.0（HA ≥ 2026.8.0b0）
> 分析日期：2026-10-07
> 结论来源：全部基于一手资料（HA core `dev` / `2026.8.0` / `2026.9.0` / `2026.9.4` 源码、MA 官方文档），未使用二手转述。

---

## 0. 结论速览

**可行性：高。但核心结论和直觉相反 —— 大部分能力今天已经可用，不需要改一行代码。**

三条已验证的通路：

| # | 通路 | 需要改代码吗 | 现在能用吗 |
|---|---|---|---|
| 1 | **HA 核心已把音乐工具挂进 Assist LLM API**，本插件默认就选 Assist API | 否 | ✅ 是 |
| 2 | **用现有 `script` function tool + YAML** 调 `music_assistant.play_media` | 否 | ✅ 是 |
| 3 | **反向：MA 把每个 `ai_task` 实体当作 AI 引擎**，本插件已提供 ai_task 实体 | 否 | ✅ 是 |

需要写代码才能补齐的能力（库浏览、精准点播、队列管理、语音播报、确定性选音箱），**推荐通过新增 `llm.py` LLM 工具平台实现** —— 这是 HA 2026.8 起就存在的官方扩展点，目前已有 15 个核心组件在使用。

> ⚠️ **方案 A 的头号风险（务必先读 §5.3）**：MA 的服务字段是**逐版本新增**的 —— `search`/`get_library` 的 `username` 需 HA ≥2026.9、`play_announcement` 的 `message`/`tts_entity_id` 需 ≥2026.10、`play_media` 的 `start_item` 需 ≥2026.11。本插件 min HA = **2026.8.0**，**硬编码这些字段会在旧版上直接触发 schema 校验失败**，必须运行时探测后裁剪工具 schema。
>
> ⚠️ **第二号风险（见 §5.2）**：`llm.Tool` 本身在 **HA 2026.10** 发生契约变更 —— `ToolResult` / `ToolAnnotations` / `Tool.title` / `async_get_match_preferences` 在 **2026.8 与 2026.9.4 上全部不存在**。方案 A 需要 3 处版本 shim。相比之下**方案 B（`functions/` 新类型）完全不碰 `llm.Tool`，天然免疫这两类风险**，因此在跨版本稳定性上反而更优（详见 §4 的取舍分析）。

**推荐落地组合：先用通路 1+2 零成本验证 → 再上方案 A（`llm.py`）补齐 → 方案 B 作为高级用户逃生舱。**

**明确不选的路**：MA 自 2.9 起确实提供了官方 MCP Server（`/mcp/v1`）与 HTTP API（`POST :8095/api`），但那两条是为**外部 agent**设计的；对本插件这种 HA 进程内集成，它们会引入额外依赖、凭据管理与网络可达性问题，且重复实现了 HA 已封装的服务层。详见 [§4 方案 E](#方案-e绕过-ha直连-mcp-mcpv1-或-http-api--本次不选)。

**必须记住的方向性事实**：MA 是 AI 的**消费方**，不是提供方 —— 它不暴露 `ai_task` 实体，也不注册 conversation agent。

---

## 1. 本插件现状与可扩展点

### 1.1 调用链

```
ConfigEntry (API key / base_url)
  ├─ subentry "conversation"  ──> ExtendedOpenAIAgentEntity   (conversation.py)
  └─ subentry "ai_task_data"  ──> ExtendedOpenAITaskEntity    (ai_task.py)
                                        │
                                        └─> ExtendedOpenAIBaseLLMEntity._async_handle_chat_log()  (entity.py:411)
                                              │
                                              ├─ chat_log.async_provide_llm_data(llm_context, CONF_LLM_HASS_API, prompt)
                                              │     └─> llm.async_get_api(hass, "assist", llm_context)
                                              │           └─> APIInstance.tools  ← 核心各集成贡献的工具
                                              ├─ function_tools (用户 YAML 自定义，functions/*)
                                              └─ tools = llm_api.tools + function_tools   (entity.py:436-448)
```

`conversation.py:120-125` 把 `CONF_LLM_HASS_API` 交给 `chat_log.async_provide_llm_data()`；`config_flow.py:399` 的默认值是 `[llm.LLM_API_ASSIST]`，也就是**默认就启用了 Assist API**。这意味着核心贡献的工具（含音乐工具）默认已经在工具列表里。

### 1.2 三个可扩展点

| 扩展点 | 位置 | 特点 |
|---|---|---|
| **A. `llm.py` LLM 工具平台** | 新建 `custom_components/hass_ai_conversation/llm.py` | HA 2026.8+ 官方机制，工具自动并入 Assist API，用户零配置 |
| **B. `functions/` 新函数类型** | `functions/__init__.py:32-44` 的 `FUNCTIONS` 注册表 | YAML 驱动，用户显式启用，可自定义工具名/描述 |
| **C. 提示词 + API 选择** | `const.py:25` `DEFAULT_PROMPT`、`CONF_LLM_HASS_API` | 纯配置，零代码 |

### 1.3 工具序列化链路（决定新工具 schema 怎么写）

新增工具最终要经过 `entity.py` 的三道处理：

| 处理 | 位置 | 约束 |
|---|---|---|
| `_schema_to_openapi` | `entity.py:205` | ≥2026.9 走 `probatio.to_openapi`，<2026.9 走 `voluptuous_openapi.convert` |
| `_strip_unsupported_keywords` | `entity.py:156` | **递归删除 `oneOf`/`anyOf`/`allOf`/`enum`/`not`** |
| `_adjust_schema` | `entity.py:124` | 强制 `strict=True`、`additionalProperties=False`、**所有属性变 required 且非必填项类型加 `null`** |

**结论：新工具的 schema 要用最朴素的类型（string / integer / boolean / array of string），不要依赖 `enum`，每个 property 必须有 `type`。**

---

## 2. Music Assistant 侧能力盘点

### 2.1 集成形态（重要认知更新）

| 项目 | 事实 |
|---|---|
| 集成归属 | **HA 核心集成**（`homeassistant/components/music_assistant`）。PR #128919 于 2024-10-30 合入，**随 HA 2024.12.0 首次发布**（实测 2024.11.0 的 `manifest.json` 仍 404）。**不是 HACS 自定义集成** |
| 旧仓库 | `music-assistant/hass-music-assistant` 已于 2025-01-02 停更并被官方标注 deprecated（README：*"The custom integration is deprecated and no longer maintained!"*）。⚠️ 其 domain 是 **`mass`**（服务名为 `mass.*`），与现役 `music_assistant.*` **无别名、无重定向**，是两套完全不同的东西 —— 查资料时极易混淆 |
| 依赖 | `music-assistant-client==1.6.0`，`after_dependencies: [media_source, tts]` |
| 最低 MA Server | **2.4**（HA 官方文档要求） |
| MA Server 当前版本 | 2.10.5 stable；`dev` 已到 2.11.0b4。HTTP 服务端口 **8095** |
| 运行方式 | HA App（推荐）或 Docker `ghcr.io/music-assistant/server` |

**MA 侧与本次接入相关的插件时间线**（按 tag 逐版本探测 `providers/<name>/manifest.json` 核实）：

| 版本 | 引入内容 | 对本插件的意义 |
|---|---|---|
| **2.9.0** | `fastmcp_server`（MCP Server，PR #3858）；HA Plugin 增加 `AI_QUERY`/`TTS`（PR #3607）；Smart Playlist AI 描述 | MCP 通道起点；**反向 AI engine 通路起点** |
| **2.10.0** | `ai_radio`（AI Radio / AI DJ，PR #3407）；`openai_compatible`（PR #5261）；`openai_tts`（PR #5262） | MA 自带 AI DJ；MA 也可绕开 HA 直连 OpenAI 兼容服务 |
| 2.8.0 及更早 | 以上均不存在（逐个 404） | — |

### 2.2 HA 服务清单（权威来源 = `services.py`）

> **📌 更正（初版曾误判为"上游 schema 漂移"，已纠正）**：`services.yaml` 里 `search` 的 `limit`/`library_only` 在 2026.9 线从顶层移入 `search_options:`，`get_library` 的 `limit`/`offset` 移入 `pagination:`。**这不是 schema 漂移，而是 HA 的"可折叠字段分组"（collapsible sections）机制** —— 官方开发文档明确：
>
> > *"Fields can be grouped in collapsible sections… Note that the collapsible section **only affect presentation to the user, service action data will not be nested**."*
> > *"The service action data for the service in the example is `{"speed_pct": 50}`, **not** `{"additional_fields": {"speed_pct": 50}}`."*
>
> 所以 `search_options` / `pagination` **只影响前端渲染，不影响调用数据**。结论不变：**编程调用一律传扁平参数**，但原因是分组机制，而非上游 bug。
>
> 不过这次分组改动顺带暴露了一个**必须注意的版本门控**问题 —— 服务字段是**逐版本新增**的，跨 2026.8～2026.11 写代码必须按版本降级：

| 字段 / 服务 | 2026.8.0 | 2026.9.4 | dev (2026.11) | 说明 |
|---|---|---|---|---|
| `search` / `get_library` / `get_queue` | ✅ | ✅ | ✅ | 2025.1.0 起就有 |
| `play_media.username` | ✅ | ✅ | ✅ | 2026.7.0 起 |
| `search.username` / `get_library.username` | ❌ | ✅ | ✅ | **2026.9.0 才加**；2026.8 传了会校验失败 |
| `play_media.start_item` | ❌ | ❌ | ✅ | **仅 2026.11+** |
| `play_announcement.message` + `tts_entity_id` | ❌ | ❌ | ✅ | **仅 2026.10+**；2026.8/2026.9 的 `play_announcement` **只有 `url` 必填** |
| `play_announcement.pre_announce_url` | ✅ | ✅ | ✅ | 2026.2.0 起 |

> 逐版本 `services.py` 关键字计数实测：`ATTR_START_ITEM` = 0 / 0 / 2，`ATTR_MESSAGE`+`ATTR_TTS_ENTITY_ID` = 0 / 0 / 4+2，`ATTR_USERNAME` = 2 / 6 / 6。

| 服务 | 作用域 | 关键字段（⚠️ 见上方版本门控） | 返回 |
|---|---|---|---|
| `music_assistant.search` | 全局（用 `config_entry_id`） | `config_entry_id`(必填)、`name`(必填)、`media_type`(list)、`artist`、`album`、`limit`(默认5)、`library_only`(默认false)、`username` | `SupportsResponse.ONLY`，**扁平 dict**：`{artists, albums, tracks, playlists, radio, audiobooks, podcasts}` |
| `music_assistant.get_library` | 全局 | `config_entry_id`(必填)、`media_type`(必填)、`favorite`、`search`、`limit`(默认25)、`offset`(默认0)、`order_by`(默认`"name"`)、`album_type`、`album_artists_only`、`username` | `SupportsResponse.ONLY`，**扁平 dict**：`{items: [...], limit, offset, order_by, media_type}` |
| `music_assistant.play_media` | **实体级**（media_player + integration + speaker + PLAY_MEDIA） | `media_id`(必填，**列表**，URI 或名称)、`media_type`、`artist`、`album`、`enqueue`(play/replace/next/replace_next/add)、`radio_mode`、`start_item`⚠️、`username` | 无 |
| `music_assistant.play_announcement` | **实体级**（+ MEDIA_ANNOUNCE） | ✅`url`；⚠️`message`+`tts_entity_id`（须成对，且与 url 互斥）、`use_pre_announce`、`pre_announce_url`、`announce_volume`(1-100) | 无 |
| `music_assistant.transfer_queue` | **实体级** | `source_player`、`auto_play` | 无 |
| `music_assistant.get_queue` | **实体级**（`schema=None`） | 无字段，仅 target | `SupportsResponse.ONLY`，**按 `entity_id` 分组**：`{"media_player.x": {queue_id, active, name, items, shuffle_enabled, repeat_mode, current_index, elapsed_time, current_item, next_item}}` |

**响应结构三个陷阱**（都已核实源码）：

1. **实体级服务的响应会被 HA 以 `entity_id` 为 key 包装** —— `helpers/service.py:811` `response_data[entity.entity_id] = result`。所以 `get_queue` 要取 `resp["media_player.kitchen"]["current_item"]`，而 `search`/`get_library` 是普通服务，直接取 `resp["tracks"]`。
2. **`items` 语义不一致** —— `get_queue` 的 `items` 是**整数条数**（`QUEUE_DETAILS_SCHEMA` 里是 `int`），`get_library` 的 `items` 是**媒体项数组**（`LIBRARY_RESULTS_SCHEMA` 里是 `EnsureList`）。同名不同义，极易写错。
3. **`ItemMapping` 是"残缺"媒体项** —— `media_item_dict_from_mass_item` 对 `ItemMapping` 提前返回，**只含 `media_type`/`uri`/`name`/`version`/`image` 五个键**，没有 `favorite`/`artists`/`album`。代码里不能假设 `item["artists"]` 一定存在。

**`media-source://` 相关（重要）**：

- ❌ **`media-source://music_assistant/...` 不存在** —— MA 组件没有 `media_source.py`，也不注册 media source 平台。
- ⚠️ 其它集成的 `media-source://`（如 `media-source://tts/...`）在 `async_play_media` 里会先被解析成 URL 再播放，但 **MA 官方 FAQ 明确警告**：*"URIs which begin with media-source:// are HA URIs and should not be used when targetting MA player entities. Doing so will result in inconsistent behaviour."* ⇒ 给 MA 播放器传 URI 时**避免** `media-source://`。

单个媒体项（`media_item_dict_from_mass_item`）的结构：

```json
{
  "media_type": "track", "uri": "spotify://track/xxxx", "name": "…",
  "version": "…", "image": "http://…", "favorite": true, "explicit": false,
  "artists": [ {…} ], "album": {…}
}
```

### 2.3 `media_id` 的解析规则（`_async_handle_play_media`）

```
media_id 依次尝试：
  1. media_source://…            → media_source.async_resolve_media() 解析为 URL
  2. 含 "://" 的 URI             → 直接用（spotify:// / library:// / tidal:// / …）
  3. 纯数字 + media_type         → 转成 library://<media_type>/<id> 后校验
  4. 本地存在的文件路径          → 直接用
  5. 以上都不是                  → music.get_item_by_name(name, artist, album, media_type) 模糊匹配
```

> 第 1 步在 `async_play_media` 入口，不在 `_async_handle_play_media` 内部；且见 §2.2 末尾警告 —— **MA 没有自己的 media source**，`media-source://music_assistant/...` 不成立，官方也建议不要给 MA 播放器传 `media-source://` URI。

**支持的 `media_content_id` 形态**（逐条对应代码路径）：

| 形态 | 示例 | 可用 |
|---|---|---|
| 纯名称 | `Queen`、`Innuendo`、播放列表/电台名 | ✅（走第 5 步兜底） |
| library URI | `library://track/123`、`library://album/20`、`library://playlist/13` | ✅（`search`/`get_library` 返回的 `uri` 可直接回填） |
| provider URI | `spotify://track/xxxx`、`tidal://...` | ✅ |
| provider 网页 URL | `https://open.spotify.com/track/...` | ✅ |
| 文件夹 / 音效 | `filesystem_smb--xxxx://folder/ABBA`、`ambient_sounds://sound_effect/ocean_waves` | ✅ |
| 纯数字 ID + `media_type` | `123` + `track` | ✅ |
| 本地文件路径 | `/media/music/a.mp3` | ✅ |
| `media-source://music_assistant/...` | — | ❌ 不存在 |
| `media-source://tts/...` | — | ⚠️ 代码能解析，但 MA 官方警告会行为不一致 |

> ⚠️ **`library://track/123` 可用于 `play_media`，但不能用于 `browse_media`** —— `media_browser.py` 只按子串匹配 `artist`/`album`/`playlist`。
>
> ⚠️ `media_content_type` 会被 `MediaType(media_type)` 强转，未命中时静默变成 `UNKNOWN`（不报错）；官方示例用 `music` 也能靠名称搜索兜底。

第 5 条意味着：**直接传歌名也能播**（MA 服务端自己搜），这是"零改动通路"能跑通的底气。

### 2.4 MA 的 LLM / MCP 能力

#### (a) FastMCP Server 插件（MA ≥ 2.9，`stage: experimental`）

把音乐库、队列、播放与播放器控制暴露为 **MCP Server**，挂载在 MA 自己的 webserver 上（默认路径 **`/mcp/v1`**，复用现有端口 8095，无需额外开端口，天然兼容反代与 HA ingress）。

关键机制（源码核实）：

| 机制 | 内容 |
|---|---|
| 工具命名 | **`ma_api:<原生命令>`**，例如 `ma_api:music/search`、`ma_api:player_queues/play_media`。工具**从 MA 实时命令注册表动态编译**，不维护平行 API |
| 元发现工具 | `search_tools` → `get_tool_schema` → `call_tool` 三步工作流（避免一次性把上百个工具塞进上下文） |
| Resources | `library://`、`player://`、`queue://`、`catalog://commands` |
| 权限 | 默认**只读**；共 **29 个开关**（16 动作 + 3 资源 + 5 Debug + 5 Config）；每客户端独立 token，可单独吊销 |
| 状态 | `stage: experimental`，官方文档明确 "still in an early stage of development. Bugs may occur." |

> ⚠️ 上游插件仓库 README 称 "MCP surface contains exactly three tools"，与官方文档的细粒度 toggles 描述不一致（应为版本/模式差异）。**默认到底暴露 3 个 meta tool 还是全部 `ma_api:*`，未在运行实例验证。**

#### (b) 原生 HTTP / WebSocket API

| 端点 | 说明 |
|---|---|
| `POST http://<host>:8095/api` | 单一端点 + JSON-RPC 风格命令 |
| `GET  http://<host>:8095/ws` | WebSocket，命令名与 `/api` **完全一致** |
| `GET  /api-docs` | 自动生成文档（含 `rest_command` YAML 示例） |
| `GET  /api-docs/commands.json` | **机器可读的全量命令名与参数** ← 生成工具清单的最佳来源 |
| `GET  /api-docs/openapi.json` | OpenAPI 3.0.0（只描述 `POST /api` 一个端点 + 数据模型） |

请求体与鉴权：

```json
POST /api
Authorization: Bearer <MA long-lived token>   // Settings → Profile 生成
{"message_id":"1","command":"player_queues/play_media",
 "args":{"queue_id":"115ee854-…","media":"library://playlist/13","start_item":"Mirrors"}}
```

**MA 服务端命令命名空间**（与 HA 侧服务名完全不同，勿混淆）：

| 命名空间 | 示例命令 |
|---|---|
| 搜索/解析 | `music/search`（参数名是 **`search_query`**，不是 `query`）、`music/item_by_uri`、`music/item`、`music/item_by_name`、`music/browse`、`music/recently_played_items` |
| 库列表 | **`music/<type>s/library_items`**（`artists`/`albums`/`tracks`/`playlists`/`audiobooks`/`podcasts`/`genres`）。**通用 `music/library_items` 不存在** |
| 推荐 | `music/recommendations`、`music/recommendations/items` |
| 队列 | `player_queues/play_media`、`play_index`、`items`、`shuffle`、`repeat`、`clear`、`transfer`、`save_as_playlist` |
| 播放器 | `players/cmd/play`、`volume_set`、`power`、`group`、`ungroup`、`set_members`、`select_source` |
| 收藏/库编辑 | `music/favorites/add_item`、`music/library/add_item`、`music/sync` |
| 配置 | `config/players/get`、`config/players/save` |

> **对本插件的意义**：MCP 与 `/api` 需要"外部 agent"或"自建 HTTP 客户端"，而本插件是**HA 进程内的集成**。走这两条路会引入额外依赖、token 管理、网络可达性问题，并且**重复实现了 HA 已经封装好的东西**。因此它们适合作为「互补玩法」（例如让 Claude Desktop 直接管音乐库），而**不是**本插件的接入路径。本插件的正确姿势是走 HA 服务层。

### 2.5 MA 的 AI 引擎（反向接入，MA ≥ 2.9）

MA 的 **Home Assistant 插件**（安装在 MA 侧）会把 HA 里**每一个 `ai_task` 实体**暴露为 MA 的一个 "AI engine"，供这些 MA 功能使用：

- **AI Radio / AI DJ**（2.10 引入，AI + TTS）
- **Music Quiz** 的 AI 答案建议、Trivia 干扰项
- **Smart Playlist** 的 AI 描述生成

MA 侧的实际调用（`providers/hass/__init__.py`）：

```python
result = await self.hass.send_command(
    "call_service", domain="ai_task", service="generate_data",
    service_data={"task_name": "music_assistant", "instructions": query, "entity_id": entity_id},
    return_response=True,
)
```

**本插件已经提供 `ai_task` 实体**（`ai_task.py`，`subentry_type = "ai_task_data"`，支持 `GENERATE_DATA` / `SUPPORT_ATTACHMENTS` / `GENERATE_IMAGE`），且 `generate_data` 正是本插件实现的方法。

**⇒ 零代码反向打通：在 MA 侧把 AI engine 选为 `Home Assistant | AI Conversation AI Task` 即可，模型/Key/推理参数/提示词全部由本插件管理。**

> 方向要记清：**MA 是 AI 的消费方，不是提供方**。MA 不暴露 `ai_task` 实体，也不注册 conversation agent（HA core `music_assistant` 组件目录下确认无 `ai_task.py` / `conversation.py` / `intent.py`）。

---

## 3. HA 核心已经替我们做了什么（本次调研最关键的发现）

### 3.1 Assist LLM API 已插件化

`homeassistant/components/llm/__init__.py`（**HA 2026.8.0 起就存在**）：

```python
class AssistAPI(API):
    async def async_get_api_instance(self, llm_context) -> APIInstance:
        llm_tools = await async_get_tools(self.hass, llm_context, self.id)
        return APIInstance(..., tools=llm_tools.tools, ...)
```

`async_get_tools()` 通过 `LazyIntegrationPlatforms` 遍历**所有已加载集成**，凡是有 `<domain>/llm.py` 且实现了 `async_get_tools(hass, llm_context, api_id) -> LLMTools | None` 的，就把工具聚合进 Assist API。

**当前 15 个核心组件在用这个平台**：`assist_satellite`、`calendar`、`climate`、`fan`、`homeassistant`、`humidifier`、`intent`、`intent_script`、`lawn_mower`、`light`、**`llm`**、**`media_player`**、`script`、`todo`、`vacuum`。

约定与约束：
- 工具名必须以 `<domain>__` 前缀（`TOOL_PREFIX_BREAKS_IN_HA_VERSION = "2027.3"`，2027.3 起将成为硬性要求）
- `Tool.integration` 应填自身 domain，否则会触发 `report_untagged_tool`
- `Tool` 基类字段：`name` / `title` / `description` / `parameters`（schema）/ `annotations`（`read_only`、`destructive`、`idempotent`、`open_world`）

### 3.2 `media_player` 已经内置了音乐 LLM 工具（版本有差异）

| HA 版本 | `media_player/llm.py` 提供的工具 |
|---|---|
| **2026.9.4（已发布）** | 纯 `IntentTool` 包装：`HassMediaNext`、`HassMediaPause`、`HassMediaPlayerMute`、`HassMediaPlayerUnmute`、`HassMediaPrevious`、**`HassMediaSearchAndPlay`**、`HassMediaUnpause`、`HassSetVolume`、`HassSetVolumeRelative` |
| **dev（→ 2026.10+）** | 去掉 `HassMediaSearchAndPlay`，改为显式两个工具：`media_player__search_media` + `media_player__play_media`，并加了 `MAX_SEARCH_RESULTS = 35` |

dev 版本里有一行注释直接点名了 MA：

```python
# Some players return hundreds of results, which would fill the LLM context.
# This fits a full Music Assistant search: 5 results for each of 7 media types.
MAX_SEARCH_RESULTS = 35
```

`HassMediaSearchAndPlay` 处理器（`media_player/intent.py:283`）内部就是 `search_media` → `play_media` 两步。

**而 MA 的 `media_player` 实体能力位包含 `SEARCH_MEDIA | PLAY_MEDIA | BROWSE_MEDIA | MEDIA_ENQUEUE | MEDIA_ANNOUNCE`**，完全满足核心工具的前置条件。

**完整调用链（已逐层核实）**：

```
"在厨房放周杰伦"
  └─> Assist LLM API 工具 media_player__search_media / HassMediaSearchAndPlay
        └─> media_player.search_media  (HA 服务)
              └─> MA 集成 media_browser.async_search_media()
                    └─> mass.music.search(search_query, media_types, limit=5)   # MA 服务端
        └─> media_player.play_media(media_content_id, media_content_type)
              └─> MA 集成 _async_handle_play_media() → 解析 URI → 入队播放
```

即：**HA 语音/LLM → HA 服务 → MA 服务端**，全程不需要本插件参与，也不需要 MA 侧任何额外配置（除了暴露实体）。

### 3.3 生效条件（4 个门槛，必须都满足）

1. **子条目勾选了 Assist API** —— 本插件默认 `["assist"]` ✅ 默认满足
2. **`llm_context.assistant` 非空** —— `ConversationInput.as_llm_context()` 硬编码 `assistant=DOMAIN`（`"conversation"`），恒为真 ✅ 自动满足
3. **MA 的 media_player 实体已暴露给 `conversation` 助手** —— 需要用户在 **设置 → 语音助手 → 暴露** 中勾选 ⚠️ 需手动
4. **播放器支持 `SEARCH_MEDIA | PLAY_MEDIA`** —— MA speaker ✅ 满足

> **结论：只要用户把 MA 播放器暴露给 Assist，本插件**今天**就能用自然语言搜歌/点播/暂停/切歌/调音量，零代码改动。**

---

## 4. 方案对比

| | **方案 D：完全依赖核心** | **方案 C：YAML script 函数** | **方案 A：`llm.py` 工具平台** ⭐ | **方案 B：`functions/` 新类型** |
|---|---|---|---|---|
| 代码量 | 0 | 0（写 YAML） | ~250-400 行 + 1 文件 | ~150 行 + 注册表 |
| 用户配置成本 | 只需暴露实体 | 粘贴一段 YAML | 0（自动） | 手写 YAML spec |
| 搜索 + 播放 | ✅ | ✅ | ✅ | ✅ |
| 播放控制（暂停/切歌/音量） | ✅ | ✅ | ✅ | ✅ |
| 库浏览 / 收藏 / `library_only` | ❌ | ⚠️ 需自己拼 | ✅ | ✅ |
| 精准点播（URI / `radio_mode` / `enqueue`） | ❌ | ✅ | ✅（`start_item` 需 ≥2026.11） | ✅ |
| 队列管理（`get_queue` / `transfer_queue`） | ❌ | ⚠️ | ✅ | ✅ |
| 语音播报（`play_announcement`） | ❌ | ✅ | ✅ | ✅ |
| 确定性选音箱（area/floor 匹配） | ⚠️ 核心自带，但行为不可控 | ⚠️ | ✅ 完全可控 | ⚠️ |
| 与核心工具冲突风险 | — | 无 | **有重叠，需设计** | 无 |
| **版本兼容成本** | 2026.9.4 与 2026.10 工具名不同 | 全兼容，无 shim | **需 ≥2026.8，且要 3 处 shim**（`ToolResult` 返回类型、`async_get_match_preferences`、字段门控） | **全兼容，零 shim** |
| **受 2026.10 `llm.Tool` 契约变更影响** | — | 否 | **是**（`entity.py` 已有同类 shim 可复用） | **否**（`functions/` 走自有格式，不碰 `llm.Tool`） |
| 可测试性 | 无 | 差 | 好（可单测） | 中 |

### 推荐组合

```
第一阶段（0 成本验证）  方案 D + 方案 C   → 验证 MA 侧配置、暴露策略、模型对工具的调用质量
第二阶段（产品化）      方案 A            → 补齐能力，工具自动下发，用户零配置
第三阶段（可选）        方案 B            → 给不希望开 Assist API 的用户 / 需要自定义工具名描述的用户
```

**不建议只做方案 A 而忽略方案 D**：核心工具和方案 A 的工具有能力重叠，必须设计好"谁负责什么"，否则模型会二选一犹豫，甚至用错。

**⚠️ 关于 A 与 B 的取舍（本轮新增认识）**：方案 A 要承担 `llm.Tool` 的全部版本代价 —— 2026.10 的契约变更（返回类型 `ToolResult`、`title`/`annotations`/`integration`）、2026.10 才有的 `async_get_match_preferences`、加 MA 服务字段的逐版本门控，**合计 3 类 shim**。方案 B 因为走 `functions/` 自有 OpenAI-format 体系（`entity.py:241` 才转成 `llm.Tool` 的是核心工具，不是它），**完全不碰 `llm.Tool`，因此天然免疫**。

所以：
- 若目标是**尽快可用 + 用户零配置** → 方案 A，接受 3 处 shim（可复用 `entity.py` 已有的 `_TOOL_RESULT_USES_LLM_TOOL_RESULT` 模式）
- 若目标是**维护成本最低、跨版本稳** → 方案 B 优先级应当提高，甚至可以作为 A 的替代而非补充
- 现实建议：**A 做主路径（覆盖面广），B 同时提供**（给高级用户与 A 出问题时的逃生舱），二者共享同一套 `music/` 业务逻辑层，只是入口不同

### 方案 E：绕过 HA，直连 MA（MCP `/mcp/v1` 或 HTTP `/api`）—— 本次不选

MA 确实提供了两条"原生"通道（见 §2.4），并且在**独立 AI agent / 外部客户端**场景下是更优解。但对 `hass_ai_conversation` 这个**HA 进程内集成**，它们不成立：

| 维度 | 直连 MA | 走 HA 服务层（方案 A） |
|---|---|---|
| 新增依赖 | 需引入 MCP client 库，或自建 HTTP/aiohttp 客户端 | 零新增依赖（`hass.services.async_call`） |
| 凭据管理 | 需用户额外生成并保管 MA long-lived token | 复用 HA 已有的 MA config entry，无新凭据 |
| 网络可达性 | 需 HA 容器能直连 `<ma-host>:8095`（Docker/远程部署时未必通） | HA 集成已处理连接与鉴权 |
| 权限模型 | MA MCP 权限体系（29 开关），与 HA 的实体暴露体系**两套并行** | 复用 HA 的 expose + `context` 归属 |
| 用户上下文 | 需自行传递 MA `username` / impersonation | `context=llm_context.context` 自动带上 HA 用户 |
| 稳定性 | MCP 插件 `experimental`；`radio_mode` 在 MA dev 已标 Deprecated（内部转为 `radio_playlist://`） | 走稳定的 HA action 契约 |
| 重复建设 | 需自行维护工具 schema（可从 `/api-docs/commands.json` 生成） | 核心已把 MA 服务封装成 action |

**结论：方案 E 适合写进 README 作为"进阶玩法"（例如让 Claude Desktop 通过 MCP 管音乐库），但不作为本插件的实现路径。**

### 社区先例（可作为交叉验证）

| 项目 | 做法 | 与本站方案的关系 |
|---|---|---|
| [music-assistant/voice-support](https://github.com/music-assistant/voice-support) Option 3 | HA `script` 包 `music_assistant.play_media`，作为 LLM tool | **正是方案 C**，MA 官方仓库背书 |
| [glm9637/mass_ai_dj](https://github.com/glm9637/mass_ai_dj) | HACS 集成，Gemini + MA 通用搜索做动态 DJ | 走 HA/MA 搜索层，佐证"文本搜索直喂 MA"可行 |
| [teancom/aidj](https://github.com/teancom/aidj) | HACS 集成，用 **HA conversation agent** 生成口播 + response-capable `music_assistant.get_queue` 读队列 | 与本插件是**互补**关系：它可作为本插件工具层的下游消费者 |
| [JoeyG1973/Music-Assistant-MCP](https://github.com/JoeyG1973/Music-Assistant-MCP) | 自建 MCP server，prompt 里明确要求 LLM **不要**用 `HassMediaSearchAndPlay` | 反面证据：核心内置工具在某些部署下体验不足，正好说明方案 A 的补齐价值 |

---

## 5. 推荐方案详细设计（方案 A）

### 5.1 新增文件

```
custom_components/hass_ai_conversation/
├── llm.py                      # 新增：LLM 工具平台（方案 A 入口）
├── music/
│   ├── __init__.py
│   ├── compat.py               # ★ 版本 shim 层（llm.Tool 契约 + MA 服务字段探测）
│   ├── target.py               # 播放器目标解析（含 area/floor 版本降级）
│   ├── client.py               # music_assistant 服务封装（扁平参数 / 响应解包）
│   └── tools.py                # 5 个 Tool 子类 + MUSIC_PROMPT
└── functions/
    └── music_assistant.py      # 可选：方案 B 的 function 类型（与 music/ 共享业务层）
```

> `music/` 业务层与入口解耦：`llm.py`（方案 A）和 `functions/music_assistant.py`（方案 B）都调用同一套 `client.py` / `target.py`，避免逻辑重复。

### 5.2 `llm.py` 骨架

> **⚠️ 先读这段：`llm.Tool` 在 2026.10 发生了契约变更**（实测各版本 `helpers/llm.py` 关键字计数）：
>
> | 符号 | 2026.8.0 | 2026.9.4 | 2026.10.0b5 / dev |
> |---|---|---|---|
> | `class ToolResult` | ❌ 0 | ❌ 0 | ✅ 1 |
> | `class ToolAnnotations` | ❌ 0 | ❌ 0 | ✅ 1 |
> | `Tool.title` / `.annotations` / `.integration` | ❌ 0 | ❌ 0 | ✅ 2 |
> | `async_get_match_preferences` | ❌ 0 | ❌ 0 | ✅ 1 |
> | `probatio` | ❌ 0 | ✅ 1 | ✅ 19 |
> | `TOOL_INTEGRATION_BREAKS_IN_HA_VERSION` | — | — | ✅ (`"2027.10"`) |
>
> 含义：**本插件 min HA = 2026.8.0，而 `llm.Tool` 的新契约是 2026.10 才有的**。必须像 `entity.py` 已有的 `_TOOL_RESULT_USES_LLM_TOOL_RESULT` shim 一样做版本适配，否则在 2026.8/2026.9 上直接 `ImportError`。
>
> 好消息：`integration` / `title` / `annotations` 作为**类属性**赋值在旧版上是无害的（基类不声明、也不读取）；真正的硬约束只有 **`async_call` 的返回类型**。

```python
"""LLM tools for AI Conversation: Music Assistant integration."""

from homeassistant.components.llm import LLMTools
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import llm

from .const import DOMAIN                       # "hass_ai_conversation"
from .music.tools import (                      # 建议拆到子包，便于单测
    MusicSearchTool, MusicPlayTool, MusicLibraryTool,
    MusicAnnounceTool, MusicQueueTool, MUSIC_PROMPT,
)
from .music.compat import LLM_HAS_TOOL_RESULT   # 版本 shim，见下

MA_DOMAIN = "music_assistant"


@callback
def async_get_tools(
    hass: HomeAssistant, llm_context: llm.LLMContext, api_id: str
) -> LLMTools | None:
    """Return Music Assistant tools when MA is installed and Assist API is used."""
    if api_id != llm.LLM_API_ASSIST:
        return None
    if not llm_context.assistant:
        return None
    if not hass.config_entries.async_entries(MA_DOMAIN):   # 可选集成探测
        return None
    if not _any_ma_player_exposed(hass, llm_context.assistant):
        return None

    return LLMTools(
        tools=[
            MusicSearchTool(), MusicLibraryTool(), MusicPlayTool(),
            MusicQueueTool(), MusicAnnounceTool(),
        ],
        prompt=MUSIC_PROMPT,
    )


def _any_ma_player_exposed(hass: HomeAssistant, assistant: str) -> bool:
    """True if at least one exposed MA media_player exists (mirrors core media_player/llm.py:281-285)."""
    from homeassistant.components.homeassistant import async_should_expose

    return any(
        async_should_expose(hass, assistant, state.entity_id)
        for state in hass.states.async_all("media_player")
        if state.attributes.get("platform") == MA_DOMAIN
        # 更稳妥的判据是 supported_features 含 SEARCH_MEDIA|PLAY_MEDIA，或 entity registry 的 platform
    )
```

版本 shim（与 `entity.py:87` 的 `_TOOL_RESULT_USES_LLM_TOOL_RESULT` 同一思路）：

```python
# music/compat.py
from dataclasses import fields
from homeassistant.helpers import llm

# 2026.10 起 llm 模块才有 ToolResult；旧版只能返回裸 dict
LLM_HAS_TOOL_RESULT = hasattr(llm, "ToolResult")

if LLM_HAS_TOOL_RESULT:
    def tool_result(data: dict, *, error: bool = False):
        return llm.ToolResult(data=data, error=error)
else:
    def tool_result(data: dict, *, error: bool = False):
        return data          # 旧契约：直接返回 JsonObjectType
```

工具基类里统一走 `return tool_result({...})`，即可在 2026.8～2026.11 全域工作。

### 5.3 工具清单与命名

命名遵循核心前缀约定（`DOMAIN` = `hass_ai_conversation`）：

| 工具名 | 能力 | 底层调用 | 版本门控 |
|---|---|---|---|
| `hass_ai_conversation__music_search` | 跨 provider 搜索（歌/专辑/艺人/播放列表/电台/播客/有声书） | `music_assistant.search` | `username` 仅 ≥2026.9 |
| `hass_ai_conversation__music_browse_library` | 浏览本地库/收藏，支持分页与排序 | `music_assistant.get_library` | `username` 仅 ≥2026.9 |
| `hass_ai_conversation__music_play` | 精准点播：URI / 数字 ID / `radio_mode` / `enqueue` | `music_assistant.play_media` | `start_item` **仅 ≥2026.11**，须运行时探测 |
| `hass_ai_conversation__music_queue` | 查队列、队列转移 | `music_assistant.get_queue` / `transfer_queue` | 全版本 ✅ |
| `hass_ai_conversation__music_announce` | 音频播报 | `music_assistant.play_announcement` | `message`+`tts_entity_id` **仅 ≥2026.10**；≤2026.9 只能 `url` |

> **⚠️ 这是方案 A 最重要的实现约束。** 本插件 min HA = 2026.8.0，而上述字段是逐版本新增的；**直接把 `start_item` / `message` 写进工具参数会在 2026.8～2026.10 上触发 schema 校验失败**，导致工具调用报错、模型反复重试。
>
> 两种正确做法（建议同时用）：
> 1. **运行时探测再决定 schema** —— 用 `hass.services.async_services()` 取已注册服务的字段集合，动态裁剪 `parameters`，让 LLM 根本不看到不可用字段：
>    ```python
>    fields = hass.services.async_services().get("music_assistant", {}).get("play_media", {}).get("fields", {})
>    has_start_item = "start_item" in fields
>    ```
> 2. **工具描述里降级说明** —— 不可用时把该项从 schema 移除，并在 `description` 里告诉模型"本系统不支持从指定曲目开始播放"。
>
> `music_announce` 在 ≤2026.9 上应退化为"只播 URL"，并在 `MUSIC_PROMPT` 里说明无法用 TTS 播报（引导用户改用核心的 TTS/`tts.speak` + 媒体播放）。

**各工具的版本可用性总览**（✅ 可用 / ⚠️ 需降级）：

| 工具 | 2026.8.0 | 2026.9.4 | 2026.11+ |
|---|---|---|---|
| `music_search` | ✅（无 `username`） | ✅ | ✅ |
| `music_browse_library` | ✅（无 `username`） | ✅ | ✅ |
| `music_play` | ✅（无 `start_item`） | ✅（无 `start_item`） | ✅（含 `start_item`） |
| `music_queue` | ✅ | ✅ | ✅ |
| `music_announce` | ⚠️ 仅 URL | ⚠️ 仅 URL | ✅ 支持 TTS |

### 5.4 目标播放器解析（确定性 > 猜测）

直接复用核心 `media_player/llm.py` 的做法，保证行为一致（`intent.async_match_targets` 及 `single_target`/`floor_name`/`assistant`/`allow_duplicate_names` 字段**已实测存在于 2026.8.0**，本身无版本门控）：

```python
constraints = intent.MatchTargetsConstraints(
    name=args.get("player_name"),
    area_name=args.get("player_area"),
    floor_name=args.get("player_floor"),
    domains={"media_player"},
    assistant=llm_context.assistant,
    features=(MediaPlayerEntityFeature.SEARCH_MEDIA
              | MediaPlayerEntityFeature.PLAY_MEDIA),
    single_target=True,
)
preferences = _match_preferences(hass, llm_context)   # ← 必须版本适配，见下
result = intent.async_match_targets(hass, constraints, preferences)
```

**⚠️ `preferences` 的获取方式逐版本不同**（实测 `helpers/llm.py` 与 `helpers/device_registry.py`）：

| HA 版本 | 可用方式 |
|---|---|
| **≥2026.10** | `llm.async_get_match_preferences(hass, llm_context)`（2026.10 新增，内部已处理 device→area→floor） |
| **2026.9.x** | 无该函数，需手工推导：`dr.async_get(hass).async_get(device_id)` → `dr.async_get_effective_area_id(hass, device)` → `ar.async_get(hass).async_get_area(area_id)` → `fr.async_get(hass).async_get_floor(area.floor_id)` |
| **2026.8.0** | 更受限：**`dr.async_get_effective_area_id` 在 2026.8 不存在**（2026.9 才加入），只能读 `device.area_id`，拿不到继承来的区域 |

```python
def _match_preferences(hass, llm_context) -> intent.MatchTargetsPreferences:
    """Version-adaptive area/floor preference derivation."""
    if hasattr(llm, "async_get_match_preferences"):          # >= 2026.10
        return llm.async_get_match_preferences(hass, llm_context)

    device_id, area_id, floor_id = llm_context.device_id, None, None
    device = dr.async_get(hass).async_get(device_id) if device_id else None
    if device is not None:
        if hasattr(dr, "async_get_effective_area_id"):        # >= 2026.9
            area_id = dr.async_get_effective_area_id(hass, device)
        else:                                                 # 2026.8
            area_id = device.area_id
    if area_id:
        area = ar.async_get(hass).async_get_area(area_id)
        floor_id = area.floor_id if area else None
    return intent.MatchTargetsPreferences(area_id=area_id, floor_id=floor_id)
```

> 注意 `llm.LLMContext` **没有 `area_id` 字段**（只有 `platform` / `context` / `language` / `assistant` / `device_id`），区域必须自己从 `device_id` 推导 —— 这也是为什么本插件 `const.py:29` 的默认 prompt 里要用 `area_id(llm_context.device_id)` 模板函数。

三级回退：**显式指定播放器 → 设备（语音卫星）所在区域的 MA 播放器 → 唯一可用 MA 播放器**。若仍无法确定，**返回错误让 LLM 反问用户，绝不猜**。

> 若 2026.8 的降级推导导致匹配率不满意，可把该功能的最低要求提到 2026.9（在 `_match_preferences` 里对 2026.8 直接返回空 preferences 并依赖显式指定）。

### 5.5 调用服务

```python
# 普通服务（search / get_library）：响应是扁平 dict
response = await hass.services.async_call(
    "music_assistant", "search",
    {"config_entry_id": entry_id, "name": name,
     "media_type": media_types, "limit": 5, "library_only": False},   # 扁平参数（分组仅影响 UI）
    context=llm_context.context,          # 保留调用上下文（权限/归属）
    blocking=True, return_response=True,
)
tracks = response["tracks"]               # 直接取 key

# 实体级服务（get_queue / play_media / ...）：响应按 entity_id 分组
queue_resp = await hass.services.async_call(
    "music_assistant", "get_queue", {"entity_id": entity_id},
    context=llm_context.context, blocking=True, return_response=True,
)
queue = queue_resp[entity_id]             # ⚠️ 必须再按 entity_id 取一层
current = queue["current_item"]           # 可能是 None
```

**易错点清单**：
- `config_entry_id` **必填**，需从 `hass.config_entries.async_entries("music_assistant")` 取
- `limit` / `library_only` / `offset` 是**扁平**的，不是 `search_options.limit` / `pagination.offset`（那只是 UI 折叠分组）
- `play_media` / `play_announcement` / `get_queue` / `transfer_queue` 是**实体级服务**，必须带 `entity_id`
- 实体级服务的响应**再包一层 `entity_id`**（`helpers/service.py:811`）
- `get_queue` 的 `items` 是**条数**，`get_library` 的 `items` 是**数组**
- `search` / `get_library` / `get_queue` 声明了 `SupportsResponse.ONLY` ⇒ 程序调用必须 `blocking=True, return_response=True`（`response_variable` 是脚本/YAML 概念，不是 `async_call` 参数）
- `play_media` / `play_announcement` / `transfer_queue` **没有响应** ⇒ 传 `return_response=True` 会报错
- `start_item`（≥2026.11）、`message`/`tts_entity_id`（≥2026.10）、`username`（≥2026.9）需**运行时探测后再传**，否则校验失败

### 5.6 schema 编写约束（受 §1.3 限制）

```python
import voluptuous as vol
from homeassistant.helpers import config_validation as cv

parameters = vol.Schema({
    vol.Required("query"): cv.string,
    vol.Optional("media_type"): cv.string,      # 不要用 vol.In([...])，enum 会被剥离
    vol.Optional("limit"): cv.positive_int,
})
```

- ❌ 不要用 `vol.In()` / enum —— `_strip_unsupported_keywords` 会递归删除 `enum`，LLM 看不到候选值，应改在 `description` 里用自然语言说明
- ✅ 每个属性都要有 `type`，否则 `_adjust_schema` 会 `KeyError`
- ⚠️ **`_adjust_schema` 会把所有属性变 required 且 nullable** ⇒ 模型可能显式传 `null`。工具 `async_call` 里必须 **`{k: v for k, v in args.items() if v is not None}`** 过滤后再调服务，否则 `entity_id: None`/`limit: None` 会触发 MA 端 schema 校验失败
- ✅ 用 `vol.Schema` 而非直接 `probatio.Schema`，可同时兼容 2026.8（无 probatio）与 2026.9+
  - 补充：HA 2026.9 起依赖中已无 voluptuous，靠 `probatio.compat.install_as_voluptuous()` 注入 `sys.modules` 别名，因此 `import voluptuous as vol` 照常可用，且 `vol.Schema is probatio.Schema`（同一类）。**本插件 `functions/base.py` 的现有 `vol` 用法无需改动**
- ✅ 工具名加 `hass_ai_conversation__` 前缀，并设置 `Tool.integration = DOMAIN`
  - `TOOL_PREFIX_BREAKS_IN_HA_VERSION = "2027.3"`（2026.9 起仅告警）
  - `TOOL_INTEGRATION_BREAKS_IN_HA_VERSION = "2027.10"`（2026.10 起对 custom 集成仅 LOG 告警）
  - 二者在 2026.8/2026.9 上是**无害的类属性赋值**（基类不声明也不读取），可放心写

### 5.7 manifest 变更

```json
{
  "after_dependencies": ["music_assistant"]
}
```

用 `after_dependencies` 而**不是** `dependencies`：MA 是可选集成，硬依赖会导致未安装 MA 的用户无法加载本插件。运行时用 `hass.config_entries.async_entries("music_assistant")` 探测。

### 5.8 与核心工具的分工（避免模型选择困难）

| 场景 | 交给谁 |
|---|---|
| "放首歌 / 放周杰伦 / 在厨房放点音乐" | **核心** `media_player__search_media` + `play_media`（2026.10+）或 `HassMediaSearchAndPlay`（2026.9） |
| "播放我的收藏"、"列出我的播放列表"、"有哪些专辑" | **方案 A** `music_browse_library` |
| "用电台模式放这首歌"、"接着当前队列后面加" | **方案 A** `music_play`（`radio_mode` / `enqueue`） |
| "从第 3 首开始播" | **方案 A** `music_play`（`start_item`，**仅 HA ≥2026.11**；更早版本需降级为"先播整张再跳到第 3 首"或用 `media_player.media_seek`） |
| "把客厅的队列挪到卧室"、"现在放到第几首了" | **方案 A** `music_queue` |
| "播报：吃饭了" | **方案 A** `music_announce`（HA ≤2026.9 只能播音频 URL；TTS 需 ≥2026.10） |

建议在 `MUSIC_PROMPT` 里用一句话写清这个分工，并在配置里提供开关允许用户关闭方案 A 的工具。

---

## 6. 风险与限制

| 风险 | 等级 | 说明与对策 |
|---|---|---|
| **`llm.Tool` 契约 2026.10 破坏性变更** | **高**（仅方案 A） | `ToolResult` / `ToolAnnotations` / `Tool.title` / `Tool.annotations` / `Tool.integration` / `async_get_match_preferences` 在 **2026.8 与 2026.9.4 上全部不存在**（实测计数 0），2026.10.0b5 与 dev 字节级相同。方案 A 必须做版本 shim（返回类型 `dict` vs `ToolResult`；area/floor 推导降级），否则旧版 ImportError。方案 B 完全免疫 |
| **服务字段逐版本新增** | **高** | `play_media.start_item` 仅 ≥2026.11、`play_announcement.message`/`tts_entity_id` 仅 ≥2026.10、`search`/`get_library` 的 `username` 仅 ≥2026.9。本插件 min HA = 2026.8，**硬编码这些字段会导致 schema 校验失败**。必须运行时探测（`hass.services.async_services()`）后裁剪工具 schema |
| **`dr.async_get_effective_area_id` 2026.8 缺失** | 中 | 该函数 2026.9 才加入，2026.8 只能读 `device.area_id`（拿不到继承区域）→ 2026.8 上的区域匹配准确率下降 |
| **voluptuous 与 probatio 共存** | 低 | HA 2026.9 起依赖中已无 voluptuous，靠 `probatio.compat.install_as_voluptuous()` 注入 `sys.modules` 别名，`import voluptuous as vol` 仍可用且 `vol.Schema is probatio.Schema`。本插件 `functions/base.py` 的 `vol` 用法**无需改动**；但 `requirements` 里的 `voluptuous-openapi` 在 ≥2026.9 已无用（仅 <2026.9 回退路径需要），可考虑标注 |
| **版本漂移（核心 LLM 工具）** | 中 | 核心 `media_player/llm.py` 在 2026.9.4 → 2026.10 发生了工具替换（`HassMediaSearchAndPlay` 被拆成两个工具）。方案 A 必须避免依赖具体核心工具名，只做能力互补 |
| **工具命名强制前缀** | 低 | `TOOL_PREFIX_BREAKS_IN_HA_VERSION = "2027.3"`，现在就按 `<domain>__` 命名可免疫 |
| **schema 关键字被剥离** | 中 | `enum`/`oneOf`/`anyOf`/`allOf`/`not` 被递归删除，且所有属性被强制 required + nullable。schema 要极简，靠 description 表达约束 |
| **响应结构陷阱** | 中 | ① 实体级服务响应按 `entity_id` 再包一层；② `get_queue.items` 是条数、`get_library.items` 是数组；③ `ItemMapping` 只有 5 个键。写工具时要按类型分支处理，不能想当然 |
| **`media-source://` 误用** | 中 | MA **没有** media source 平台；官方明确警告不要给 MA 播放器传 `media-source://` URI（行为不一致）。工具里应对该前缀做拦截或改写 |
| **`radio_mode` / `start_item` 语义变更** | 中 | MA 服务端 `player_queues/play_media` 的 `radio_mode` 在 `dev` 已标 **Deprecated**（内部转为 `radio_playlist://`）。方案 A 应优先用 HA action 而非 MA 原生命令，以隔离该变更 |
| **MA Server < 2.10 的能力落差** | 中 | `library://type/id` 的 `verify_item_uri` 路径需 server schema ≥33、`username` 需 ≥35、TTS 播报消息需 ≥46 —— **实际都要求 MA ≥ 2.10.0**。MA 2.9 会走旧分支（`"://"` 直通），行为有差异。应在工具里做能力探测并在 README 标注推荐 MA ≥ 2.10.5 |
| **MA MCP 插件仍在实验阶段** | 低 | `stage: experimental`。本报告不依赖该通道，仅作进阶说明 |
| **MA 未安装** | 低 | `after_dependencies` + 运行时探测，工具返回 `None`，对现有用户零影响 |
| **暴露策略是前置条件** | 中 | MA 播放器未暴露给 `conversation` 助手 → 核心工具直接返回 `None`。需要在文档/README 中明确指引，或由插件做一次"未检测到已暴露 MA 播放器"的提示 |
| **参数校验失败导致 LLM 反复重试** | 中 | 工具内 `try/except HomeAssistantError`，返回 `{"error": ..., "error_text": ...}`（`entity.py:594` 已有对齐核心的约定），让模型能自我纠正 |
| **`config_entry_id` 多实例** | 低 | 用户可能有多个 MA 实例；默认取第一个，多实例时在工具描述中暴露选择 |
| **Token 膨胀** | 中 | 搜索结果最多 35 条（对齐核心 `MAX_SEARCH_RESULTS`）。`get_library` 的 `limit` 要给保守默认值 |
| **`execute_service` 无法调 `search`** | — | 现 `native.execute_service_single`（`functions/native.py:75`）要求 `entity_id`/`area_id`/`device_id` 至少一个，且不支持 `return_response`，因此**无法**用它调 `music_assistant.search`。库搜索必须走方案 A/B/C |

---

## 7. 分期路线图

### Phase 0 — 零成本验证（0.5 天）
1. 在测试 HA 上装好 MA（Server 2.10.5）+ 核心 `music_assistant` 集成
2. 把 MA 播放器暴露给 Assist（设置 → 语音助手 → 暴露）
3. 用本插件对话代理说"在厨房放周杰伦"，确认核心工具生效
4. 用方案 C 的 YAML function tool 试 `music_assistant.search` + `play_media` 两步链路
5. **记录**：模型选了哪个工具、失败模式、参数错误类型

### Phase 1 — 方案 A 最小可用（3-4 天，含 shim）
1. **先写 `music/compat.py` 版本 shim 层**：`LLM_HAS_TOOL_RESULT`、`tool_result()`、`_match_preferences()`、服务字段探测（`service_fields()`）—— 这是后续所有工具的地基，**不要跳过**
2. 新增 `llm.py` + `music/tools.py`，先只做 `music_browse_library` 和 `music_play`
3. `manifest.json` 加 `after_dependencies`
4. 写单测：schema 序列化不报错、服务参数正确、目标解析回退链、**shim 在两个契约下的分支都覆盖**
5. 在真实 HA 上跑，确认工具出现在 `chat_log` trace 中
6. **务必在 HA 2026.8.0 与 2026.10.x 各验证一次**（两个契约端点）

### Phase 2 — 补齐与打磨（2-3 天）
1. 加 `music_search` / `music_queue` / `music_announce`
2. `MUSIC_PROMPT` 写清与核心工具的分工
3. 配置项：开关方案 A 工具 / 多 MA 实例选择
4. 更新 `README.md`、`CHANGELOG.md`、`strings.json` + 13 种翻译

### Phase 3 — 可选增强
1. 方案 B：`functions/music_assistant.py`（YAML 逃生舱）
2. MA FastMCP 通路文档：面向 Claude Desktop 等外部客户端的互补玩法
3. 反向文档：如何把本插件 ai_task 设为 MA 的 AI engine

---

## 8. 验证清单（可执行）

```bash
# 1. 语法/导入自检
python -m compileall -q custom_components/hass_ai_conversation
python -m json.tool custom_components/hass_ai_conversation/manifest.json > /dev/null

# 2. 确认 HA 版本落在支持区间
python -c "import homeassistant; print(homeassistant.__version__)"

# 3. 确认核心 LLM 平台与媒体工具存在（HA 2026.8+）
python - <<'PY'
from homeassistant.components.llm import AssistAPI, LLMTools, async_get_tools
import homeassistant.components.media_player.llm as mpllm
print([t.__name__ for t in (mpllm.MediaSearchTool, mpllm.MediaPlayTool)]
      if hasattr(mpllm, "MediaSearchTool") else mpllm.LLM_INTENTS)
PY

# 4. 关键：探测 MA 服务字段的版本可用性（方案 A 必须据此裁剪工具 schema）
python - <<'PY'
# 在 HA 环境内运行；或对照本报告 §2.2 的版本门控表
from homeassistant.core import HomeAssistant
# hass.services.async_services()["music_assistant"]["play_media"].get("fields", {})
for svc in ("search", "get_library", "play_media", "play_announcement",
            "transfer_queue", "get_queue"):
    print(svc, "->", "需确认已注册")
print("play_media.start_item     可用当且仅当 HA >= 2026.11")
print("announce.message/tts      可用当且仅当 HA >= 2026.10")
print("search/get_library.username 可用当且仅当 HA >= 2026.9")
PY

# 5. 关键：探测 llm 模块的版本契约（方案 A 的 shim 依据）
python - <<'PY'
from homeassistant.helpers import llm
from homeassistant.helpers import device_registry as dr
print("llm.ToolResult            :", hasattr(llm, "ToolResult"),        "(>=2026.10 才为 True)")
print("llm.ToolAnnotations       :", hasattr(llm, "ToolAnnotations"),   "(>=2026.10 才为 True)")
print("llm.async_get_match_prefs :", hasattr(llm, "async_get_match_preferences"), "(>=2026.10)")
print("dr.async_get_effective_area_id:", hasattr(dr, "async_get_effective_area_id"), "(>=2026.9)")
PY
```

手工验收（含**跨版本**回归）：
- [ ] 未安装 MA 时，插件正常加载，日志无报错，工具列表不变
- [ ] 安装 MA 但未暴露播放器时，工具不出现（或出现但返回明确错误）
- [ ] "列出我的播放列表" → 走 `music_browse_library`
- [ ] "在厨房用电台模式放这首歌" → 走 `music_play` 且 `radio_mode=true`，目标命中厨房音箱
- [ ] "播报吃饭了" → 走 `music_announce`；**在 HA 2026.9.4 上确认降级为"仅 URL"且不报 schema 错误**
- [ ] "从第 3 首开始播" → **在 HA 2026.9.4 上确认该参数未出现在工具 schema 中**（不触发校验失败）
- [ ] **在 HA 2026.8.0 上确认 `llm.py` 可正常 import**（无 `ToolResult`/`ToolAnnotations`/`async_get_match_preferences` 引用错误），工具返回裸 dict
- [ ] **在 HA 2026.8.0 上确认区域推导走 `device.area_id` 分支**且不抛异常
- [ ] **在 HA 2026.10+ 上确认返回 `llm.ToolResult` 而非裸 dict**（否则触发 deprecation 告警）
- [ ] `get_queue` 返回后能正确按 `entity_id` 取值，且 `items` 被当作**条数**而非数组
- [ ] `get_library` 返回中介于 `ItemMapping` 的条目（只有 5 个键）不会导致 `KeyError`
- [ ] 向 MA 播放器传 `media-source://` 时被拦截并给出明确提示
- [ ] 工具调用失败时返回 `{"error","error_text"}` 且模型能纠正重试
- [ ] `chat_log` trace 中不出现 schema 序列化异常
- [ ] **在 HA 2026.8.0 / 2026.9.4 / 2026.10.x / 2026.11 四个版本上各跑一遍**（本报告识别的两条最高风险均为跨版本差异）

---

## 9. 参考资料

### Home Assistant 核心源码（一手）
- [**HA 开发文档：Integration service actions**](https://developers.home-assistant.io/docs/dev_101_services/) — **本节纠正了初版误判**：明确 "collapsible section only affect presentation… service action data will not be nested"，即 `search_options`/`pagination` 只是 UI 分组
- [`music_assistant/services.py`](https://github.com/home-assistant/core/blob/dev/homeassistant/components/music_assistant/services.py) — 服务注册与真实 schema
- [`music_assistant/services.yaml`](https://github.com/home-assistant/core/blob/dev/homeassistant/components/music_assistant/services.yaml) — UI 字段定义（含折叠分组）
- [`music_assistant/schemas.py`](https://github.com/home-assistant/core/blob/dev/homeassistant/components/music_assistant/schemas.py) — 响应结构（`get_queue.items` 为 `int`、`get_library.items` 为 `EnsureList` 的出处）
- [`music_assistant/media_player.py`](https://github.com/home-assistant/core/blob/dev/homeassistant/components/music_assistant/media_player.py) — `media_id` 解析规则、能力位
- [`music_assistant/media_browser.py`](https://github.com/home-assistant/core/blob/dev/homeassistant/components/music_assistant/media_browser.py) — `async_search_media`（`limit = 5`）
- [`music_assistant/const.py`](https://github.com/home-assistant/core/blob/dev/homeassistant/components/music_assistant/const.py) — 属性名
- [`music_assistant/helpers.py`](https://github.com/home-assistant/core/blob/dev/homeassistant/components/music_assistant/helpers.py) — 客户端获取与错误转换
- [`helpers/service.py`](https://github.com/home-assistant/core/blob/dev/homeassistant/helpers/service.py) — **实体级服务响应按 `entity_id` 包装**（第 811 行）
- [`components/llm/__init__.py`](https://github.com/home-assistant/core/blob/dev/homeassistant/components/llm/__init__.py) — AssistAPI + LazyIntegrationPlatforms（**方案 A 的基础**）
- [`components/media_player/llm.py`](https://github.com/home-assistant/core/blob/dev/homeassistant/components/media_player/llm.py) — 核心音乐 LLM 工具（dev）
- [`components/media_player/llm.py@2026.9.4`](https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/components/media_player/llm.py) — 已发布版本的工具集差异
- [`components/media_player/intent.py`](https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/components/media_player/intent.py) — `HassMediaSearchAndPlay` 处理器
- [`helpers/llm.py`](https://github.com/home-assistant/core/blob/dev/homeassistant/helpers/llm.py) — `Tool` / `APIInstance` / `LLMContext` / `IntentTool`
- [`helpers/integration_platform.py`](https://github.com/home-assistant/core/blob/dev/homeassistant/helpers/integration_platform.py) — `LazyIntegrationPlatforms` 发现机制
- [`components/conversation/models.py`](https://github.com/home-assistant/core/blob/dev/homeassistant/components/conversation/models.py) — `ConversationInput.as_llm_context`（`assistant=DOMAIN`）

### Music Assistant（官方文档）
- [HA 集成总览](https://www.music-assistant.io/integration/) — 三个组件的方向关系
- [语音控制](https://www.music-assistant.io/integration/voice/) — HA 2025.6 起内置 Search and Play intent，"created with Music Assistant in mind"
- [Home Assistant 插件](https://www.music-assistant.io/ha-plugin/) — 每个 `ai_task` 实体 → MA AI engine（**反向通路**）
- [FastMCP Server 插件](https://www.music-assistant.io/plugins/fastmcp-server/) — `/mcp/v1` MCP Server
- [OpenAI Compatible 插件](https://www.music-assistant.io/plugins/openai_compatible/) — MA 侧 AI 引擎的另一来源
- [MA API](https://www.music-assistant.io/api/) — `POST /api` + Long-lived token
- [MA search Action](https://www.music-assistant.io/faq/masssearch/) — 脚本中使用响应数据
- [voice-support 蓝图仓库](https://github.com/music-assistant/voice-support) — Option 3 即"脚本作为 LLM 工具"的社区先例（对应方案 C）
- [2.9 发布说明](https://www.music-assistant.io/blog/2026/06/10/music-assistant-2-9/) — MCP Server provider（PR #3858）、HA Plugin AI/TTS（PR #3607）
- [2.10 发布说明](https://www.music-assistant.io/blog/2026/08/26/music-assistant-2-10/) — AI Radio DJ（PR #3407）、OpenAI Compatible provider（PR #5261）
- [AI Radio 插件](https://www.music-assistant.io/plugins/ai-radio/) — MA 自带的 AI DJ（**消费** AI engine）
- [Sonic Similarity 插件](https://www.music-assistant.io/plugins/sonic-similarity/) — 本地 CLAP 语义检索，非 LLM
- [music-assistant/server](https://github.com/music-assistant/server) — Server 2.10.5 stable / 2.11.0b4 dev

### Music Assistant 服务端源码（MA 原生命令签名，一手）
- [`controllers/music/controller.py`](https://github.com/music-assistant/server/blob/dev/music_assistant/controllers/music/controller.py) — `music/search`（参数 **`search_query`**）等
- [`controllers/music/media/base.py`](https://github.com/music-assistant/server/blob/dev/music_assistant/controllers/music/media/base.py) — `music/<type>s/library_items` 动态注册
- [`controllers/player_queues/controller.py`](https://github.com/music-assistant/server/blob/dev/music_assistant/controllers/player_queues/controller.py) — `player_queues/play_media`（`radio_mode` 已 Deprecated）
- [`controllers/players/controller.py`](https://github.com/music-assistant/server/blob/dev/music_assistant/controllers/players/controller.py) — `players/cmd/*`
- [`controllers/webserver/controller.py`](https://github.com/music-assistant/server/blob/dev/music_assistant/controllers/webserver/controller.py) — `/api`、`/ws`、`/api-docs*` 路由表
- [`providers/hass/__init__.py`](https://github.com/music-assistant/server/blob/dev/music_assistant/providers/hass/__init__.py) — MA 调 HA `ai_task.generate_data`
- [`providers/fastmcp_server/execution.py`](https://github.com/music-assistant/server/blob/dev/music_assistant/providers/fastmcp_server/execution.py) — 工具名 `ma_api:<command>`
- [`providers/fastmcp_server/meta_discovery.py`](https://github.com/music-assistant/server/blob/dev/music_assistant/providers/fastmcp_server/meta_discovery.py) — `search_tools` / `get_tool_schema` / `call_tool`
- [MA 服务器端 2.9.0 / 2.10.0 插件目录按 tag 探测](https://github.com/music-assistant/server/tags) — 本报告据此确认 `fastmcp_server`(2.9) / `ai_radio`+`openai_compatible`(2.10)

### 社区先例（LLM ↔ MA）
- [glm9637/mass_ai_dj](https://github.com/glm9637/mass_ai_dj) — HACS 集成，Gemini + MA 通用搜索做动态 DJ
- [teancom/aidj](https://github.com/teancom/aidj) — HACS 集成，用 HA conversation agent + response-capable `get_queue`（与本插件互补）
- [JoeyG1973/Music-Assistant-MCP](https://github.com/JoeyG1973/Music-Assistant-MCP) — 自建 MCP，prompt 明确弃用核心内置工具（方案 A 的补齐价值佐证）

### 已废弃的 `mass` 自定义集成（历史对照，勿使用）
- [music-assistant/hass-music-assistant](https://github.com/music-assistant/hass-music-assistant) — README：*"The custom integration is deprecated and no longer maintained!"*；2025-01-02 仅 2 个 commit 后停更
- 其 domain 为 **`mass`**（`custom_components/mass/`），服务是 `mass.play_media` / `mass.play_announcement` / `mass.transfer_queue` / `mass.search` / `mass.get_library` / `mass.get_queue`，与现役 `music_assistant.*` **无别名、无重定向**
- 它曾注册过 intent `MassPlayMediaAssist` / `MassPlayMediaOnMediaPlayer`（后者仅在某 entry 配了 `openai_agent_id` 时注册，用 `conversation.process` 让 LLM 返回 JSON 再调 MA）—— 这是 MA 侧最早的 "LLM → MA" 实现，**未进入 HA core**

### 附带产出
- `ma_ha_integration_report.md` — 并行的独立调研存档（707 行，位于**仓库之外**的 `/workspace/Hass/` 下），包含逐版本 tag 实测的 HA 服务时间线、MA client/server `schema_version` 门槛对照表（server schema ≥33 `library://` 校验 / ≥35 `username` / ≥46 TTS 播报消息）、以及废弃 `mass` 集成的完整快照。**本报告已吸收其经核实的内容，并纠正了其中把 `services.yaml` 嵌套分组当作真实 schema 的引用**
- `ha-llm-api-2026-report.md` — 并行独立调研存档（2047 行，位于 `/workspace/Hass/docs/`），含 HA 2026.8/2026.9.4/2026.10.0b5/dev 四个 ref 的逐行引用与约 180 个 URL。**本报告 §5.2 / §5.4 的版本 shim 即源自该报告并经我实测复核**

### HA 开发者文档与破坏性变更公告
- [LLM API](https://developers.home-assistant.io/docs/core/llm/) — `Tool` / `ToolResult` / `ToolAnnotations` 当前契约
- [Integration service actions](https://developers.home-assistant.io/docs/dev_101_services/) — 折叠分组、响应数据、实体服务
- [AI Task 实体](https://developers.home-assistant.io/docs/core/entity/ai-task) — `GenDataTask` / `GenDataTaskResult`
- [2026-09-26 LLM tool result 变更](https://developers.home-assistant.io/blog/2026/09/26/llm-tool-result) — `ToolResult` 引入公告
- [2026-09-30 probatio 校验引擎](https://developers.home-assistant.io/blog/2026/09/30/probatio-validation-engine) — voluptuous 替换与 `install_as_voluptuous()` 兼容层

---

## 10. 调研方法与可信度说明

本报告的所有技术结论均来自**一手源码/官方文档**，并在三路并行调研后做了**交叉核实**。过程中主动纠正了三处初版错误（其中两处是我自己的误判）：

| 初版结论 | 核实后 | 纠正依据 |
|---|---|---|
| `search_options`/`pagination` 是"上游 schema 漂移/bug" | **是 HA 的 UI 折叠分组机制**，数据始终扁平 | [HA 开发文档](https://developers.home-assistant.io/docs/dev_101_services/) 明写 "service action data will not be nested" |
| `start_item` / `play_announcement.message` 可直接使用 | **是 2026.10/2026.11 新增字段**，2026.8/2026.9 上不可用 | 逐版本 `services.py` 关键字计数实测（`start_item` 0/0/2、`message` 0/0/4） |
| §5.2/§5.4 示例代码按 HA ≥2026.8 编写 | **`ToolResult`/`ToolAnnotations`/`async_get_match_preferences`/`dr.async_get_effective_area_id` 在 2026.8～2026.9.4 上不存在**，直接 ImportError；已补 3 处版本 shim | 逐版本 `helpers/llm.py` 与 `helpers/device_registry.py` 关键字计数实测（全部为 0） |

**核实方法**：所有版本门控结论均通过**逐 tag 拉取源码 + 关键字计数**独立复现，未采信任何单一来源的转述。三份调研报告凡有冲突之处，一律以源码实测为准。

**仍标记为未验证的点**（使用时需自行确认）：
1. MA MCP 默认工具集合（官方文档 29 个 toggles vs 上游 README "exactly three tools"）—— 需在运行实例验证
2. `get_library` 的 `album_type` 运行时类型写作 `list[MediaType]`，probatio 是否会把 `single` 强转为 `unknown` —— 未验证
3. MA Server < 2.10 时服务端是否原生接受 `library://…`（HA 侧只做 `"://"` 直通）—— 未验证
4. `/api-docs/openapi.json` → OpenAI function schema 的自动转换可行性 —— 存在 OpenAPI 3.0.0，但转换效果未验证
5. **HA 2026.12 无任何 tag**，本报告未覆盖；2026.10 正式版与 `2026.10.0b5` 是否完全一致未验证
6. 上游 `extended_openai_conversation` 是否已迁移到 `llm` 平台 —— 未验证
