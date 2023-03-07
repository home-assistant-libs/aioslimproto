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
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict
from uuid import uuid1

from aioslimproto.client import PlayerState
from aioslimproto.errors import SlimProtoException
from aioslimproto.util import select_free_port

if TYPE_CHECKING:
    from .client import SlimClient
    from .server import SlimServer

CHUNK_SIZE = 50


class CometDResponse(TypedDict):
    """CometD Response Message."""

    channel: str
    id: str
    data: dict[str, Any]


class PlayerMessage(TypedDict):
    """Player Message as sent on the cli."""

    ip: str  # "1.1.1.1:38380"
    playerid: str  # 00:11:22:33:44:55
    playerindex: int
    seq_no: int  # 0
    displaytype: str | None  # none
    canpoweroff: int
    isplaying: int
    power: int
    firmware: str
    name: str
    modelname: str
    connected: int
    model: str
    uuid: str | None  # None
    isplayer: int  # 1


class PlayersMessage(TypedDict):
    """Players Message as sent on the cli."""

    players_loop: list[PlayerMessage]
    count: int


ServerStatusMessage = TypedDict(
    "ServerStatusMessage",
    {
        "ip": str,
        "httpport": str,
        "version": str,
        "uuid": str,
        "info total genres": int,
        "sn player count": int,
        "lastscan": str,
        "info total duration": int,
        "info total albums": int,
        "info total songs": int,
        "info total artists": int,
        "players_loop": list[PlayerMessage],
        "player count": int,
        "other player count": int,
        "other_players_loop": list[PlayerMessage],
    },
)


@dataclass
class CLIMessage:
    """Representation of a CLI Command message message."""

    player_id: str
    command_str: str
    command: str
    command_args: list[str]

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

    id: int | str
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

    @classmethod
    def from_cometd(  # pylint: disable=redefined-builtin
        cls, cometd_msg: dict
    ) -> JSONRPCMessage:
        """Parse a JSONRPCMessage from JSON."""
        return cls.from_json(
            id=cometd_msg.get("id", 0),
            method=cometd_msg["channel"],
            params=cometd_msg["data"]["request"],
        )


