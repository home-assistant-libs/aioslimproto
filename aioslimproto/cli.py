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
import urllib.parse
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import shortuuid

from aioslimproto.client import PlayerState
from aioslimproto.errors import SlimProtoException
from aioslimproto.util import empty_queue, select_free_port

from .models import (
    PLAYMODE_MAP,
    CometDResponse,
    CommandErrorMessage,
    CommandMessage,
    CommandResultMessage,
    EventType,
    MediaDetails,
    PlayerItem,
    PlayersResponse,
    PlayerStatusResponse,
    PlaylistItem,
    ServerStatusResponse,
    SlimEvent,
    SlimMenuItem,
    SlimSubscribeMessage,
)

if TYPE_CHECKING:
    from .client import SlimClient
    from .server import SlimServer

# ruff: noqa: ARG004,ARG002

CHUNK_SIZE = 50

ArgsType = list[int | str]
KwargsType = dict[str, Any]


@dataclass
class CometDClient:
    """Representation of a connected CometD client."""

    client_id: str
    player_id: str = ""
    queue: asyncio.Queue[CometDResponse] = field(default_factory=asyncio.Queue)
    last_seen: int = int(time.time())
    first_event: CometDResponse | None = None
    meta_subscriptions: set[str] = field(default_factory=set)
    slim_subscriptions: dict[str, SlimSubscribeMessage] = field(default_factory=dict)


def parse_value(raw_value: int | str) -> int | str | tuple[str, int | str]:
    """
    Transform API param into a usable value.

    Integer values are sometimes sent as string so we try to parse that.
    """
    if isinstance(raw_value, str):
        if ":" in raw_value:
            # this is a key:value value
            key, val = raw_value.split(":", 1)
            if val.isnumeric():
                val = int(val)
            return (key, val)
        if raw_value.isnumeric():
            # this is an integer sent as string
            return int(raw_value)
    return raw_value


def parse_args(raw_values: list[int | str]) -> tuple[ArgsType, KwargsType]:
    """Pargse Args and Kwargs from raw CLI params."""
    args: ArgsType = []
    kwargs: KwargsType = {}
    for raw_value in raw_values:
        value = parse_value(raw_value)
        if isinstance(value, tuple):
            kwargs[value[0]] = value[1]
        else:
            args.append(value)
    return (args, kwargs)


