"""Server implementation holding (and discovering) the SLIMProto players."""
from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Callable, Dict, List, Tuple

from .client import SlimClient
from .const import EventType
from .discovery import start_discovery

EventCallBackType = Callable[[EventType, SlimClient], None]
EventSubscriptionType = Tuple[EventCallBackType, Tuple[EventType], Tuple[str]]


class SlimServer:
    """Server holding the SLIMproto players."""

    def __init__(self, port: int = 3483, json_port: int | None = None) -> None:
        """
        Initialize SlimServer instance.

        control_port: The TCP port for the slimproto communication, default is 3483.
        json_port: Optionally start a simple json rpc server on this port for compatability
        with players relying on the server providing this feature.
        """
        self.logger = logging.getLogger(__name__)
        self.port = port
        self.json_port = json_port
        self._subscribers: List[EventSubscriptionType] = []
        self._bgtasks: List[asyncio.Task] = []
        self._players: Dict[str, SlimClient] = {}

    @property
    def players(self) -> List[SlimClient]:
        """Return all registered players."""
        return list(self._players.values())

    def get_player(self, player_id: str) -> SlimClient | None:
        """Return player by id."""
        return self._players.get(player_id)

    async def start(self):
        """Start running the server."""
        self.logger.info("Starting SLIMProto server on port %s", self.port)
        self._bgtasks = [
            # start slimproto server
            asyncio.create_task(
                asyncio.start_server(self._create_client, "0.0.0.0", self.port)
            ),
            # setup discovery
            asyncio.create_task(start_discovery(self.port, self.json_port)),
        ]

    async def stop(self):
        """Stop runninbg the server."""
        for task in self._bgtasks:
            task.cancel()

    def signal_event(
        self, event_type: EventType, player: SlimClient | None = None
    ) -> None:
        """
        Signal event to all subscribers.

            :param event_msg: the eventmessage to signal
            :param event_details: optional details to send with the event.
        """
        for cb_func, event_filter, player_filter in self._subscribers:
            if player and player_filter and player.id not in player_filter:
                continue
            if event_filter and event_type not in event_filter:
                continue
            if asyncio.iscoroutinefunction(cb_func):
                asyncio.create_task(cb_func(event_type, player))
            else:
                asyncio.get_running_loop().call_soon_threadsafe(
                    cb_func, event_type, player
                )

    def subscribe(
        self,
        cb_func: EventCallBackType,
        event_filter: EventType | Tuple[EventType] | None = None,
        player_filter: str | Tuple(str) | None = None,
    ) -> Callable:
        """
        Add callback to event listeners.

        Returns function to remove the listener.
            :param cb_func: callback function or coroutine
            :param event_filter: Optionally only listen for this event(s).
            :param event_filter: Optionally only listen for this player(s).
        """
        if isinstance(event_filter, EventType):
            event_filter = (event_filter,)
        elif event_filter is None:
            event_filter = tuple()
        if isinstance(player_filter, str):
            player_filter = (player_filter,)
        elif player_filter is None:
            player_filter = tuple()

        listener = (cb_func, event_filter, player_filter)
        self._subscribers.append(listener)

        def remove_listener():
            self._subscribers.remove(listener)

        return remove_listener

    async def _create_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Create player from new connection on the socket."""
        addr = writer.get_extra_info("peername")
        self.logger.debug("Socket client connected: %s", addr)
        SlimClient(reader, writer, self._client_callback)

    def _client_callback(self, event: EventType, client: SlimClient):
        """Handle event/callback from SlimClient."""
        player_id = client.player_id
        if not player_id:
            return

        if event == EventType.PLAYER_DISCONNECTED:
            # player disconnected, cleanup and emit event
            player = self._players.pop(player_id)
            player.disconnect()
            self.signal_event(EventType.PLAYER_REMOVED, player)
            return

        if event == EventType.PLAYER_CONNECTED:
            # new player (or reconnected)
            assert player_id not in self._players
            self._players[player_id] = client
            self.signal_event(EventType.PLAYER_ADDED, client)
            return

        # forward all other events
        self.signal_event(event, client)
