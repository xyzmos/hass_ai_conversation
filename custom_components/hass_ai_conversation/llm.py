"""LLM tools platform for the hass_ai_conversation integration.

Exposes Music Assistant tools to the Assist LLM API. The hook
``async_get_tools`` is *synchronous* (called without await by
``homeassistant.components.llm.async_get_tools``); every body guard is cheap
and synchronous.

Design notes:

* This module NEVER imports ``homeassistant.components.music_assistant`` —
  that component drags in the ``music_assistant_client`` pip package which is
  only installed when Music Assistant is configured. Importing it here would
  break LLM tool loading for every integration on systems without Music
  Assistant. We only touch the ``"music_assistant"`` domain string.
* We return ``None`` unless this is the Assist API *and* the conversation is
  running through a voice/text assistant (``llm_context.assistant``). The
  ``ai_task`` path passes ``assistant=None`` — we must not inject music tools
  into generic AI-task calls.
* We return ``None`` unless Music Assistant is set up with at least one
  ``media_player`` entity exposed to this assistant.
"""

from __future__ import annotations

import logging

from homeassistant.components import llm as llm_comp
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, llm

from .music import MEDIA_PLAYER_DOMAIN, MUSIC_ASSISTANT_DOMAIN
from .music.tools import MUSIC_PROMPT, build_tools

_LOGGER = logging.getLogger(__name__)


def _has_ma_player(hass: HomeAssistant) -> bool:
    """True if a loaded Music Assistant entry owns ≥1 media_player."""
    if not any(
        e.state == ConfigEntryState.LOADED
        for e in hass.config_entries.async_entries(MUSIC_ASSISTANT_DOMAIN)
    ):
        return False
    entity_reg = er.async_get(hass)
    return any(
        e.platform == MUSIC_ASSISTANT_DOMAIN
        and e.domain == MEDIA_PLAYER_DOMAIN
        and e.original_device_class == "speaker"
        for e in entity_reg.entities.values()
    )


@callback
def async_get_tools(
    hass: HomeAssistant, llm_context: llm.LLMContext, api_id: str
) -> llm_comp.LLMTools | None:
    """Return Music Assistant tools for the Assist API, else None."""
    if api_id != llm.LLM_API_ASSIST:
        return None
    if not llm_context.assistant:
        # ai_task / generic API calls have no assistant context — do not
        # inject music tools there.
        return None
    try:
        if not _has_ma_player(hass):
            return None
        tools = build_tools(hass)
    except Exception:  # noqa: BLE001 - never break other platforms' tools
        _LOGGER.exception("Failed to build Music Assistant LLM tools")
        return None
    return llm_comp.LLMTools(tools=tools, prompt=MUSIC_PROMPT)
