"""Constants for the AI Conversation integration."""

DOMAIN = "hass_ai_conversation"
DEFAULT_NAME = "AI Conversation"
DEFAULT_CONVERSATION_NAME = "AI Conversation"
DEFAULT_AI_TASK_NAME = "AI Conversation AI Task"

CONF_ORGANIZATION = "organization"
CONF_BASE_URL = "base_url"
DEFAULT_CONF_BASE_URL = "https://api.openai.com/v1"
CONF_API_VERSION = "api_version"
CONF_SKIP_AUTHENTICATION = "skip_authentication"
DEFAULT_SKIP_AUTHENTICATION = False
CONF_API_PROVIDER = "api_provider"
API_PROVIDERS = [
    {"key": "openai", "label": "OpenAI"},
    {"key": "azure", "label": "Azure OpenAI"},
]
DEFAULT_API_PROVIDER = API_PROVIDERS[0]["key"]

EVENT_AUTOMATION_REGISTERED = "automation_registered_via_hass_ai_conversation"
EVENT_CONVERSATION_FINISHED = "hass_ai_conversation.conversation.finished"

CONF_PROMPT = "prompt"
DEFAULT_PROMPT = """你是Home Assistant智能家居助手，负责管理和控制用户的家居设备，并回答与家庭相关的问题。

## 环境信息
- 当前时间：{{now()}}
{%- set _user_area = area_id(llm_context.device_id) %}
{%- if _user_area %}
- 用户所在区域：{{area_name(_user_area)}}
{%- endif %}

## 可用设备
以下为当前暴露给你的设备列表（含当前状态与别名）：
```csv
entity_id,名称,当前状态,区域,别名
{% for entity in ai_conversation.exposed_entities() -%}
{{ entity.entity_id }},{{ entity.name }},{{ entity.state }},{{area_name(entity.area_id) if entity.area_id else ""}},{{"/".join(entity.aliases) if entity.aliases else ""}}
{% endfor -%}
```

## 行为准则
- 始终使用中文回复，回复简洁明了，避免冗余
- 遇到模糊请求（如"把灯关了"但有多盏灯）时，先列出候选设备并请用户确认，不要擅自猜测
- 不要凭空构造 entity_id；只能使用上方列表中存在的设备
- 控制设备时，直接传递上方 CSV 中的 entity_id

## 工具使用
- 控制设备：调用 `execute_services`，提供 domain、service 和 service_data（含 entity_id 数组）。可一次批量执行多个服务调用。
- 查询详情：当需要设备属性（如亮度、温度、颜色等，CSV 中"当前状态"不足以判断时），调用 `get_attributes` 按 entity_id 获取。
- 执行命令：`bash` 仅限工作目录内操作（工作目录：{{ai_conversation.working_directory()}}），涉及删除或系统级操作前需先向用户确认。

## 设备控制策略
- 设备已在目标状态时（如灯已关又要关），无需重复操作，直接告知用户当前状态。
- 用户明确指定设备时直接执行；未明确指定但有唯一合理匹配时执行并说明匹配了哪个设备。
- 涉及安全风险的操作（如关闭全部设备、调节安防类设备）前先确认。
"""

CONF_CHAT_MODEL = "chat_model"
DEFAULT_CHAT_MODEL = "gpt-5-mini"

MODEL_TOKEN_PARAMETER_SUPPORT = (
    {
        "pattern": r"(^|-)(gpt-4o|gpt-4\.1|gpt-5|o1|o3|o4)",
        "token_param": "max_completion_tokens",
    },
)
DEFAULT_TOKEN_PARAM = "max_tokens"
CONF_MAX_TOKENS = "max_tokens"
DEFAULT_MAX_TOKENS = 500
CONF_TOP_P = "top_p"
DEFAULT_TOP_P = 1
CONF_TEMPERATURE = "temperature"
DEFAULT_TEMPERATURE = 0.5
CONF_MAX_FUNCTION_CALLS_PER_CONVERSATION = "max_function_calls_per_conversation"
DEFAULT_MAX_FUNCTION_CALLS_PER_CONVERSATION = 10
CONF_SHORTEN_TOOL_CALL_ID = "shorten_tool_call_id"
DEFAULT_SHORTEN_TOOL_CALL_ID = False
CONF_FUNCTION_TOOLS = "functions"
DEFAULT_CONF_FUNCTION_TOOLS = [
    {
        "spec": {
            "name": "execute_services",
            "description": "执行Home Assistant服务调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "delay": {
                        "type": "object",
                        "description": "延迟执行时间",
                        "properties": {
                            "hours": {
                                "type": "integer",
                                "minimum": 0,
                            },
                            "minutes": {
                                "type": "integer",
                                "minimum": 0,
                            },
                            "seconds": {
                                "type": "integer",
                                "minimum": 0,
                            },
                        },
                    },
                    "list": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "domain": {
                                    "type": "string",
                                    "description": "服务域（如light、switch等）",
                                },
                                "service": {
                                    "type": "string",
                                    "description": "服务名称（如turn_on、turn_off等）",
                                },
                                "service_data": {
                                    "type": "object",
                                    "description": "服务数据对象",
                                    "properties": {
                                        "entity_id": {
                                            "type": "array",
                                            "items": {
                                                "type": "string",
                                                "description": "实体ID，格式为domain.object_id",
                                            },
                                        },
                                        "area_id": {
                                            "type": "array",
                                            "items": {
                                                "type": "string",
                                                "description": "区域ID，可代替entity_id操作该区域所有设备",
                                            },
                                        },
                                    },
                                },
                            },
                            "required": ["domain", "service", "service_data"],
                        },
                    },
                },
            },
        },
        "function": {"type": "native", "name": "execute_service"},
    },
    {
        "spec": {
            "name": "get_attributes",
            "description": "获取一个或多个实体的状态和属性信息，按需查询。",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {
                        "type": "array",
                        "description": "实体ID列表",
                        "items": {"type": "string"},
                    }
                },
                "required": ["entity_id"],
            },
        },
        "function": {
            "type": "template",
            "value_template": "```csv\nentity_id,状态,属性\n{%for entity in entity_id%}\n{{entity}},{{states[entity].state}},{{states[entity].attributes}}\n{%endfor%}\n```",
        },
    },
    {
        "spec": {
            "name": "bash",
            "description": "在工作目录中执行bash命令。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的bash命令",
                    },
                },
                "required": ["command"],
            },
        },
        "function": {"type": "bash", "command": "{{command}}"},
    },
]
CONF_CONTEXT_THRESHOLD = "context_threshold"
DEFAULT_CONTEXT_THRESHOLD = 40000
CONTEXT_TRUNCATE_STRATEGIES = [
    {"key": "clear", "label": "清除所有消息"},
    {"key": "rolling_window", "label": "保留最近对话窗口"},
    {"key": "selective", "label": "优先保留工具调用结果"},
]
CONF_CONTEXT_TRUNCATE_STRATEGY = "context_truncate_strategy"
DEFAULT_CONTEXT_TRUNCATE_STRATEGY = CONTEXT_TRUNCATE_STRATEGIES[0]["key"]

