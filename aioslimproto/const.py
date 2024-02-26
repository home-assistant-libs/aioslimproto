"""Contsnats for aioslimproto."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class EventType(Enum):
    """Enum with possible slim proto client events."""

    PLAYER_UPDATED = "player_updated"
    PLAYER_HEARTBEAT = "player_heartbeat"
    PLAYER_CONNECTED = "player_connected"
    PLAYER_DISCONNECTED = "player_disconnected"
    PLAYER_NAME_RECEIVED = "player_name_received"
    PLAYER_DECODER_READY = "player_decoder_ready"
    PLAYER_DECODER_ERROR = "player_decoder_error"
    PLAYER_OUTPUT_UNDERRUN = "player_output_underrun"
    PLAYER_BUFFER_READY = "player_buffer_ready"
    PLAYER_CLI_EVENT = "player_cli_event"
    PLAYER_BTN_EVENT = "player_btn_event"


@dataclass
class SlimEvent:
    """Representation of an Event emitted by/on behalf of a player."""

    type: EventType
    player_id: str
    data: dict[str, Any] | None = None


SLIMPROTO_PORT = 3483
FALLBACK_CODECS = ["pcm", "mp3"]
