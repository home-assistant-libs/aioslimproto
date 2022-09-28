"""
Basic implementation of CLI control for SlimProto players.

Some players (e.g. PiCorePlayer) use the jsonrpc api to control for example volume remotely.

Both the (legacy) Telnet CLI and the JSON RPC interface are supported.
This is a very basic implementation that only fulfills commands needed by those players,
other commands will be published as-is on the eventbus for library consumers to act on.
there's no support for media browsing through this minimal api, this is NOT a replacement for
the Logitech Media Server.

https://github.com/elParaguayo/LMS-CLI-Documentation/blob/master/LMS-CLI.md
https://gist.github.com/samtherussell/335bf9ba75363bd167d2470b8689d9f2
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, List, Optional, Union

from aioslimproto.client import PlayerState
from aioslimproto.const import EventType, SlimEvent
from aioslimproto.errors import InvalidPlayer, SlimProtoException, UnsupportedCommand
from aioslimproto.util import select_free_port

if TYPE_CHECKING:
    from .client import SlimClient
    from .server import SlimServer

CHUNK_SIZE = 50


@dataclass
class CLIMessage:
    """Representation of a CLI Command message message."""

    player_id: str
    command_str: str
    command: str
    command_args: List[str]

    @classmethod
    def from_string(cls, raw: str) -> CLIMessage:
        """Parse CLIMessage from raw message string."""
        cmd_parts = raw.split(" ")
        if len(cmd_parts) == 1:
            player_id = ""
            command = cmd_parts[0]
            command_str = raw
        else:
            player_id = cmd_parts[0]
            command = cmd_parts[1]
            command_str = raw.replace(player_id, "").strip()
        return cls(
            player_id=player_id,
            command_str=command_str,
            command=command,
            command_args=cmd_parts[1:],
        )


@dataclass
class JSONRPCMessage(CLIMessage):
    """Representation of JSON RPC Message."""

    id: Union[int, str]
    method: str

    @classmethod
    def from_json(  # pylint: disable=redefined-builtin
        cls, id: int, method: str, params: list
    ) -> JSONRPCMessage:
        """Parse a JSONRPCMessage from JSON."""
        player_id = str(params[0])
        command = str(params[1][0])
        command_args = [str(v) for v in params[1][1:]]
        command_str = " ".join([command] + command_args)
        return cls(
            id=id,
            method=method,
            player_id=player_id,
            command=command,
            command_args=command_args,
            command_str=command_str,
        )


@dataclass
class CometDMessage(CLIMessage):
    """Representation of CometD RPC Message."""

    id: Union[int, str]
    method: str

    @classmethod
    def from_json(  # pylint: disable=redefined-builtin
        cls, id: int, method: str, params: list
    ) -> JSONRPCMessage:
        """Parse a JSONRPCMessage from JSON."""
        player_id = str(params[0])
        command = str(params[1][0])
        command_args = [str(v) for v in params[1][1:]]
        command_str = " ".join([command] + command_args)
        return cls(
            id=id,
            method=method,
            player_id=player_id,
            command=command,
            command_args=command_args,
            command_str=command_str,
        )


# [{"ext":{"mac":"ac:de:48:00:11:22","uuid":"6dca26156a1e39c3027a72dbf9f0dab5","rev":"8.0.1 r1382"},"supportedConnectionTypes":["streaming"],"version":"1.0","channel":"\\/meta\\/handshake"}]


class SlimProtoCLI:
    """Basic implementation of CLI control for SlimProto players."""

    def __init__(
        self,
        server: "SlimServer",
        cli_port: Optional[int] = 0,
        cli_port_json: Optional[int] = 0,
    ) -> None:
        """
        Initialize Telnet and/or Json interface CLI.

        Set port to None to disable the interface, set to 0 for auto select a free port.
        """
        self.server = server
        self.cli_port = cli_port
        self.cli_port_json = cli_port_json
        self.logger = server.logger.getChild("jsonrpc")

    async def start(self) -> List[asyncio.Server]:
        """Start running the server(s)."""
        # if port is specified as 0, auto select a free port for the cli/json interface
        if self.cli_port == 0:
            self.cli_port = await select_free_port(9090, 9190)
        if self.cli_port_json == 0:
            self.cli_port_json = await select_free_port(self.cli_port + 1, 9190)
        servers: List[asyncio.Server] = []
        if self.cli_port is not None:
            self.logger.info(
                "Starting (legacy/telnet) SLIMProto CLI on port %s", self.cli_port
            )
            servers.append(
                await asyncio.start_server(
                    self._handle_telnet_client, "0.0.0.0", self.cli_port
                )
            )
        if self.cli_port_json is not None:
            self.logger.info(
                "Starting SLIMProto JSON RPC CLI on port %s", self.cli_port_json
            )
            servers.append(
                await asyncio.start_server(
                    self._handle_json_client, "0.0.0.0", self.cli_port_json
                )
            )
        return servers

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
            await player.volume_set(min(100, player.volume_level + 5))
        elif cmd == "voldown":
            await player.volume_set(max(0, player.volume_level - 5))
        elif cmd == "power":
            await player.power(not player.powered)

    @staticmethod
    async def handle_play(player: SlimClient, args: List[str]) -> None:
        """Handle play command."""
        await player.play()

    @staticmethod
    async def handle_pause(player: SlimClient, args: List[str]) -> None:
        """Handle pause command."""
        if args:
            should_pause = bool(args[0])
        else:
            # without args = toggle
            should_pause = player.state != PlayerState.PAUSED
        if should_pause:
            await player.pause()
        else:
            await player.play()

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

    async def _handle_telnet_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle new connection on the socket."""
        try:
            while True:
                raw_request = await reader.readline()
                raw_request = raw_request.strip().decode("iso-8859-1")
                cli_msg = CLIMessage.from_string(raw_request)
                result = await self._handle_message(cli_msg)
                # echo back the command
                response = raw_request
                if result is not None:
                    response += json.dumps(result)
                response += "\n"
                writer.write(response.encode("iso-8859-1"))
                await writer.drain()
        except SlimProtoException:
            err = f"Unhandled request: {raw_request}\n"
            writer.write(err.encode("iso-8859-1"))
        finally:
            # make sure the connection gets closed
            writer.close()
            await writer.wait_closed()

    async def _handle_json_client(
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
            head, body = request.split("\r\n\r\n", 1)
            headers = head.split("\r\n")
            method, path, _ = headers[0].split(" ")

            if method != "POST" or path not in ("/jsonrpc.js", "/cometd"):
                await self.send_json_response(writer, 405, "Method or path not allowed")
                return

            rpc_msg = JSONRPCMessage.from_json(**json.loads(body))
            result = await self._handle_message(rpc_msg)
            await self.send_json_response(
                writer, data={"result": result, "id": rpc_msg.id}
            )

        except SlimProtoException as exc:
            await self.send_json_response(writer, 501, str(exc))

        finally:
            # make sure the connection gets closed
            writer.close()
            await writer.wait_closed()

    # async def _handle_cometd_message(self, message: )

    async def _handle_message(self, msg: CLIMessage) -> Any:
        """Handle message from one of the CLi interfaces."""
        self.logger.debug(
            "handle request: %s for player %s",
            msg.command_str,
            msg.player_id,
        )
        if not msg.player_id:
            # we do not (yet) support generic commands
            raise UnsupportedCommand(f"No handler for {msg.command_str}")

        player = self.server.get_player(msg.player_id)
        if not player:
            raise InvalidPlayer(f"Player {msg.player_id} not found")

        # emit event for all commands, so that lib consumer can handle special usecases
        self.server.signal_event(
            SlimEvent(
                EventType.PLAYER_CLI_EVENT,
                player.player_id,
                {
                    "command": msg.command,
                    "args": msg.command_args,
                    "command_str": msg.command_str,
                },
            )
        )
        # find handler for request
        handler = getattr(self, f"handle_{msg.command}", None)
        if handler is None:
            raise UnsupportedCommand(f"No handler for {msg.command_str}")

        return await handler(player, msg.command_args)

    @staticmethod
    async def send_json_response(
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
