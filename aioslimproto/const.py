"""Contsnats for aioslimproto."""
from __future__ import annotations

from enum import Enum


class EventType(Enum):
    """Enum with possible slim proto server events."""

    PLAYER_UPDATED = "player_updated"
    PLAYER_CONNECTED = "player_connected"
    PLAYER_DISCONNECTED = "player_disconnected"
    PLAYER_NAME_RECEIVED = "player_name_received"
    PLAYER_DECODER_READY = "decoder_ready"
    PLAYER_DECODER_ERROR = "decoder_error"
