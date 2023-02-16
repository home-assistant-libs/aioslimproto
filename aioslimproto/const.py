"""Contsnats for aioslimproto."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class EventType(Enum):
    """Enum with possible slim proto server events."""

    PLAYER_UPDATED = "player_updated"
    PLAYER_TIME_UPDATED = "player_time_updated"
    PLAYER_CONNECTED = "player_connected"
    PLAYER_DISCONNECTED = "player_disconnected"
    PLAYER_NAME_RECEIVED = "player_name_received"
    PLAYER_DECODER_READY = "decoder_ready"
    PLAYER_DECODER_ERROR = "decoder_error"
    PLAYER_BUFFER_READY = "buffer_ready"
    PLAYER_CLI_EVENT = "player_cli_event"


@dataclass
class SlimEvent:
    """Representation of an Event emitted by/on behalf of a player."""

    type: EventType
    player_id: str
    data: Optional[Dict[str, Any]] = None


DEFAULT_SLIMPROTO_PORT = 3483
