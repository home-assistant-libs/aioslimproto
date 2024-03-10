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
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
import json
import time
from typing import TYPE_CHECKING, Any
import urllib.parse
from uuid import uuid1

from aiohttp import web

from aioslimproto.client import PlayerState
from aioslimproto.util import empty_queue, select_free_port

from .models import (
    PLAYMODE_MAP,
    CometDResponse,
    CommandErrorMessage,
    CommandMessage,
    CommandResultMessage,
    EventType,
    MediaDetails,
    MediaMetadata,
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

# ruff: noqa: ARG004,ARG002,FBT001,FBT002,PLR0912,RUF006


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
    _cli_server: asyncio.Server | None = None

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
        self._apprunner: web.AppRunner | None = None
        self._webapp: web.Application | None = None
        self._tcp_site: web.TCPSite | None = None

    async def start(self) -> None:
        """Start running the server(s)."""
        # if port is specified as 0, auto select a free port for the cli/json interface
        if self.cli_port == 0:
            self.cli_port = await select_free_port(9090, 9190)
        if self.cli_port_json == 0:
            self.cli_port_json = await select_free_port(9000, 9089)
        if self.cli_port is not None:
            self.logger.info(
                "Starting (legacy/telnet) SLIMProto CLI on port %s",
                self.cli_port,
            )
            self._cli_server = await asyncio.start_server(
                self._handle_cli_client,
                "0.0.0.0",  # noqa: S104
                self.cli_port,
            )
        if self.cli_port_json is not None:
            self.logger.info(
                "Starting SLIMProto JSON RPC CLI on port %s",
                self.cli_port_json,
            )
            self._webapp = web.Application(
                logger=self.logger,
            )
            self._apprunner = web.AppRunner(self._webapp, access_log=None)
            self._webapp.router.add_route(
                "*",
                "/jsonrpc.js",
                self._handle_jsonrpc_client,
            )
            self._webapp.router.add_route("*", "/cometd", self._handle_cometd_client)
            await self._apprunner.setup()
            # set host to None to bind to all addresses on both IPv4 and IPv6
            self._tcp_site = web.TCPSite(
                self._apprunner,
                host=None,
                port=self.cli_port_json,
                shutdown_timeout=10,
            )
            await self._tcp_site.start()
        # setup subscriptions
        self._unsub_callback = self.server.subscribe(
            self._on_player_event,
            (
                EventType.PLAYER_UPDATED,
                EventType.PLAYER_CONNECTED,
                EventType.PLAYER_PRESETS_UPDATED,
            ),
        )
        self._periodic_task = asyncio.create_task(self._do_periodic())

    async def stop(self) -> None:
        """Stop running the server(s)."""
        # stop/clean json-rpc webserver
        if self._tcp_site:
            await self._tcp_site.stop()
            self._tcp_site = None
        if self._apprunner:
            await self._apprunner.cleanup()
            self._apprunner = None
        if self._webapp:
            await self._webapp.shutdown()
            await self._webapp.cleanup()
            self._webapp = None
        # stop cli server
        if self._cli_server:
            self._cli_server.close()
            self._cli_server = None
        # cleanup callbacks and tasks
        if self._unsub_callback:
            self._unsub_callback()
            self._unsub_callback = None
        if self._periodic_task:
            self._periodic_task.cancel()
            self._periodic_task = None

    async def _handle_cli_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
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
                        result_str = " ".join(
                            urllib.parse.quote(x) for x in result_parts
                        )
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
        except Exception as err:  # noqa: BLE001
            self.logger.debug("Error handling CLI command", exc_info=err)
        finally:
            self.logger.debug("Client disconnected from Telnet CLI")

    async def _handle_jsonrpc_client(self, request: web.Request) -> web.Response:
        """Handle request on JSON-RPC endpoint."""
        command_msg: CommandMessage = await request.json()
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
        return web.json_response(result)

    async def _handle_cometd_client(self, request: web.Request) -> web.Response:
        """
        Handle CometD request on the json CLI.

        https://github.com/Logitech/slimserver/blob/public/8.4/Slim/Web/Cometd.pm
        """
        logger = self.logger.getChild("cometd")
        # ruff: noqa: PLR0915
        clientid: str = ""
        response = []
        streaming = False
        json_msg: list[dict[str, Any]] = await request.json()
        # cometd message is an array of commands/messages
        for cometd_msg in json_msg:
            channel = cometd_msg.get("channel")
            # try to figure out clientid
            if not clientid:
                clientid = cometd_msg.get("clientId")
            if not clientid and channel == "/meta/handshake":
                # generate new clientid
                clientid = uuid1().hex
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
            logger.debug(
                "Incoming message for channel '%s' - clientid: %s",
                channel,
                clientid,
            )

            # messageid is optional but if provided we must pass it along
            msgid = cometd_msg.get("id", "")

            if clientid not in self._cometd_clients:
                # If a client sends any request and we do not have a valid clid record
                # because the streaming connection has been lost for example, re-handshake them
                return web.json_response(
                    [
                        {
                            "id": msgid,
                            "channel": channel,
                            "clientId": None,
                            "successful": False,
                            "timestamp": time.strftime(
                                "%a, %d %b %Y %H:%M:%S %Z",
                                time.gmtime(),
                            ),
                            "error": "invalid clientId",
                            "advice": {
                                "reconnect": "handshake",
                                "interval": 0,
                            },
                        },
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
                    },
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
                        "timestamp": time.strftime(
                            "%a, %d %b %Y %H:%M:%S %Z",
                            time.gmtime(),
                        ),
                        "advice": {
                            # update interval for streaming mode
                            "interval": 5000 if streaming else 0,
                        },
                    },
                )
                # TODO: do we want to implement long-polling support too ?
                # https://github.com/Logitech/slimserver/blob/d9ebda7ebac41e82f1809dd85b0e4446e0c9be36/Slim/Web/Cometd.pm#L292

            elif channel == "/meta/disconnect":
                # disconnect message
                logger.debug("CometD Client disconnected: %s", clientid)
                self._cometd_clients.pop(clientid)
                return web.json_response(
                    [
                        {
                            "id": msgid,
                            "channel": channel,
                            "clientId": clientid,
                            "successful": True,
                            "timestamp": time.strftime(
                                "%a, %d %b %Y %H:%M:%S %Z",
                                time.gmtime(),
                            ),
                        },
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
                    },
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
                    },
                )
            elif channel == "/slim/subscribe":
                # ruff: noqa: E501, ERA001
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
                    },
                )
                cometd_client.slim_subscriptions[cometd_msg["data"]["response"]] = (
                    cometd_msg
                )
                # Return one-off result now, rest is handled by the subscription logic
                self._handle_cometd_client_request(cometd_client, cometd_msg)

            elif channel == "/slim/unsubscribe":
                # ruff: noqa: E501, ERA001
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
                    },
                )
                cometd_client.slim_subscriptions.pop(
                    cometd_msg["data"]["unsubscribe"],
                    None,
                )

            elif channel == "/slim/request":
                # ruff: noqa: E501, ERA001
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
                    logger.debug(
                        "Not sending response to request, caller does not want it",
                    )
                else:
                    # This response is optional, but we do it anyway
                    response.append(
                        {
                            "id": msgid,
                            "channel": channel,
                            "clientId": clientid,
                            "successful": True,
                        },
                    )
                    self._handle_cometd_client_request(cometd_client, cometd_msg)
            else:
                logger.warning("Unhandled channel %s", channel)
                # always reply with the (default) response to every message
                response.append(
                    {
                        "channel": channel,
                        "id": msgid,
                        "clientId": clientid,
                        "successful": True,
                    },
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
            return web.json_response(response, headers=headers)

        # streaming mode: send messages from the queue to the client
        # the subscription connection is kept open and events are streamed to the client
        headers.update({"Content-Type": "application/json"})
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers=headers,
        )
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        chunk = json.dumps(response).encode("utf8")
        await resp.write(chunk)

        # keep delivering messages to the client until it disconnects
        # keep sending messages/events from the client's queue
        while True:
            # make sure we always send an array of messages
            msg = [await cometd_client.queue.get()]
            try:
                chunk = json.dumps(msg).encode("utf8")
                await resp.write(chunk)
                cometd_client.last_seen = int(time.time())
            except ConnectionResetError:
                break
        return resp

    def _handle_cometd_client_request(
        self,
        client: CometDClient,
        cometd_request: dict[str, Any],
    ) -> None:
        """
        Handle CometD request on the json CLI.

        https://github.com/Logitech/slimserver/blob/public/8.4/Slim/Web/Cometd.pm
        """

        async def _handle() -> None:
            result = await self._handle_command(cometd_request["data"]["request"])
            await client.queue.put(
                {
                    "channel": cometd_request["data"]["response"],
                    "id": cometd_request["id"],
                    "data": result,
                    "ext": {"priority": cometd_request["data"].get("priority")},
                },
            )

        asyncio.create_task(_handle())

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
        if (
            player_id
            and "seq_no" in kwargs
            and (player := self.server.get_player(player_id))
        ):
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
            "waitingToPlay": 0,
        }
        cur_item = playlist_items[0] if playlist_items else None
        # additional details if player powered
        if player.powered:
            result = {
                **result,
                "mode": PLAYMODE_MAP[player.state],
                "remote": 1,
                "current_title": self.server.name,
                "time": int(player.elapsed_seconds),
                "duration": cur_item.metadata.get("duration", 0) if cur_item else 0,
                "sync_master": "",
                "sync_slaves": "",
                "mixer volume": player.volume_level,
                "player_ip": player.device_address,
                "playlist_cur_index": 0,
                "playlist_tracks": len(playlist_items),
                "playlist_loop": [
                    playlist_item_from_media_details(index, item)
                    for index, item in enumerate(playlist_items)
                ],
                **player.extra_data,
            }

        # additional details if menu requested
        if menu == "menu":
            # in menu-mode the regular playlist_loop is replaced by item_loop
            result.pop("playlist_loop", None)
            preset_data: list[dict] = []
            preset_loop: list[int] = []
            for index, preset in enumerate(player.presets):
                preset_data.append(
                    {
                        "URL": str(index),
                        "text": preset.text,
                        "type": "audio",
                    },
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
                        },
                    },
                },
                "preset_loop": preset_loop,
                "preset_data": preset_data,
                "item_loop": [
                    menu_item_from_media_details(
                        item,
                    )
                    for item in (player.current_media, player.next_media)
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
            },
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
            return None
        # <playerid> mixer volume <0 .. 100|-100 .. +100|?>
        if subcommand == "volume" and isinstance(arg, int):
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
            return None
        if number == "?":
            return int(player.elapsed_seconds)
        raise NotImplementedError("No handler for seek")

    async def _handle_power(
        self,
        player_id: str,
        value: str | int,
        *args,
        **kwargs,
    ) -> int | None:
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
            return
        if subcommand == "volup":
            await player.volume_up()
            return
        if subcommand == "voldown":
            await player.volume_down()
            return
        if subcommand == "power":
            await player.power(not player.powered)
            return
        if subcommand == "jump_fwd" and player.next_media:
            await player.next()
            return
        if subcommand.startswith("preset_") and subcommand.endswith(".single"):
            # only handle http-based presets, ignore/forward all other
            preset_id = subcommand.split("preset_")[1].split(".")[0]
            preset_index = int(preset_id) - 1
            if len(player.presets) >= preset_index + 1:
                preset = player.presets[preset_index]
                if preset.uri.startswith("http"):
                    await player.play_url(
                        preset.uri,
                        metadata=MediaMetadata(
                            title=preset.text,
                            image_url=preset.icon,
                        ),
                    )

        raise NotImplementedError(f"No handler for button/{subcommand}")

    async def _handle_playlist(
        self,
        player_id: str,
        subcommand: str,
        *args,
        **kwargs,
    ) -> int | None:
        """Handle player `playlist` command."""
        # <playerid> playlist index <index|+index|-index|?> <fadeInSecs>
        arg = args[0] if args else "?"
        player = self.server.get_player(player_id)
        if not player:
            return None
        # we only handle playlist index +1 - the rest is forwarded
        if subcommand == "index" and arg in (1, "1", "+1") and player.next_media:
            await player.next()
            return None
        raise NotImplementedError(f"No handler for playlist/{subcommand}")

    async def _handle_menu(
        self,
        player_id: str,
        offset: int = 0,
        limit: int = 10,
        **kwargs,
    ) -> dict[str, Any]:
        """Handle menu request from CLI."""
        menu_items = []
        if player := self.server.get_player(player_id):
            for index, preset in enumerate(player.presets):
                preset_id = f"preset_{index+1}"
                menu_items.append(
                    {
                        "id": preset_id,
                        "icon": preset.icon,
                        "text": preset.text,
                        "homeMenuText": preset.text,
                        "weight": 35,
                        "node": "myMusic",
                        "style": "itemplay",
                        "nextWindow": "nowPlaying",
                        "actions": {
                            "go": {
                                "cmd": ["button", f"{preset_id}.single"],
                                "itemsParams": "commonParams",
                                "params": {},
                                "player": 0,
                                "nextWindow": "nowPlaying",
                            },
                            "add": {
                                "player": 0,
                                "itemsParams": "commonParams",
                                "params": {"uri": preset.uri, "cmd": "add"},
                                "cmd": ["playlistcontrol"],
                                "nextWindow": "refresh",
                            },
                            "more": {
                                "player": 0,
                                "itemsParams": "commonParams",
                                "params": {"uri": preset.uri, "cmd": "add"},
                                "cmd": ["playlistcontrol"],
                                "nextWindow": "refresh",
                            },
                            "play": {
                                "cmd": ["playlistcontrol"],
                                "itemsParams": "commonParams",
                                "params": {
                                    "uri": preset.uri,
                                    "cmd": "play",
                                },
                                "player": 0,
                                "nextWindow": "nowPlaying",
                            },
                            "play-hold": {
                                "cmd": ["playlistcontrol"],
                                "itemsParams": "commonParams",
                                "params": {"uri": preset.uri, "cmd": "load"},
                                "player": 0,
                                "nextWindow": "nowPlaying",
                            },
                            "add-hold": {
                                "itemsParams": "commonParams",
                                "params": {"uri": preset.uri, "cmd": "insert"},
                                "player": 0,
                                "cmd": ["playlistcontrol"],
                                "nextWindow": "refresh",
                            },
                        },
                    },
                )
        return {
            "item_loop": menu_items[offset:limit],
            "offset": offset,
            "count": len(menu_items[offset:limit]),
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
        client = next(
            (
                x
                for x in self._cometd_clients.values()
                if x.player_id == event.player_id
            ),
            None,
        )
        if not client:
            return
        # regular player updated (or connected) event, signal playerstatus
        if event.type in (EventType.PLAYER_CONNECTED, EventType.PLAYER_UPDATED):
            if sub := client.slim_subscriptions.get(
                f"/{client.client_id}/slim/playerstatus/{event.player_id}",
            ):
                self._handle_cometd_client_request(client, sub)
            if sub := client.slim_subscriptions.get(
                f"/{client.client_id}/slim/serverstatus",
            ):
                self._handle_cometd_client_request(client, sub)
            return
        # player presets updated, signal menustatus event
        if event.type == EventType.PLAYER_PRESETS_UPDATED and (
            sub := client.slim_subscriptions.get(
                f"/{client.client_id}/slim/menustatus/{event.player_id}",
            )
        ):
            menu = await self._handle_menu(event.player_id)
            await client.queue.put(
                {
                    "channel": sub["data"]["response"],
                    "id": sub["id"],
                    "data": [
                        event.player_id,
                        menu["item_loop"],
                        "replace",
                        event.player_id,
                    ],
                    "ext": {"priority": sub["data"].get("priority")},
                },
            )

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
                    self._handle_cometd_client_request(cometd_client, sub)

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


def menu_item_from_media_details(
    media_item: MediaDetails,
    include_actions: bool = False,
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
        "url": media.url,
        "title": media.metadata.get("title", media.url),
        "artist": media.metadata.get("artist", ""),
        "album": media.metadata.get("album", ""),
        "remote": 1,
        "artwork_url": media.metadata.get("image_url", ""),
        "coverid": "-187651250107376",
        "duration": media.metadata.get("duration", ""),
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
        "uuid": player.extra_data["uuid"],
        "seq_no": str(player.extra_data["seq_no"]),
        "ip": player.device_address,
    }
