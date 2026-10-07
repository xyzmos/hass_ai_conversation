"""Music Assistant (music-assistant.io) integration helpers for the LLM tool platform.

This subpackage deliberately does NOT import ``homeassistant.components.music_assistant``
(or anything that imports it). That component pulls in the ``music_assistant_client``
pip package, which is only installed when the Music Assistant integration is
configured. Importing it here would make the whole ``llm.py`` platform fail to
load on systems without Music Assistant. All Music Assistant access goes through
``hass.services.async_call`` and the entity registry, keyed by the literal
``"music_assistant"`` domain string.
"""

MUSIC_ASSISTANT_DOMAIN = "music_assistant"
MEDIA_PLAYER_DOMAIN = "media_player"
