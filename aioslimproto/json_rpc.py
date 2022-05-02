"""

Basic implementation of JSON RPC control of SlimProto players.

Some players (e.g. PiCorePlayer) use the jsonrpc api to control for example volume remotely.
This is a very basic implementation that only fulfills commands needed by those players,
other commands will be published as-is on the eventbus for library consumers to act on.
there's no support for media browsing through this minimal api, this is NOT a replacement for
the Logitech Media Server.

https://gist.github.com/samtherussell/335bf9ba75363bd167d2470b8689d9f2
"""
from __future__ import annotations

import asyncio
import json
from ctypes import Union
from dataclasses import dataclass
from typing import TYPE_CHECKING, List

from aioslimproto.const import EventType, SlimEvent

if TYPE_CHECKING:
    from .client import SlimClient
    from .server import SlimServer

CHUNK_SIZE = 50


@dataclass
class JSONRPCMessage:
    """Representation of a JSON RPC message."""

    id: Union[int]
    method: str
    params: List[Union[str, List[str]]]

    @property
    def player_id(self) -> str:
        """Return player ID targetting this request."""
        return self.params[0]

    @property
    def command(self) -> str:
        """Return the params for the target player."""
        return self.params[1][0]

    @property
    def command_args(self) -> List[str]:
        """Return the command arguments."""
        if len(self.params[1]) > 1:
            return self.params[1][1:]
        return []

    @property
    def command_str(self) -> str:
        """Return string representation of the command+args."""
        return " ".join([self.command] + self.command_args)


class SlimJSONRPC:
    """Basic implementation of JSON RPC control of SlimProto players."""

    def __init__(self, server: "SlimServer", port: int = 3484) -> None:
        """Initialize."""
        self.server = server
        self.port = port
        self.logger = server.logger.getChild("jsonrpc")

    async def start(self) -> asyncio.Server:
        """Start running the server."""
        self.logger.info("Starting SLIMProto JSON RPC server on port %s", self.port)
        return await asyncio.start_server(self._handle_client, "0.0.0.0", self.port)

    @staticmethod
    async def handle_mixer(player: SlimClient, args: List[str]) -> None:
        """Handle mixer command."""
        cmd = args[0]
        arg = args[1]
        if cmd == "volume" and "+" in arg:
            volume_level = player.volume_level + int(arg.split("+")[1])
            await player.volume_set(min(100, volume_level))
        elif cmd == "volume" and "-" in arg:
            volume_level = player.volume_level - int(arg.split("-")[1])
            await player.volume_set(max(0, volume_level))
        elif cmd == "volume":
            await player.volume_set(int(arg))
        elif cmd == "muting":
            await player.mute(bool(arg))

    @staticmethod
    async def handle_button(player: SlimClient, args: List[str]) -> None:
        """Handle button command."""
        cmd = args[0]
        if cmd == "volup":
            await player.volume_set(min(100, player.volume_level + 2))
        elif cmd == "voldown":
            await player.volume_set(max(0, player.volume_level - 2))
        elif cmd == "power":
            await player.power(not player.powered)

    @staticmethod
    async def handle_play(player: SlimClient, args: List[str]) -> None:
        """Handle play command."""
        await player.play()

    @staticmethod
    async def handle_pause(player: SlimClient, args: List[str]) -> None:
        """Handle pause command."""
        await player.pause()

    @staticmethod
    async def handle_stop(player: SlimClient, args: List[str]) -> None:
        """Handle stop command."""
        await player.pause()

    @staticmethod
    async def handle_power(player: SlimClient, args: List[str]) -> None:
        """Handle power command."""
        if len(args) == 0:
            await player.power(not player.powered)
        else:
            await player.power(bool(args[0]))

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle new connection on the socket."""
        try:
            raw_request = b""
            while True:
                chunk = await reader.read(CHUNK_SIZE)
                raw_request += chunk
                if len(chunk) < CHUNK_SIZE:
                    break
            request = raw_request.decode("iso-8859-1")
            head, body = request.split("\r\n\r\n")
            headers = head.split("\r\n")
            method, path, _ = headers[0].split(" ")

            if method != "POST" or path != "/jsonrpc.js":
                await self.send_response(writer, 405, "Method or path not allowed")
                return

            rpc_msg = JSONRPCMessage(**json.loads(body))
            self.logger.debug(
                "handle request: %s for player %s",
                rpc_msg.command_str,
                rpc_msg.player_id,
            )
            player = self.server.get_player(rpc_msg.player_id)
            if not player:
                await self.send_response(
                    writer, 404, f"Player {rpc_msg.player_id} not found"
                )
                return
            # emit event for all commands, so that lib consumer can handle special usecases
            self.server.signal_event(
                SlimEvent(
                    EventType.PLAYER_RPC_EVENT,
                    player.player_id,
                    {
                        "command": rpc_msg.command,
                        "args": rpc_msg.command_args,
                        "command_str": rpc_msg.command_str,
                    },
                )
            )
            # find handler for request
            handler = getattr(self, f"handle_{rpc_msg.command}", None)
            if handler is None:
                await self.send_response(
                    writer, 405, f"No handler for {rpc_msg.command}"
                )
                return

            result = await handler(player, rpc_msg.command_args)
            await self.send_response(writer, data={"result": result, "id": rpc_msg.id})

        except Exception as exc:  # pylint: disable=broad-except
            self.logger.exception(exc)
            await self.send_response(writer, 501, str(exc))

        finally:
            # make sure the connection gets closed
            writer.close()
            await writer.wait_closed()

    @staticmethod
    async def send_response(
        writer: asyncio.StreamWriter,
        status: int = 200,
        status_text: str = "OK",
        data: dict = None,
    ) -> str:
        """Build HTTP response from data."""
        content_type = "json" if data is not None else "text"
        response = (
            f"HTTP/1.1 {status} {status_text}\r\n"
            f"Content-Type: text/{content_type}; charset=UTF-8\r\n"
            "Content-Encoding: UTF-8\r\n"
            "Connection: closed\r\n\r\n"
        )
        if data is not None:
            response += json.dumps(data)
        writer.write(response.encode("iso-8859-1"))
        await writer.drain()