class SlimProtoCLI:
    """Basic implementation of CLI control for SlimProto players."""

    def __init__(
        self,
        server: "SlimServer",
        cli_port: int | None = None,
        cli_port_json: int | None = 0,
    ) -> None:
        """
        Initialize Telnet and/or Json interface CLI.

        Set port to None to disable the interface, set to 0 for auto select a free port.
        """
        self.server = server
        self.cli_port = cli_port
        self.cli_port_json = cli_port_json
        self.logger = server.logger.getChild("cli")
        self._cometd_clients: dict[str, asyncio.Queue[CometDResponse]] = {}
        self._player_map: dict[str, str] = {}

    async def start(self) -> list[asyncio.Server]:
        """Start running the server(s)."""
        # if port is specified as 0, auto select a free port for the cli/json interface
        if self.cli_port == 0:
            self.cli_port = await select_free_port(9090, 9190)
        if self.cli_port_json == 0:
            self.cli_port_json = await select_free_port(9000, 9089)
        servers: list[asyncio.Server] = []
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
    async def handle_mixer(player: SlimClient, args: list[str]) -> None:
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
    async def handle_button(player: SlimClient, args: list[str]) -> None:
        """Handle button command."""
        cmd = args[0]
        if cmd == "volup":
            await player.volume_set(min(100, player.volume_level + 5))
        elif cmd == "voldown":
            await player.volume_set(max(0, player.volume_level - 5))
        elif cmd == "power":
            await player.power(not player.powered)

    @staticmethod
    async def handle_play(player: SlimClient, args: list[str]) -> None:
        """Handle play command."""
        await player.play()

    @staticmethod
    async def handle_pause(player: SlimClient, args: list[str]) -> None:
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
    async def handle_stop(player: SlimClient, args: list[str]) -> None:
        """Handle stop command."""
        await player.pause()

    @staticmethod
    async def handle_power(player: SlimClient, args: list[str]) -> None:
        """Handle power command."""
        if len(args) == 0:
            await player.power(not player.powered)
        else:
            await player.power(bool(args[0]))

    async def handle_players(
        self, player: SlimClient | None, args: list[str]
    ) -> PlayersMessage:
        """Handle players command."""
        return {
            "players_loop": [
                {
                    "ip": f"{player.device_address}",
                    "playerid": player.player_id,
                    "playerindex": index,
                    "seq_no": 0,
                    "displaytype": "none",
                    "canpoweroff": 1,
                    "isplaying": player.state == PlayerState.PLAYING,
                    "power": int(player.powered),
                    "firmware": player.firmware,
                    "name": player.name,
                    "modelname": player.device_model,
                    "connected": int(player.connected),
                    "model": player.device_type,
                    "uuid": None,
                    "isplayer": 1,
                }
                for index, player in enumerate(self.server.players)
            ],
            "count": len(self.server.players),
        }

    async def handle_serverstatus(
        self, player: SlimClient | None, args: list[str]
    ) -> ServerStatusMessage:
        """Handle serverstatus command."""
        players = await self.handle_players(None, [])
        return {
            "ip": "0.0.0.0",  # TODO
            "httpport": str(self.cli_port_json),
            "version": "7.7.5",
            "uuid": "aioslimproto",
            "info total duration": 0,
            "info total genres": 0,
            "sn player count": 0,
            "lastscan": "0",
            "info total albums": 0,
            "info total songs": 0,
            "info total artists": 0,
            "players_loop": players["players_loop"],
            "player count": players["count"],
            "other player count": 0,
            "other_players_loop": [],
        }

    async def handle_menu(
        self, player: SlimClient | None, args: list[str]
    ) -> dict[str, Any]:
        """Handle menu request from CLI."""
        return {
            "item_loop": [],
            "offset": 0,
            "count": 0,
        }

    async def handle_status(self, player: SlimClient | None, args: list[str]):
        """Handle request for Player status."""
        # TODO: Extend this metadata with a bit more sane information,
        # like current playing metadata etc.
        # at least with this basic info returned the player boots up and plays music.
        return {
            "base": {"actions": {}},
            "time": player.elapsed_seconds,
            "playlist_timestamp": player.elapsed_seconds,
            "playlist_cur_index": "0",
            "playlist mode": "off",
            "mixer volume": player.volume_level,
            "alarm_timeout_seconds": 3600,
            "count": 1,
            "alarm_version": 2,
            "preset_data": [],
            "seq_no": 0,
            "remoteMeta": {},
            "playlist_name": "aioslimproto",
            "power": int(player.powered),
            "player_ip": player.device_address,
            "rate": 1,
            "player_name": player.name,
            "duration": 0,
            "remote": 0,
            "playlist_modified": 0,
            "item_loop": [],
            "playlist shuffle": 0,
            "alarm_next": 0,
            "digital_volume_control": 1,
            "playlist_id": 0,
            "alarm_snooze_seconds": 0,
            "can_seek": 0,
            "player_connected": 1,
            "playlist_tracks": 1,
            "current_title": None,
            "mode": "play" if player.state == PlayerState.PLAYING else "pause",
            "preset_loop": [],
            "alarm_state": "none",
            "offset": "-",
            "playlist repeat": 0,
            "signalstrength": 0,
        }

    async def _handle_telnet_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle new connection on the socket."""
        try:
            while True:
                raw_request = await reader.readline()
                raw_request = raw_request.strip().decode("iso-8859-1")
                cli_msg = CLIMessage.from_string(raw_request)
                result = await self._handle_cli_message(cli_msg)
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
            await writer.drain()
            self.logger.debug(err)
        finally:
            # make sure the connection gets closed
            if not writer.is_closing():
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
            request = raw_request.decode("UTF-8")
            head, body = request.split("\r\n\r\n", 1)
            headers = head.split("\r\n")
            method, path, _ = headers[0].split(" ")
            self.logger.debug(
                "Client request on JSON RPC: %s %s -- %s", method, path, body
            )

            # regular json rpc request
            if path == "/jsonrpc.js":
                json_msg = json.loads(body)
                rpc_msg = JSONRPCMessage.from_json(**json_msg)
                result = await self._handle_cli_message(rpc_msg)
                await self.send_json_response(
                    writer, data={**json_msg, "result": result}
                )
                return

            # cometd request (used by jive interface)
            if path == "/cometd":
                try:
                    json_msg = json.loads(body)
                except json.JSONDecodeError:
                    return
                # forward cometd to seperate handler as it is slightly more involved
                await self._handle_cometd_client(json_msg, writer)
                return

            # deny all other paths
            await self.send_status_response(writer, 405, "Method or path not allowed")

        except SlimProtoException as exc:
            self.logger.exception(exc)
            await self.send_status_response(writer, 501, str(exc))

    async def _handle_cometd_client(
        self,
        json_msg: list(dict[str, Any]),
        writer: asyncio.StreamWriter,
    ) -> None:
        """
        Handle CometD request on the json CLI.

        https://github.com/Logitech/slimserver/blob/public/8.4/Slim/Web/Cometd.pm
        """
        responses = []
        is_subscribe_connection = False
        clientid = ""

        # cometd message is an array of commands/messages
        for cometd_msg in json_msg:

            channel = cometd_msg.get("channel")
            # try to figure out clientid
            if not clientid and cometd_msg.get("clientId"):
                clientid = cometd_msg["clientId"]
            elif not clientid and cometd_msg.get("data", {}).get("response"):
                clientid = cometd_msg["data"]["response"].split("/")[1]
            # messageid is optional but if provided we must pass it along
            msgid = cometd_msg.get("id", "")
            response = {
                "channel": channel,
                "successful": True,
                "id": msgid,
                "clientId": clientid,
            }
            self._cometd_clients.setdefault(clientid, asyncio.Queue())

            if channel == "/meta/handshake":
                # handshake message
                response["version"] = "1.0"
                response["clientId"] = uuid1().hex
                response["supportedConnectionTypes"] = ["streaming"]
                response["advice"] = {
                    "reconnect": "retry",
                    "timeout": 60000,
                    "interval": 0,
                }

            elif channel in ("/meta/connect", "/meta/reconnect"):
                # (re)connect message
                self.logger.debug("CometD Client (re-)connected: %s", clientid)
                response["timestamp"] = time.strftime(
                    "%a, %d %b %Y %H:%M:%S %Z", time.gmtime()
                )
                response["advice"] = {"interval": 5000}

            elif channel == "/meta/subscribe":
                is_subscribe_connection = True
                response["subscription"] = cometd_msg.get("subscription")

            elif channel in ("/slim/request", "/slim/subscribe"):
                # async request (similar to json rpc call)
                rpc_msg = JSONRPCMessage.from_cometd(cometd_msg)
                result = await self._handle_cli_message(rpc_msg)
                if rpc_msg.player_id:
                    # create mapping table which client is subscribing to which player
                    self._player_map[clientid] = rpc_msg.player_id

                # the result is posted on the client queue
                await self._cometd_clients[clientid].put(
                    {
                        "channel": cometd_msg["data"]["response"],
                        "id": msgid,
                        "data": result,
                        "ext": {"priority": ""},
                    }
                )

            else:
                self.logger.warning("Unhandled CometD channel %s", channel)

            # always reply with the (default) response to every message
            responses.append(response)

        # regular command/handshake messages are just replied and connection closed
        if not is_subscribe_connection:
            await self.send_json_response(writer, data=responses)
            return

        # send headers only
        await self.send_status_response(
            writer, 200, headers={"Transfer-Encoding": "chunked"}
        )
        # the subscription connection is kept open and events are streamed to the client
        data = json.dumps(responses)
        writer.write(
            f"{hex(len(data)).replace('0x', '')}\r\n{data}\r\n".encode("iso-8859-1")
        )
        await writer.drain()

        # as some sort of heartbeat, the server- and playerstatus is sent every 30 seconds
        loop = asyncio.get_running_loop()

        async def send_status():
            if clientid not in self._cometd_clients:
                return

            player = self.server.get_player(self._player_map.get(clientid))
            await self._cometd_clients[clientid].put(
                {
                    "channel": f"/{clientid}/slim/serverstatus",
                    "id": "1",
                    "data": await self.handle_serverstatus(player, []),
                }
            )
            if player is not None:
                await self._cometd_clients[clientid].put(
                    {
                        "channel": f"/{clientid}/slim/status",
                        "id": "1",
                        "data": await self.handle_status(player, []),
                    }
                )

            # reschedule self
            loop.call_later(30, loop.create_task, send_status())

        loop.create_task(send_status())

        # keep delivering messages to the client until it disconnects
        try:
            # keep sending messages/events from the client's queue
            while clientid in self._cometd_clients and not writer.is_closing():
                msg = await self._cometd_clients[clientid].get()
                # make sure we always send an array of messages
                data = json.dumps([msg])
                writer.write(
                    f"{hex(len(data)).replace('0x', '')}\r\n{data}\r\n".encode(
                        "iso-8859-1"
                    )
                )
                try:
                    await writer.drain()
                except (ConnectionResetError, BrokenPipeError):
                    return

        finally:
            self._cometd_clients.pop(clientid, None)

    async def _handle_cli_message(self, msg: CLIMessage) -> Any:
        """Handle message from one of the CLi interfaces."""
        # NOTE: Player_id can be empty string for generic commands
        player = self.server.get_player(msg.player_id)

        # find handler for request
        handler = getattr(self, f"handle_{msg.command}", None)
        if handler is None:
            self.logger.warning(f"No handler for {msg.command_str}")
            return {}

        return await handler(player, msg.command_args)

    @staticmethod
    async def send_json_response(
        writer: asyncio.StreamWriter,
        data: dict | list[dict] | None,
        status: int = 200,
        status_text: str = "OK",
        headers: dict | None = None,
    ) -> str:
        """Build HTTP response from data."""
        body = json.dumps(data)
        response = ""
        if not headers:
            headers = {}
        headers = {
            "Server": "aioslimproto",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Content-Type": "application/json",
            "Expires": "-1",
            "Connection": "keep-alive",
            **headers,
        }
        response = f"HTTP/1.1 {status} {status_text}\r\n"
        for key, value in headers.items():
            response += f"{key}: {value}\r\n"
        response += "\r\n"
        response += body
        writer.write(response.encode("iso-8859-1"))
        await writer.drain()

    @staticmethod
    async def send_status_response(
        writer: asyncio.StreamWriter,
        status: int,
        status_text: str = "OK",
        headers: dict | None = None,
    ) -> str:
        """Build HTTP response from data."""
        response = ""
        if not headers:
            headers = {}
        headers = {"Server": "aioslimproto", **headers}
        response = f"HTTP/1.1 {status} {status_text}\r\n"
        for key, value in headers.items():
            response += f"{key}: {value}\r\n"
        response += "\r\n"
        writer.write(response.encode())
        await writer.drain()
