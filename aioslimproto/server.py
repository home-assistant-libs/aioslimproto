"""Server implementation holding (and discovering) the SLIMProto players."""
from __future__ import annotations

import asyncio
import logging
from types import TracebackType
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union

from .client import SlimClient
from .const import EventType, SlimEvent
from .discovery import start_discovery
from .json_rpc import SlimJSONRPC

EventCallBackType = Callable[[SlimEvent], None]
EventSubscriptionType = Tuple[EventCallBackType, Tuple[EventType], Tuple[str]]


class SlimServer:
    """Server holding the SLIMproto players."""

    def __init__(self, port: int = 3483, json_port: Optional[int] = 3484) -> None:
        """
        Initialize SlimServer instance.

        control_port: The TCP port for the slimproto communication, default is 3483.
        json_port: Optionally start a simple json rpc server on this port for compatability
        with players relying on the server providing this feature. None to disable.
        """
        self.logger = logging.getLogger(__name__)
        self.port = port
        self.json_port = json_port
        self._subscribers: List[EventSubscriptionType] = []
        self._socket_servers: List[Union[asyncio.Server, asyncio.BaseTransport]] = []
        self._players: Dict[str, SlimClient] = {}
        self._jsonrpc: Optional[SlimJSONRPC] = None
        if json_port is not None:
            self._jsonrpc = SlimJSONRPC(self, json_port)

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
        self._socket_servers = [
            # start slimproto server
            await asyncio.start_server(self._create_client, "0.0.0.0", self.port),
            # setup discovery
            await start_discovery(self.port, self.json_port),
        ]
        if self._jsonrpc is not None:
            self._socket_servers.append(await self._jsonrpc.start())

    async def stop(self):
        """Stop running the server."""
        for client in list(self._players.values()):
            client.disconnect()
        self._players = {}
        for _server in self._socket_servers:
            _server.close()

    def signal_event(self, event: SlimEvent) -> None:
        """
        Signal event to all subscribers.

            :param event_msg: the eventmessage to signal
            :param event_details: optional details to send with the event.
        """
        for cb_func, event_filter, player_filter in self._subscribers:
            if (
                event.player_id
                and player_filter
                and event.player_id not in player_filter
            ):
                continue
            if event_filter and event.type not in event_filter:
                continue
            if asyncio.iscoroutinefunction(cb_func):
                asyncio.create_task(cb_func(event))
            else:
                asyncio.get_running_loop().call_soon_threadsafe(cb_func, event)

    def subscribe(
        self,
        cb_func: EventCallBackType,
        event_filter: Union[EventType, Tuple[EventType], None] = None,
        player_filter: Union[str, Tuple[str], None] = None,
    ) -> Callable:
        """
        Subscribe to events from Slimclients.

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

        def client_callback(
            event_type: EventType, client: SlimClient, data: Any = None
        ):
            player_id = client.player_id

            if event_type == EventType.PLAYER_DISCONNECTED:
                self._players.pop(player_id, None)

            if event_type == EventType.PLAYER_CONNECTED:
                assert player_id not in self._players
                self._players[player_id] = client

            # forward all other events as-is
            self.signal_event(SlimEvent(event_type, player_id, data))

        SlimClient(reader, writer, client_callback)

    async def __aenter__(self) -> "SlimServer":
        """Return Context manager."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: Type[BaseException],
        exc_val: BaseException,
        exc_tb: TracebackType,
    ) -> Optional[bool]:
        """Exit context manager."""
        await self.stop()
        if exc_val:
            raise exc_val
        return exc_type
