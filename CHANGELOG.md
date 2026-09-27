# Changelog

## [Unreleased]

### Fixed

- **openai 上限约束冲突**: `requirements` 由 `openai>=2.21.0,<3.0.0` 改为 `openai>=2.21.0`。此前一旦 Home Assistant 核心锁定 openai 3.x（例如 `openai==3.10.0`），pip 解析会报 `Because you require openai>=2.21.0,<3.0.0 and openai==3.10.0 ... your requirements are unsatisfiable`。现在只限制最低版本，由 HA 核心决定实际安装版本。

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
