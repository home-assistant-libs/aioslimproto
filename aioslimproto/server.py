"""Server implementation holding (and discovering) the SLIMProto players."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from types import TracebackType
from typing import Any, Self

from .cli import SlimProtoCLI
from .client import SlimClient
from .const import SLIMPROTO_PORT
from .discovery import start_discovery
from .models import EventType, SlimEvent
from .util import get_hostname, get_ip

EventCallBackType = Callable[[SlimEvent], None]
EventSubscriptionType = tuple[EventCallBackType, tuple[EventType], tuple[str]]


class SlimServer:
    """Server holding the SLIMproto players."""

    def __init__(
        self,
        cli_port: int | None = 0,
        cli_port_json: int | None = 0,
        ip_address: str | None = None,
        name: str | None = None,
        control_port: int = SLIMPROTO_PORT,
    ) -> None:
        """
        Initialize SlimServer instance.

        Params:
        - cli_port: Optionally start a simple Telnet CLI server on this port for
           compatibility with players relying on the server providing this feature.
           None to disable, 0 for autoselect.
        - cli_port_json: Same as cli port but it's newer JSON RPC equivalent.
        - ip_address: IP to broadcast to clients to discover this server, None=autoselect.
        - name: Name to broadcast to clients to discover this server, None =autoselect.
        - control_port: The port to start the slimproto server on, default is 3483.
          Note that only software clients can actually handle a non default control port.
        """  # noqa: E501
        self.logger = logging.getLogger(__name__)
        self.ip_address = ip_address or get_ip()
        self.name = name or get_hostname()
        self.control_port = control_port
        self.cli = SlimProtoCLI(self, cli_port, cli_port_json)
        self._subscribers: list[EventSubscriptionType] = []
        self._server: asyncio.Server | None = None
        self._discovery: asyncio.BaseTransport | None = None
        self._players: dict[str, SlimClient] = {}

    @property
    def players(self) -> list[SlimClient]:
        """Return all registered players."""
        return list(self._players.values())

    def get_player(self, player_id: str) -> SlimClient | None:
        """Return player by id."""
        return self._players.get(player_id)

    async def start(self) -> None:
        """Start running the servers."""
        self.logger.info("Starting SLIMProto server on port %s", self.control_port)
        # start slimproto server
        self._server = await asyncio.start_server(
            self._create_client,
            "0.0.0.0",  # noqa: S104
            self.control_port,
        )
        # start cli
        await self.cli.start()
        # start discovery
        self._discovery = await start_discovery(
            self.ip_address,
            self.control_port,
            self.cli.cli_port,
            self.cli.cli_port_json,
            self.name,
        )

    async def stop(self) -> None:
        """Stop running the server."""
        for client in list(self._players.values()):
            client.disconnect()
        self._players = {}
        if self._server:
            self._server.close()
        if self._discovery:
            self._discovery.close()
        await self.cli.stop()

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
                asyncio.create_task(cb_func(event))  # noqa: RUF006
            else:
                asyncio.get_running_loop().call_soon_threadsafe(cb_func, event)

    def subscribe(
        self,
        cb_func: EventCallBackType,
        event_filter: EventType | tuple[EventType] | None = None,
        player_filter: str | tuple[str] | None = None,
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
            event_filter = ()
        if isinstance(player_filter, str):
            player_filter = (player_filter,)
        elif player_filter is None:
            player_filter = ()

        listener = (cb_func, event_filter, player_filter)
        self._subscribers.append(listener)

        def remove_listener() -> None:
            self._subscribers.remove(listener)

        return remove_listener

    async def _create_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Create player from new connection on the socket."""
        addr = writer.get_extra_info("peername")
        self.logger.debug("Socket client connected: %s", addr)

        def client_callback(
            client: SlimClient,
            event_type: EventType,
            data: Any = None,
        ) -> None:
            player_id = client.player_id

            if event_type == EventType.PLAYER_DISCONNECTED:
                prev = self._players.pop(player_id, None)
                if prev is None:
                    # already cleaned up ?
                    return

            if event_type == EventType.PLAYER_CONNECTED:
                prev = self._players.pop(player_id, None)
                if prev is not None:
                    # player reconnected while we did not yet cleanup the old socket
                    prev.disconnect()

                self._players[player_id] = client

            # forward all other events as-is
            self.signal_event(SlimEvent(event_type, player_id, data))

        SlimClient(reader, writer, client_callback)

    async def __aenter__(self) -> Self:
        """Return Context manager."""
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        """Exit context manager."""
        await self.stop()
        if exc_val:
            raise exc_val
        return exc_type