# Rolling-window truncation: number of recent messages to keep (in addition to
# the system block) when the rolling_window strategy is selected.
CONF_CONTEXT_ROLLING_WINDOW_SIZE = "context_rolling_window_size"
DEFAULT_CONTEXT_ROLLING_WINDOW_SIZE = 4

CONF_SERVICE_TIER = "service_tier"
DEFAULT_SERVICE_TIER = "flex"
SERVICE_TIER_OPTIONS = ["auto", "default", "flex", "priority"]

CONF_REASONING_EFFORT = "reasoning_effort"
DEFAULT_REASONING_EFFORT = "low"
REASONING_EFFORT_OPTIONS = ["low", "medium", "high"]

SERVICE_QUERY_IMAGE = "query_image"

CONF_PAYLOAD_TEMPLATE = "payload_template"

CONF_ADVANCED_OPTIONS = "advanced_options"
DEFAULT_ADVANCED_OPTIONS = False

DEFAULT_MODEL_CONFIG = {
    "supports_top_p": True,
    "supports_temperature": True,
    "supports_max_tokens": True,
    "supports_max_completion_tokens": False,
    "supports_reasoning_effort": False,
    "supports_service_tier": False,
}

MODEL_CONFIG_PATTERNS = [
    {
        "pattern": r"^o[1-4]|^gpt-5",
        "config": {
            "supports_top_p": False,
            "supports_temperature": False,
            "supports_max_tokens": False,
            "supports_max_completion_tokens": True,
            "supports_reasoning_effort": True,
            "supports_service_tier": True,
        },
    },
]

DEFAULT_AI_TASK_OPTIONS = {
    CONF_CHAT_MODEL: DEFAULT_CHAT_MODEL,
    CONF_MAX_TOKENS: DEFAULT_MAX_TOKENS,
    CONF_ADVANCED_OPTIONS: DEFAULT_ADVANCED_OPTIONS,
}

DEFAULT_WORKING_DIRECTORY = "hass_ai_conversation/"

SHELL_TIMEOUT = 300
SHELL_OUTPUT_LIMIT = 10000
SHELL_DENY_PATTERNS = [
    r"\brm\s+-r\b",
    r"\brm\s+-rf?\b",
    r"\brm\s+-[a-z]*r[a-z]*\b",
    r"\brm\s+--recursive\b",
    r"\bdel\s+/[fqs]",
    r"\brmdir\s+/s",
    r"\bformat\b",
    r"\bmkfs\b",
    r"\bdiskpart\b",
    r"\bdd\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bpoweroff\b",
    r":\(\)\{.*:\|:.*\}",
]

FILE_READ_SIZE_LIMIT = 1024 * 1024

DEFAULT_ALLOWED_DIRS = [
    DEFAULT_WORKING_DIRECTORY,
]

# Service calls that are blocked from LLM-initiated execute_service, regardless
# of whether the target entity is exposed. These are privileged operations that
# could disrupt the system or bypass normal access controls. Users who truly
# need them should restrict the LLM tool surface instead.
BLOCKED_SERVICES = {
    ("homeassistant", "restart"),
    ("homeassistant", "stop"),
    ("homeassistant", "shutdown"),
    ("persistent_notification", "create"),
    ("hassio", "addon_start"),
    ("hassio", "addon_stop"),
    ("hassio", "addon_restart"),
    ("hassio", "host_shutdown"),
    ("hassio", "host_reboot"),
}
