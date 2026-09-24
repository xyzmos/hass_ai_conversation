# AI Conversation (hass_ai_conversation)

[![Validate](https://github.com/xyzmos/hass_ai_conversation/actions/workflows/validate.yml/badge.svg)](https://github.com/xyzmos/hass_ai_conversation/actions/workflows/validate.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-%E2%89%A52026.8-41BDF5.svg)](https://www.home-assistant.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

一个 Home Assistant 自定义集成，扩展 OpenAI 对话能力：支持自定义 function tools、设备别名映射、上下文截断策略、流式响应、AI 任务结构化输出、Azure OpenAI 等多端点。

> 本仓库为 **HACS 自定义仓库（Custom Repository）**，请通过 HACS → 集成 → 自定义仓库添加。

## 功能特性

- **对话代理（Conversation agent）**：把自然语言指令映射到 Home Assistant 服务调用
- **自定义 function tools**：YAML 定义 `native` / `sqlite` / `template` / `script` / `rest` / `scrape` / `bash` / `file` / `composite` 等函数
- **设备别名映射**：`rebuild_device_map` 服务重建「设备名称 → 实体 ID」映射，降低 LLM 误调用
- **上下文管理**：`context_threshold` 配合截断策略（含滚动窗口）控制 token 用量
- **多端点**：OpenAI 官方、Azure OpenAI，以及任意 OpenAI 兼容 `base_url`
- **AI Task 平台**：为 Home Assistant 的 `ai_task` 提供结构化输出能力
- **完整本地化**：`strings.json` + `translations/`，含简体中文（`translations/zh.json`）

## 安装

### 方式一：HACS（推荐）

1. 确认已安装 [HACS](https://hacs.xyz/)
2. 在 HACS → 集成 → 右上角 ⋮ → **自定义仓库** 中添加：

   ```
   https://github.com/xyzmos/hass_ai_conversation
   ```

   类型选择 **Integration**。也可以直接点击下面的按钮跳转：

   [![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=xyzmos&repository=hass_ai_conversation&category=integration)

3. 搜索 **AI Conversation** 并下载
4. **重启 Home Assistant**
5. 设置 → 设备与服务 → 添加集成 → **AI Conversation**

> HACS 依据 GitHub Release 判断版本，本仓库每次发布都会创建对应的 Release；仅有 tag 而无 Release 时 HACS 只会提供默认分支。

### 方式二：手动安装

把本仓库 `custom_components/hass_ai_conversation/` 整个目录复制到 HA 配置目录下：

```bash
git clone https://github.com/xyzmos/hass_ai_conversation.git
cp -r hass_ai_conversation/custom_components/hass_ai_conversation \
      /path/to/homeassistant/config/custom_components/
```

重启 Home Assistant 后添加集成。

> 集成目录名必须为 `hass_ai_conversation`（与 `manifest.json` 的 `domain` 一致），否则无法加载。
> 注意复制的是 `custom_components/hass_ai_conversation/`（含 `brand/`、`translations/`、`functions/`、`icons.json`、`manifest.json` 等），而不是仓库根目录。

## 从 extended_openai_conversation 升级（重要）

由于 `domain` 已变更，Home Assistant 会将本集成视为全新集成，**存量配置条目不会自动迁移**。升级步骤：

1. 在 HA 中删除旧的 "Extended OpenAI Conversation" 集成条目（设置 → 设备与服务）
2. 删除旧的 `custom_components/extended_openai_conversation/` 目录
3. 安装本集成到 `custom_components/hass_ai_conversation/`
4. 重新添加集成并重新填写 API key 等配置

### 需要手动迁移的内容

- **工作目录**：默认工作目录由 `extended_openai_conversation/` 改为 `hass_ai_conversation/`。若 bash/file 工具曾使用旧目录存放文件，请将 `<config_dir>/extended_openai_conversation/` 重命名为 `hass_ai_conversation/`，否则新集成无法访问旧文件。
- **Prompt 模板**：模板全局变量名由 `extended_openai` 改为 `ai_conversation`。若你自定义过 prompt 模板，请将其中所有 `extended_openai.exposed_entities()`、`extended_openai.working_directory()` 替换为 `ai_conversation.exposed_entities()`、`ai_conversation.working_directory()`。默认 prompt 已同步更新，仅自定义过 prompt 的用户受影响。

## 配置

通过 UI 配置流完成配置：API key、模型、提示词、自定义函数、上下文阈值与截断策略等。高级选项含温度、top_p、reasoning_effort、service_tier、shorten_tool_call_id（Mistral 兼容）。

详细字段说明见集成配置界面与 `strings.json`。

## 服务

| 服务 | 说明 |
|------|------|
| `hass_ai_conversation.query_image` | 接收图像并回答相关问题（消耗 API 额度） |
| `hass_ai_conversation.change_config` | 修改集成配置（仅管理员） |
| `hass_ai_conversation.rebuild_device_map` | 重建设备别名映射表 |

> 服务的名称、描述与字段说明统一维护在 `strings.json` / `translations/*.json`，`services.yaml` 只保留字段与选择器定义（Home Assistant 官方约定）。

## 仓库结构

本仓库遵循 HACS 集成仓库规范（`ROOT/custom_components/<domain>/`）：

```
hass_ai_conversation/
├── custom_components/
│   └── hass_ai_conversation/          # 集成本体，HACS 只分发此目录
│       ├── brand/                     # 本地品牌图标（HA 2026.3+）
│       │   ├── icon.png               # 256×256
│       │   └── icon@2x.png            # 512×512
│       ├── functions/                 # 各类 function tool 实现
│       ├── translations/              # 各语言翻译（en/zh/…）
│       ├── __init__.py
│       ├── ai_task.py
│       ├── config_flow.py
│       ├── const.py
│       ├── conversation.py
│       ├── device_map.py
│       ├── entity.py
│       ├── exceptions.py
│       ├── helpers.py
│       ├── icons.json                 # 服务图标
│       ├── manifest.json              # domain 必须与目录名一致
│       ├── services.py
│       ├── services.yaml              # 仅字段/选择器定义
│       ├── strings.json               # 翻译源文件（内容与 en.json 一致）
│       └── template.py
├── .github/workflows/
│   ├── validate.yml                   # hassfest + HACS Action 校验
│   └── release.yml                    # tag 推送时创建 Release
├── .gitignore
├── CHANGELOG.md
├── LICENSE
├── README.md
└── hacs.json                          # HACS 仓库元数据（name/homeassistant/render_readme）
```

## 开发与发布

### 校验

`.github/workflows/validate.yml` 在 push、pull request、每日定时任务中运行：

- [hassfest](https://github.com/home-assistant/actions#hassfest)：Home Assistant 官方集成校验（manifest、services、translations 等）
- [HACS Action](https://github.com/hacs/action)：HACS 仓库规范校验（`category: integration`）

本地快速自检：

```bash
python -m json.tool custom_components/hass_ai_conversation/manifest.json > /dev/null
python -m json.tool custom_components/hass_ai_conversation/strings.json > /dev/null
python -m compileall -q custom_components/hass_ai_conversation
```

### 发布新版本

1. 更新 `custom_components/hass_ai_conversation/manifest.json` 中的 `version`（例如 `3.2.0`）
2. 更新 `CHANGELOG.md`
3. 提交并推送 tag：

   ```bash
   git tag v3.2.0
   git push origin v3.2.0
   ```

4. `Release` 工作流会校验 tag 与 manifest 版本一致，并自动创建 GitHub Release

### 提交信息约定

推荐使用 Conventional Commits，例如：

```
fix(i18n): 修正异常文案中的单引号占位符
```

## 致谢

本项目基于 [@jekalmin](https://github.com/jekalmin) 的开源项目 [extended_openai_conversation](https://github.com/jekalmin/extended_openai_conversation) 修改而来，感谢原作者的贡献。

本 fork 相对原项目的主要变更：

- **依赖修复**：放宽 openai 约束为 `>=2.21.0,<3.0.0`，解决与 HA 核心锁定 `openai==2.45.0` 的依赖解析冲突
- **改名**：domain 由 `extended_openai_conversation` 改为 `hass_ai_conversation`
- **规范合规**：`AuthenticationError` 改用 `ConfigEntryAuthFailed` 触发 reauth；异常类文案接入 i18n translation 体系；`iot_class` 修正为 `cloud_push`；日志改为英文
- **HACS 规范化**：仓库改为 `custom_components/<domain>/` 标准布局，补充 `hacs.json`、品牌图标、CI 校验与发布流程

## 许可

[MIT](LICENSE)。原始项目为 [extended_openai_conversation](https://github.com/jekalmin/extended_openai_conversation)，版权归原作者所有。
