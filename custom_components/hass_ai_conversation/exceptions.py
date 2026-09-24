"""The exceptions used by AI Conversation."""

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN


class EntityNotFound(HomeAssistantError):
    """When referenced entity not found."""

    def __init__(self, entity_id: str) -> None:
        """Initialize error."""
        super().__init__(
            translation_domain=DOMAIN,
            translation_key="entity_not_found",
            translation_placeholders={"entity_id": entity_id},
        )
        self.entity_id = entity_id


class EntityNotExposed(HomeAssistantError):
    """When referenced entity not exposed."""

    def __init__(self, entity_id: str) -> None:
        """Initialize error."""
        super().__init__(
            translation_domain=DOMAIN,
            translation_key="entity_not_exposed",
            translation_placeholders={"entity_id": entity_id},
        )
        self.entity_id = entity_id


class CallServiceError(HomeAssistantError):
    """Error during service calling."""

    def __init__(self, domain: str, service: str, data: object) -> None:
        """Initialize error."""
        super().__init__(
            translation_domain=DOMAIN,
            translation_key="call_service_error",
            translation_placeholders={"domain": domain, "service": service},
        )
        self.domain = domain
        self.service = service
        self.data = data


class FunctionNotFound(HomeAssistantError):
    """When referenced function not found."""

    def __init__(self, function: str) -> None:
        """Initialize error."""
        super().__init__(
            translation_domain=DOMAIN,
            translation_key="function_not_found",
            translation_placeholders={"function": function},
        )
        self.function = function


class NativeNotFound(HomeAssistantError):
    """When native function not found."""

    def __init__(self, name: str) -> None:
        """Initialize error."""
        super().__init__(
            translation_domain=DOMAIN,
            translation_key="native_not_found",
            translation_placeholders={"name": name},
        )
        self.name = name


class FunctionLoadFailed(HomeAssistantError):
    """When function load failed."""

    def __init__(self) -> None:
        """Initialize error."""
        super().__init__(
            translation_domain=DOMAIN,
            translation_key="function_load_failed",
        )


class ParseArgumentsFailed(HomeAssistantError):
    """When parse arguments failed."""

    def __init__(self, arguments: str) -> None:
        """Initialize error."""
        super().__init__(
            translation_domain=DOMAIN,
            translation_key="parse_arguments_failed",
            translation_placeholders={"arguments": arguments},
        )
        self.arguments = arguments


class TokenLengthExceededError(HomeAssistantError):
    """When openai return 'length' as 'finish_reason'."""

    def __init__(self, token: int) -> None:
        """Initialize error."""
        super().__init__(
            translation_domain=DOMAIN,
            translation_key="token_length_exceeded",
            translation_placeholders={"token": str(token)},
        )
        self.token = token


class ContentFilterError(HomeAssistantError):
    """When openai return 'content_filter' as 'finish_reason'."""

    def __init__(self) -> None:
        """Initialize error."""
        super().__init__(
            translation_domain=DOMAIN,
            translation_key="content_filter",
        )


class InvalidFunction(HomeAssistantError):
    """When function validation failed."""

    def __init__(self, function_name: str) -> None:
        """Initialize error."""
        super().__init__(
            translation_domain=DOMAIN,
            translation_key="invalid_function",
            translation_placeholders={"function_name": function_name},
        )
        self.function_name = function_name