class SlimProtoCLI:
    """Basic implementation of CLI control for SlimProto players."""

    _unsub_callback: Callable | None = None
    _periodic_task: asyncio.Task | None = None
    _servers: list[asyncio.Server] | None = None

    def __init__(
        self,
        server: SlimServer,
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
        self._cometd_clients: dict[str, CometDClient] = {}
        self._player_map: dict[str, str] = {}

    async def start(self) -> None:
        """Start running the server(s)."""
        # if port is specified as 0, auto select a free port for the cli/json interface
        if self.cli_port == 0:
            self.cli_port = await select_free_port(9090, 9190)
        if self.cli_port_json == 0:
            self.cli_port_json = await select_free_port(9000, 9089)
        servers: list[asyncio.Server] = []
        if self.cli_port is not None:
            self.logger.info("Starting (legacy/telnet) SLIMProto CLI on port %s", self.cli_port)
            servers.append(
                await asyncio.start_server(self._handle_cli_client, "0.0.0.0", self.cli_port)
            )
        if self.cli_port_json is not None:
            self.logger.info("Starting SLIMProto JSON RPC CLI on port %s", self.cli_port_json)
            servers.append(
                await asyncio.start_server(self._handle_json_request, "0.0.0.0", self.cli_port_json)
            )
            self._unsub_callback = self.server.subscribe(
                self._on_player_event,
                (EventType.PLAYER_UPDATED, EventType.PLAYER_CONNECTED),
            )
            self._periodic_task = asyncio.create_task(self._do_periodic())
        self._servers = servers

    async def stop(self) -> None:
        """Stop running the server(s)."""
        for server in self._servers or []:
            server.close()
        if self._unsub_callback:
            self._unsub_callback()
            self._unsub_callback = None
        if self._periodic_task:
            self._periodic_task.cancel()
            self._periodic_task = None

    async def _handle_json_request(
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
            try:
                head, body = request.split("\r\n\r\n", 1)
            except ValueError:
                head = request
                body = ""
            headers = head.split("\r\n")
            method, path, _ = headers[0].split(" ")
            self.logger.debug("Client request on JSON RPC: %s %s -- %s", method, path, body)

            if method not in ("GET", "POST"):
                await self.send_status_response(writer, 405, "Method not allowed")

            # regular json rpc request
            if path == "/jsonrpc.js":
                command_msg: CommandMessage = json.loads(body)
                self.logger.debug("Received request: %s", command_msg)
                cmd_result = await self._handle_command(command_msg["params"])
                if cmd_result is None:
                    result: CommandErrorMessage = {
                        **command_msg,
                        "error": {"code": -1, "message": "Invalid command"},
                    }
                else:
                    result: CommandResultMessage = {
                        **command_msg,
                        "result": cmd_result,
                    }
                # return the response to the client
                await self.send_json_response(writer, data=result)
                return

            # cometd request (used by jive interface)
            if path == "/cometd":
                try:
                    json_msg = json.loads(body)
                except json.JSONDecodeError:
                    return
                # forward cometd to separate handler as it is slightly more involved
                await self._handle_cometd_client(json_msg, writer)
                return

            # deny all other paths
            await self.send_status_response(writer, 405, "Method or path not allowed")

        except SlimProtoException as exc:
            self.logger.exception(exc)
            await self.send_status_response(writer, 501, str(exc))
        finally:
            if not writer.is_closing():
                writer.close()

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

    async def _handle_cli_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle new connection on the legacy CLI."""
        # https://raw.githubusercontent.com/Logitech/slimserver/public/7.8/HTML/EN/html/docs/cli-api.html
        # https://github.com/elParaguayo/LMS-CLI-Documentation/blob/master/LMS-CLI.md
        self.logger.debug("Client connected on Telnet CLI")
        try:
            while True:
                raw_request = await reader.readline()
                raw_request = raw_request.strip().decode("iso-8859-1")
                if not raw_request:
                    break
                # request comes in as url encoded strings, separated by space
                raw_params = [urllib.parse.unquote(x) for x in raw_request.split(" ")]
                # the first param is either a macaddress or a command
                if ":" in raw_params[0]:
                    # assume this is a mac address (=player_id)
                    player_id = raw_params[0]
                    command = raw_params[1]
                    command_params = raw_params[2:]
                else:
                    player_id = ""
                    command = raw_params[0]
                    command_params = raw_params[1:]

                args, kwargs = parse_args(command_params)

                response: str = raw_request

                # check if we have a handler for this command
                # note that we only have support for very limited commands
                # just enough for compatibility with players but not to be used as api
                # with 3rd party tools!
                try:
                    handler = getattr(self, f"_handle_{command}")
                    self.logger.debug(
                        "Handling CLI-request (player: %s command: %s - args: %s - kwargs: %s)",
                        player_id,
                        command,
                        str(args),
                        str(kwargs),
                    )
                    cmd_result: list[str] = handler(player_id, *args, **kwargs)
                    if asyncio.iscoroutine(cmd_result):
                        cmd_result = await cmd_result

                    if isinstance(cmd_result, dict):
                        result_parts = dict_to_strings(cmd_result)
                        result_str = " ".join(urllib.parse.quote(x) for x in result_parts)
                    elif not cmd_result:
                        result_str = ""
                    else:
                        result_str = str(cmd_result)
                    response += " " + result_str
                except (AttributeError, NotImplementedError):
                    # no handler found, forward as event
                    if player := self.server.get_player(player_id):
                        args_str = " ".join([command] + [str(x) for x in args])
                        player.callback(EventType.PLAYER_CLI_EVENT, player, args_str)
                    else:
                        self.logger.warning(
                            "No handler for %s (player: %s - args: %s - kwargs: %s)",
                            command,
                            player_id,
                            str(args),
                            str(kwargs),
                        )
                # echo back the request and the result (if any)
                response += "\n"
                writer.write(response.encode("iso-8859-1"))
                await writer.drain()
        except ConnectionResetError:
            pass
        except Exception as err:
            self.logger.debug("Error handling CLI command", exc_info=err)
        finally:
            self.logger.debug("Client disconnected from Telnet CLI")

    async def _handle_cometd_client(  # noqa: PLR0912
        self,
        json_msg: list[dict[str, Any]],
        writer: asyncio.StreamWriter,
    ) -> None:
        """
        Handle CometD request on the json CLI.

        https://github.com/Logitech/slimserver/blob/public/8.4/Slim/Web/Cometd.pm
        """
        logger = self.logger.getChild("cometd")
        # ruff: noqa: PLR0915
        clientid: str = ""
        response = []
        streaming = False
        # cometd message is an array of commands/messages
        for cometd_msg in json_msg:
            channel = cometd_msg.get("channel")
            # try to figure out clientid
            if not clientid:
                clientid = cometd_msg.get("clientId")
            if not clientid and channel == "/meta/handshake":
                # generate new clientid
                clientid = shortuuid.uuid()
                self._cometd_clients[clientid] = CometDClient(
                    client_id=clientid,
                )
            elif not clientid and channel in ("/slim/subscribe", "/slim/request"):
                # pull clientId out of response channel
                clientid = cometd_msg["data"]["response"].split("/")[1]
            elif not clientid and channel == "/slim/unsubscribe":
                # pull clientId out of unsubscribe
                clientid = cometd_msg["data"]["unsubscribe"].split("/")[1]
            assert clientid, "No clientID provided"
            logger.debug("Incoming message for channel '%s' - clientid: %s", channel, clientid)

            # messageid is optional but if provided we must pass it along
            msgid = cometd_msg.get("id", "")

            if clientid not in self._cometd_clients:
                # If a client sends any request and we do not have a valid clid record
                # because the streaming connection has been lost for example, re-handshake them
                return await self.send_json_response(
                    writer,
                    [
                        {
                            "id": msgid,
                            "channel": channel,
                            "clientId": None,
                            "successful": False,
                            "timestamp": time.strftime("%a, %d %b %Y %H:%M:%S %Z", time.gmtime()),
                            "error": "invalid clientId",
                            "advice": {
                                "reconnect": "handshake",
                                "interval": 0,
                            },
                        }
                    ],
                )

            # get the cometd_client object for the clientid
            cometd_client = self._cometd_clients[clientid]
            cometd_client.last_seen = int(time.time())

            if channel == "/meta/handshake":
                # handshake message
                response.append(
                    {
                        "id": msgid,
                        "channel": channel,
                        "version": "1.0",
                        "supportedConnectionTypes": ["long-polling", "streaming"],
                        "clientId": clientid,
                        "successful": True,
                        "advice": {
                            # one of "none", "retry", "handshake"
                            "reconnect": "retry",
                            # initial interval is 0 to support long-polling's connect request
                            "interval": 0,
                            "timeout": 60000,
                        },
                    }
                )
                # playerid (mac) and uuid belonging to the client is sent in the ext field
                if player_id := cometd_msg.get("ext", {}).get("mac"):
                    cometd_client.player_id = player_id
                    if (uuid := cometd_msg.get("ext", {}).get("uuid")) and (
                        player := self.server.get_player(player_id)
                    ):
                        player.extra_data["uuid"] = uuid

            elif channel in ("/meta/connect", "/meta/reconnect"):
                # (re)connect message
                logger.debug("Client (re-)connected: %s", clientid)
                streaming = cometd_msg["connectionType"] == "streaming"
                # confirm the connection
                response.append(
                    {
                        "id": msgid,
                        "channel": channel,
                        "clientId": clientid,
                        "successful": True,
                        "timestamp": time.strftime("%a, %d %b %Y %H:%M:%S %Z", time.gmtime()),
                        "advice": {
                            # update interval for streaming mode
                            "interval": 5000 if streaming else 0
                        },
                    }
                )
                # TODO: do we want to implement long-polling support too ?
                # https://github.com/Logitech/slimserver/blob/d9ebda7ebac41e82f1809dd85b0e4446e0c9be36/Slim/Web/Cometd.pm#L292

            elif channel == "/meta/disconnect":
                # disconnect message
                logger.debug("CometD Client disconnected: %s", clientid)
                self._cometd_clients.pop(clientid)
                return await self.send_json_response(
                    writer,
                    [
                        {
                            "id": msgid,
                            "channel": channel,
                            "clientId": clientid,
                            "successful": True,
                            "timestamp": time.strftime("%a, %d %b %Y %H:%M:%S %Z", time.gmtime()),
                        }
                    ],
                )

            elif channel == "/meta/subscribe":
                cometd_client.meta_subscriptions.add(cometd_msg["subscription"])
                response.append(
                    {
                        "id": msgid,
                        "channel": channel,
                        "clientId": clientid,
                        "successful": True,
                        "subscription": cometd_msg["subscription"],
                    }
                )

            elif channel == "/meta/unsubscribe":
                if cometd_msg["subscription"] in cometd_client.meta_subscriptions:
                    cometd_client.meta_subscriptions.remove(cometd_msg["subscription"])
                response.append(
                    {
                        "id": msgid,
                        "channel": channel,
                        "clientId": clientid,
                        "successful": True,
                        "subscription": cometd_msg["subscription"],
                    }
                )
            elif channel == "/slim/subscribe":
                # ruff: noqa: E501
                # A request to execute & subscribe to some Logitech Media Server event
                # A valid /slim/subscribe message looks like this:
                # {
                #   channel  => '/slim/subscribe',
                #   id       => <unique id>,
                #   data     => {
                #     response => '/slim/serverstatus', # the channel all messages should be sent back on
                #     request  => [ '', [ 'serverstatus', 0, 50, 'subscribe:60' ],
                #     priority => <value>, # optional priority value, is passed-through with the response
                #   }
                response.append(
                    {
                        "id": msgid,
                        "channel": channel,
                        "clientId": clientid,
                        "successful": True,
                    }
                )
                cometd_client.slim_subscriptions[cometd_msg["data"]["response"]] = cometd_msg
                # Return one-off result now, rest is handled by the subscription logic
                self._handle_cometd_request(cometd_client, cometd_msg)

            elif channel == "/slim/unsubscribe":
                # A request to unsubscribe from a Logitech Media Server event, this is not the same as /meta/unsubscribe
                # A valid /slim/unsubscribe message looks like this:
                # {
                #   channel  => '/slim/unsubscribe',
                #   data     => {
                #     unsubscribe => '/slim/serverstatus',
                #   }
                response.append(
                    {
                        "id": msgid,
                        "channel": channel,
                        "clientId": clientid,
                        "successful": True,
                    }
                )
                cometd_client.slim_subscriptions.pop(cometd_msg["data"]["unsubscribe"], None)

            elif channel == "/slim/request":
                # A request to execute a one-time Logitech Media Server event
                # A valid /slim/request message looks like this:
                # {
                #   channel  => '/slim/request',
                #   id       => <unique id>, (optional)
                #   data     => {
                #     response => '/slim/<clientId>/request',
                #     request  => [ '', [ 'menu', 0, 100, ],
                #     priority => <value>, # optional priority value, is passed-through with the response
                #   }
                if not msgid:
                    # If the caller does not want the response, id will be undef
                    logger.debug("Not sending response to request, caller does not want it")
                else:
                    # This response is optional, but we do it anyway
                    response.append(
                        {
                            "id": msgid,
                            "channel": channel,
                            "clientId": clientid,
                            "successful": True,
                        }
                    )
                    self._handle_cometd_request(cometd_client, cometd_msg)
            else:
                logger.warning("Unhandled channel %s", channel)
                # always reply with the (default) response to every message
                response.append(
                    {
                        "channel": channel,
                        "id": msgid,
                        "clientId": clientid,
                        "successful": True,
                    }
                )
        # append any remaining messages from the queue
        while True:
            try:
                msg = cometd_client.queue.get_nowait()
                response.append(msg)
            except asyncio.QueueEmpty:
                break
        # send response
        headers = {
            "Server": "Logitech Media Server (7.9.9 - 1667251155)",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Expires": "-1",
            "Connection": "keep-alive",
        }
        # regular command/handshake messages are just replied and connection closed
        if not streaming:
            return await self.send_json_response(writer, data=response, headers=headers)

        # streaming mode: send messages from the queue to the client
        # the subscription connection is kept open and events are streamed to the client
        headers.update({"Content-Type": "application/json", "Transfer-Encoding": "chunked"})
        # send headers only
        await self.send_status_response(writer, 200, headers=headers)
        data = json.dumps(response)
        writer.write(f"{hex(len(data)).replace('0x', '')}\r\n{data}\r\n".encode())
        await writer.drain()

        # keep delivering messages to the client until it disconnects
        # keep sending messages/events from the client's queue
        while not writer.is_closing():
            # make sure we always send an array of messages
            response = [await cometd_client.queue.get()]
            data = json.dumps(response)
            try:
                writer.write(f"{hex(len(data)).replace('0x', '')}\r\n{data}\r\n".encode())
                await writer.drain()
            except ConnectionResetError:
                break
            cometd_client.last_seen = int(time.time())

    async def _handle_command(self, params: tuple[str, list[str | int]]) -> Any:
        """Handle command for either JSON or CometD request."""
        # Slim request handler
        # {"method":"slim.request","id":1,"params":["aa:aa:ca:5a:94:4c",["status","-", 2, "tags:xcfldatgrKN"]]}
        self.logger.debug(
            "Handling request: %s",
            str(params),
        )
        player_id = params[0]
        command = str(params[1][0])
        args, kwargs = parse_args(params[1][1:])
        if player_id and "seq_no" in kwargs and (player := self.server.get_player(player_id)):
            player.extra_data["seq_no"] = int(kwargs["seq_no"])
        if handler := getattr(self, f"_handle_{command}", None):
            # run handler for command
            with suppress(NotImplementedError):
                cmd_result = handler(player_id, *args, **kwargs)
                if asyncio.iscoroutine(cmd_result):
                    cmd_result = await cmd_result
                if cmd_result is None:
                    cmd_result = {}
                elif not isinstance(cmd_result, dict):
                    # individual values are returned with underscore ?!
                    cmd_result = {f"_{command}": cmd_result}
                return cmd_result
        # no handler found, forward as event
        if player := self.server.get_player(player_id):
            args_str = " ".join([command] + [str(x) for x in args])
            player.callback(EventType.PLAYER_CLI_EVENT, player, args_str)
        else:
            self.logger.warning("No handler for %s", command)
        return None

    def _handle_cometd_request(self, client: CometDClient, cometd_request: dict[str, Any]) -> None:
        """Handle request for CometD client (and put result on client queue)."""

        async def _handle() -> None:
            result = await self._handle_command(cometd_request["data"]["request"])
            await client.queue.put(
                {
                    "channel": cometd_request["data"]["response"],
                    "id": cometd_request["id"],
                    "data": result,
                    "ext": {"priority": cometd_request["data"].get("priority")},
                }
            )

        asyncio.create_task(_handle())

    def _handle_players(
        self,
        player_id: str,
        start_index: int | str = 0,
        limit: int = 999,
        **kwargs,
    ) -> PlayersResponse:
        """Handle players command."""
        players: list[PlayerItem] = []
        for index, player in enumerate(self.server.players):
            if not isinstance(start_index, int):
                start_index = 0
            if isinstance(start_index, int) and index < start_index:
                continue
            if len(players) > limit:
                break
            players.append(create_player_item(start_index + index, player))
        return PlayersResponse(count=len(players), players_loop=players)

    async def _handle_status(
        self,
        player_id: str,
        offset: int | str = "-",
        limit: int = 2,
        menu: str = "",
        useContextMenu: int | bool = False,  # noqa: N803
        tags: str = "xcfldatgrKN",
        **kwargs,
    ) -> PlayerStatusResponse:
        """Handle player status command."""
        player = self.server.get_player(player_id)
        if player is None:
            return None
        playlist_items: list[MediaDetails] = []
        if player.previous_media:
            playlist_items.append(player.previous_media)
        if player.current_media:
            playlist_items.append(player.current_media)
        if player.next_media:
            playlist_items.append(player.next_media)
        # base details
        result = {
            "player_name": player.name,
            "player_connected": int(player.connected),
            "player_needs_upgrade": False,
            "player_is_upgrading": False,
            "power": int(player.powered),
            "signalstrength": 0,
            "waitingToPlay": 0,  # TODO?
        }
        # additional details if player powered
        if player.powered:
            result = {
                **result,
                "mode": PLAYMODE_MAP[player.state],
                "remote": 1,
                "current_title": self.server.name,
                "time": int(player.elapsed_seconds),
                "rate": 1,
                "duration": 0,
                "sleep": 0,
                "will_sleep_in": 0,
                "sync_master": "",
                "sync_slaves": "",
                "mixer volume": player.volume_level,
                "playlist repeat": "none",
                "playlist shuffle": 0,
                "playlist_timestamp": 0,
                "playlist_cur_index": 1,
                "playlist_tracks": len(playlist_items),
                "seq_no": player.extra_data.get("seq_no", 0),
                "player_ip": player.device_address,
                "digital_volume_control": 1,
                "can_seek": 1,
                "playlist mode": "off",
                "playlist_loop": [
                    playlist_item_from_media_details(index, item)
                    for index, item in enumerate(playlist_items)
                ],
            }
        # additional details if menu requested
        if menu == "menu":
            # in menu-mode the regular playlist_loop is replaced by item_loop
            result.pop("playlist_loop", None)
            presets = await self._get_preset_items(player_id)
            preset_data: list[dict] = []
            preset_loop: list[int] = []
            for _, media_item in presets:
                preset_data.append(
                    {
                        "URL": media_item["params"]["uri"],
                        "text": media_item["track"],
                        "type": "audio",
                    }
                )
                preset_loop.append(1)
            while len(preset_loop) < 10:
                preset_data.append({})
                preset_loop.append(0)
            result = {
                **result,
                "alarm_state": "none",
                "alarm_snooze_seconds": 540,
                "alarm_timeout_seconds": 3600,
                "count": len(playlist_items),
                "offset": offset,
                "base": {
                    "actions": {
                        "more": {
                            "itemsParams": "params",
                            "window": {"isContextMenu": 1},
                            "cmd": ["contextmenu"],
                            "player": 0,
                            "params": {"context": "playlist", "menu": "track"},
                        }
                    }
                },
                "preset_loop": preset_loop,
                "preset_data": preset_data,
                "item_loop": [
                    menu_item_from_media_item(
                        item,
                    )
                    for item in (player.previous_media, player.current_media, player.next_media)
                    if item
                ],
            }
        # additional details if contextmenu requested
        if bool(useContextMenu):
            result = {
                **result,
                # TODO ?!,
            }

        return result

    async def _handle_serverstatus(
        self,
        player_id: str,
        start_index: int = 0,
        limit: int = 2,
        **kwargs,
    ) -> ServerStatusResponse:
        """Handle server status command."""
        if start_index == "-":
            start_index = 0
        players: list[PlayerItem] = []
        for index, player in enumerate(self.server.players):
            if isinstance(start_index, int) and index < start_index:
                continue
            if len(players) > limit:
                break
            players.append(create_player_item(start_index + index, player))
        return ServerStatusResponse(
            {
                "httpport": self.cli_port_json,
                "ip": self.server.ip_address,
                "version": "7.999.999",
                "uuid": "aioslimproto",
                # TODO: set these vars ?
                "info total duration": 0,
                "info total genres": 0,
                "sn player count": 0,
                "lastscan": 1685548099,
                "info total albums": 0,
                "info total songs": 0,
                "info total artists": 0,
                "players_loop": players,
                "player count": len(players),
                "other player count": 0,
                "other_players_loop": [],
            }
        )

    async def _handle_firmwareupgrade(
        self,
        player_id: str,
        *args,
        **kwargs,
    ) -> ServerStatusResponse:
        """Handle firmwareupgrade command."""
        return {
            "firmwareUpgrade": 0,
            "relativeFirmwareUrl": "/firmware/baby_7.7.3_r16676.bin",
        }

    async def _handle_artworkspec(
        self,
        player_id: str,
        *args,
        **kwargs,
    ) -> ServerStatusResponse:
        """Handle firmwareupgrade command."""
        # https://github.com/Logitech/slimserver/blob/e9c2f88e7ca60b3648b66116240f3f5fe6ca3188/Slim/Control/Commands.pm#L224
        return None

    async def _handle_mixer(
        self,
        player_id: str,
        subcommand: str,
        *args,
        **kwargs,
    ) -> int | None:
        """Handle player mixer command."""
        arg = args[0] if args else "?"
        player = self.server.get_player(player_id)
        if not player:
            return
        # <playerid> mixer volume <0 .. 100|-100 .. +100|?>
        if subcommand == "volume" and isinstance(arg, int):
            if "seq_no" in kwargs:
                # handle a (jive based) squeezebox that already executed the command
                # itself and just reports the new state
                player.volume_level = arg
            else:
                await player.volume_set(arg)
            return None
        if subcommand == "volume" and arg == "?":
            return player.volume_level
        if subcommand == "volume" and "+" in arg:
            volume_level = min(100, player.volume_level + int(arg.split("+")[1]))
            await player.volume_set(volume_level)
            return None
        if subcommand == "volume" and "-" in arg:
            volume_level = max(0, player.volume_level - int(arg.split("-")[1]))
            await player.volume_set(volume_level)
            return None

        # <playerid> mixer muting <0|1|toggle|?|>
        if subcommand == "muting" and isinstance(arg, int):
            await player.mute(bool(arg))
            return None
        if subcommand == "muting" and arg == "toggle":
            await player.mute(not player.muted)
            return None
        if subcommand == "muting":
            return int(player.muted)
        raise NotImplementedError(f"No handler for mixer/{subcommand}")

    def _handle_time(self, player_id: str, number: str | int) -> int | None:
        """Handle player `time` command."""
        # <playerid> time <number|-number|+number|?>
        # The "time" command allows you to query the current number of seconds that the
        # current song has been playing by passing in a "?".
        # You may jump to a particular position in a song by specifying a number of seconds
        # to seek to. You may also jump to a relative position within a song by putting an
        # explicit "-" or "+" character before a number of seconds you would like to seek.
        player = self.server.get_player(player_id)
        if not player:
            return
        if number == "?":
            return int(player.elapsed_seconds)
        raise NotImplementedError("No handler for seek")

    async def _handle_power(self, player_id: str, value: str | int, *args, **kwargs) -> int | None:
        """Handle player `time` command."""
        # <playerid> power <0|1|?|>
        # The "power" command turns the player on or off.
        # Use 0 to turn off, 1 to turn on, ? to query and
        # no parameter to toggle the power state of the player.
        player = self.server.get_player(player_id)
        if not player:
            return None
        if value == "?":
            return int(player.powered)
        if "seq_no" in kwargs:
            # handle a (jive based) squeezebox that already executed the command
            # itself and just reports the new state
            player.powered = bool(value)
            return None
        await player.power(bool(value))
        return None

    async def _handle_play(
        self,
        player_id: str,
        *args,
        **kwargs,
    ) -> None:
        """Handle player `play` command."""
        if player := self.server.get_player(player_id):
            await player.play()

    async def _handle_stop(
        self,
        player_id: str,
        *args,
        **kwargs,
    ) -> None:
        """Handle player `stop` command."""
        if player := self.server.get_player(player_id):
            await player.stop()

    async def _handle_pause(
        self,
        player_id: str,
        force: int = 0,
        *args,
        **kwargs,
    ) -> None:
        """Handle player `stop` command."""
        if player := self.server.get_player(player_id):
            if player.state == PlayerState.PLAYING:
                await player.pause()
            else:
                await player.play()

    async def _handle_mode(
        self,
        player_id: str,
        subcommand: str,
        *args,
        **kwargs,
    ) -> None:
        """Handle player 'mode' command."""
        if subcommand == "play":
            return await self._handle_play(player_id, *args, **kwargs)
        if subcommand == "pause":
            return await self._handle_pause(player_id, *args, **kwargs)
        if subcommand == "stop":
            return await self._handle_stop(player_id, *args, **kwargs)

        raise NotImplementedError(f"No handler for mode/{subcommand}")

    async def _handle_button(
        self,
        player_id: str,
        subcommand: str,
        *args,
        **kwargs,
    ) -> None:
        """Handle player 'button' command."""
        player = self.server.get_player(player_id)
        if not player:
            return None
        if subcommand == "volup":
            await player.volume_up()
            return None
        if subcommand == "voldown":
            await player.volume_down()
            return None
        if subcommand == "power":
            await player.power(not player.powered)
            return None
        raise NotImplementedError(f"No handler for button/{subcommand}")

    async def _handle_menu(
        self,
        player_id: str,
        offset: int = 0,
        limit: int = 10,
        **kwargs,
    ) -> dict[str, Any]:
        """Handle menu request from CLI."""
        return {
            "item_loop": [],
            "offset": offset,
            "count": 0,
        }

    def _handle_menustatus(
        self,
        player_id: str,
        *args,
        **kwargs,
    ) -> dict[str, Any]:
        """Handle menustatus request from CLI."""
        return None

    def _handle_displaystatus(
        self,
        player_id: str,
        *args,
        **kwargs,
    ) -> dict[str, Any]:
        """Handle displaystatus request from CLI."""
        return None

    def _handle_date(
        self,
        player_id: str,
        *args,
        **kwargs,
    ) -> dict[str, Any]:
        """Handle date request from CLI."""
        return {"date_epoch": int(time.time()), "date": "0000-00-00T00:00:00+00:00"}

    async def _on_player_event(self, event: SlimEvent) -> None:
        """Forward player events."""
        if not event.player_id:
            return
        for client in self._cometd_clients.values():
            if sub := client.slim_subscriptions.get(
                f"/{client.client_id}/slim/playerstatus/{event.player_id}"
            ):
                self._handle_cometd_request(client, sub)

    async def _do_periodic(self) -> None:
        """Execute periodic sending of state and cleanup."""
        while True:
            # cleanup orphaned clients
            disconnected_clients = set()
            for cometd_client in self._cometd_clients.values():
                if (time.time() - cometd_client.last_seen) > 80:
                    disconnected_clients.add(cometd_client.client_id)
                    continue
            for clientid in disconnected_clients:
                client = self._cometd_clients.pop(clientid)
                empty_queue(client.queue)
                self.logger.debug("Cleaned up disconnected CometD Client: %s", clientid)
            # handle client subscriptions
            for cometd_client in self._cometd_clients.values():
                for sub in cometd_client.slim_subscriptions.values():
                    self._handle_cometd_request(cometd_client, sub)

            await asyncio.sleep(60)


def dict_to_strings(source: dict) -> list[str]:
    """Convert dict to key:value strings (used in slimproto cli)."""
    result: list[str] = []

    for key, value in source.items():
        if value in (None, ""):
            continue
        if isinstance(value, list):
            for subval in value:
                if isinstance(subval, dict):
                    result += dict_to_strings(subval)
                else:
                    result.append(str(subval))
        elif isinstance(value, dict):
            result += dict_to_strings(value)
        else:
            result.append(f"{key}:{value!s}")
    return result


def menu_item_from_media_item(
    media_item: MediaDetails, include_actions: bool = False
) -> PlaylistItem:
    """Parse (menu) MediaItem from MA MediaItem."""
    go_action = {
        "cmd": ["playlistcontrol"],
        "itemsParams": "commonParams",
        "params": {"uri": media_item.url, "cmd": "play"},
        "player": 0,
        "nextWindow": "nowPlaying",
    }
    details = SlimMenuItem(
        track=media_item.metadata.get("title", media_item.url),
        album=media_item.metadata.get("album", ""),
        trackType="radio",
        icon=media_item.metadata.get("image_url", ""),
        artist=media_item.metadata.get("artist", ""),
        text=media_item.metadata.get("title", media_item.url),
        params={
            "track_id": media_item.metadata.get("item_id", media_item.url),
            "item_id": media_item.metadata.get("item_id", media_item.url),
            "uri": media_item.url,
        },
        type="track",
    )
    # optionally include actions
    if include_actions:
        details["actions"] = {
            "go": go_action,
            "add": {
                "player": 0,
                "itemsParams": "commonParams",
                "params": {"uri": media_item.url, "cmd": "add"},
                "cmd": ["playlistcontrol"],
                "nextWindow": "refresh",
            },
            "more": {
                "player": 0,
                "itemsParams": "commonParams",
                "params": {"uri": media_item.url, "cmd": "add"},
                "cmd": ["playlistcontrol"],
                "nextWindow": "refresh",
            },
            "play": {
                "cmd": ["playlistcontrol"],
                "itemsParams": "commonParams",
                "params": {
                    "uri": media_item.url,
                    "cmd": "play",
                },
                "player": 0,
                "nextWindow": "nowPlaying",
            },
            "play-hold": {
                "cmd": ["playlistcontrol"],
                "itemsParams": "commonParams",
                "params": {"uri": media_item.url, "cmd": "load"},
                "player": 0,
                "nextWindow": "nowPlaying",
            },
            "add-hold": {
                "itemsParams": "commonParams",
                "params": {"uri": media_item.url, "cmd": "insert"},
                "player": 0,
                "cmd": ["playlistcontrol"],
                "nextWindow": "refresh",
            },
        }
    details["style"] = "itemplay"
    details["nextWindow"] = "nowPlaying"
    return details


def playlist_item_from_media_details(index: int, media: MediaDetails) -> PlaylistItem:
    """Parse PlaylistItem for the Json RPC interface from MediaDetails."""
    return {
        "playlist index": index,
        "id": "-187651250107376",
        "title": media.metadata.get("title", media.url),
        "artist": media.metadata.get("artist", ""),
        "album": media.metadata.get("album", ""),
        "remote": 1,
        "artwork_url": media.metadata.get("image_url", ""),
        "coverid": "-187651250107376",
        "duration": 0,
        "bitrate": "",
        "samplerate": "",
        "samplesize": "",
    }


def create_player_item(playerindex: int, player: SlimClient) -> PlayerItem:
    """Parse PlayerItem for the Json RPC interface from SlimClient."""
    return {
        "playerindex": str(playerindex),
        "playerid": player.player_id,
        "name": player.name,
        "modelname": player.device_model,
        "connected": int(player.connected),
        "isplaying": 1 if player.state == PlayerState.PLAYING else 0,
        "power": int(player.powered),
        "model": player.device_type,
        "canpoweroff": 1,
        "firmware": "unknown",
        "isplayer": 1,
        "displaytype": "none",
        "uuid": player.extra_data.get("uuid"),
        "seq_no": str(player.extra_data.get("seq_no", 0)),
        "ip": player.device_address,
    }
