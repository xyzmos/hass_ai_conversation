"""Services for the extended openai conversation component."""

import base64
import logging
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

from openai._exceptions import OpenAIError
import voluptuous as vol

from homeassistant.const import CONF_API_KEY
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, selector
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_API_PROVIDER,
    CONF_API_VERSION,
    CONF_BASE_URL,
    CONF_ORGANIZATION,
    CONF_SKIP_AUTHENTICATION,
    DEFAULT_CHAT_MODEL,
    DEFAULT_CONF_BASE_URL,
    DOMAIN,
    SERVICE_QUERY_IMAGE,
)
from .helpers import get_authenticated_client, get_token_param_for_model
from .device_map import async_get_device_map

QUERY_IMAGE_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry"): selector.ConfigEntrySelector(
            {
                "integration": DOMAIN,
            }
        ),
        vol.Required("model", default=DEFAULT_CHAT_MODEL): cv.string,
        vol.Required("prompt"): cv.string,
        vol.Required("images"): vol.All(cv.ensure_list, [{"url": cv.string}]),
        vol.Optional("max_tokens", default=300): cv.positive_int,
    }
)

CHANGE_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required("config_entry"): selector.ConfigEntrySelector(
            {
                "integration": DOMAIN,
            }
        ),
        vol.Optional(CONF_API_KEY): cv.string,
        vol.Optional(CONF_BASE_URL): cv.string,
        vol.Optional(CONF_API_VERSION): cv.string,
        vol.Optional(CONF_ORGANIZATION): cv.string,
        vol.Optional(CONF_SKIP_AUTHENTICATION): cv.boolean,
        vol.Optional(CONF_API_PROVIDER): cv.string,
    }
)

REBUILD_DEVICE_MAP_SCHEMA = vol.Schema({})

_LOGGER = logging.getLogger(__package__)


async def async_setup_services(hass: HomeAssistant, config: ConfigType) -> None:
    """Set up services for the extended openai conversation component."""

    async def query_image(call: ServiceCall) -> ServiceResponse:
        """Query an image."""
        try:
            model = call.data["model"]
            images = [
                {"type": "image_url", "image_url": await async_to_image_param(hass, image)}
                for image in call.data["images"]
            ]

            messages = [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": call.data["prompt"]}, *images],
                }
            ]
            _LOGGER.debug("Querying image with model %s, %d images", model, len(images))

            entry = hass.config_entries.async_get_entry(call.data["config_entry"])
            if entry is None:
                raise HomeAssistantError("Config entry not found")

            client = entry.runtime_data

            token_param = get_token_param_for_model(model)
            token_kwargs = {token_param: call.data["max_tokens"]}

            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                **token_kwargs,
            )
            response_dict: dict = response.model_dump()
            _LOGGER.debug("Image query response received")
        except OpenAIError as err:
            raise HomeAssistantError(f"Error generating image: {err}") from err

        return response_dict

    async def change_config(call: ServiceCall) -> None:
        """Change configuration.

        Restricted to admin users because it can redirect API traffic to an
        arbitrary base_url (potential exfiltration of conversation content)
        and rotate API keys.
        """
        user = call.user
        if user is None or not user.is_admin:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="admin_only_change_config",
            )

        entry_id = call.data["config_entry"]
        entry = hass.config_entries.async_get_entry(entry_id)
        if not entry or entry.domain != DOMAIN:
            raise HomeAssistantError(f"Config entry {entry_id} not found")

        updates = {}
        for key in (
            CONF_API_KEY,
            CONF_BASE_URL,
            CONF_API_VERSION,
            CONF_ORGANIZATION,
            CONF_SKIP_AUTHENTICATION,
            CONF_API_PROVIDER,
        ):
            if key in call.data:
                updates[key] = call.data[key]

        if not updates:
            return

        new_data = entry.data.copy()
        new_data.update(updates)

        _LOGGER.debug("Updating config entry %s with %s", entry_id, new_data)

        base_url = new_data.get(CONF_BASE_URL)
        if base_url == DEFAULT_CONF_BASE_URL:
            base_url = None
            new_data.pop(CONF_BASE_URL)

        if new_data.get(CONF_API_PROVIDER) == "azure" and not base_url:
            raise HomeAssistantError("Azure OpenAI requires a custom base URL.")

        await get_authenticated_client(
            hass=hass,
            api_key=new_data[CONF_API_KEY],
            base_url=new_data.get(CONF_BASE_URL),
            api_version=new_data.get(CONF_API_VERSION),
            organization=new_data.get(CONF_ORGANIZATION),
            skip_authentication=new_data.get(CONF_SKIP_AUTHENTICATION, False),
            api_provider=new_data.get(CONF_API_PROVIDER),
        )

        hass.config_entries.async_update_entry(entry, data=new_data)

    async def rebuild_device_map(call: ServiceCall) -> ServiceResponse:
        """Rebuild the device alias mapping."""
        device_map = async_get_device_map(hass)
        device_map.async_build_map()

        return {
            "total_entities": len(device_map.get_all_entities()),
            "total_aliases": device_map.get_total_aliases(),
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_QUERY_IMAGE,
        query_image,
        schema=QUERY_IMAGE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        "change_config",
        change_config,
        schema=CHANGE_CONFIG_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        "rebuild_device_map",
        rebuild_device_map,
        schema=REBUILD_DEVICE_MAP_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


async def async_to_image_param(hass: HomeAssistant, image: dict) -> dict:
    """Convert url to base64 encoded image if local (async version)."""
    url = image["url"]

    if urlparse(url).scheme in cv.EXTERNAL_URL_PROTOCOL_SCHEMA_LIST:
        return image

    # Validate + read the file in the executor to avoid blocking the loop.
    encoded, error = await hass.async_add_executor_job(_read_image_file, hass, url)
    if error is not None:
        raise error
    mime_type, _ = mimetypes.guess_type(url)
    if mime_type is None or not mime_type.startswith("image"):
        raise HomeAssistantError(f"`{url}` is not an image")
    image["url"] = f"data:{mime_type};base64,{encoded}"
    return image


def _read_image_file(hass: HomeAssistant, url: str) -> tuple[str | None, HomeAssistantError | None]:
    """Synchronously validate access and read+encode a local image file."""
    if not hass.config.is_allowed_path(url):
        return None, HomeAssistantError(
            f"Cannot read `{url}`, no access to path; "
            "`allowlist_external_dirs` may need to be adjusted in "
            "`configuration.yaml`"
        )
    path = Path(url)
    if not path.exists():
        return None, HomeAssistantError(f"`{url}` does not exist")
    try:
        with open(path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode("utf-8"), None
    except OSError as err:
        return None, HomeAssistantError(f"Failed to read `{url}`: {err}")
