"""
Socketclient implementation for SLIMproto client (e.g. Squeezebox).

Large parts of this code are based on code by Doug Winter, all rights reserved.
https://github.com/winjer/squeal/blob/master/src/squeal/net/slimproto.py
"""

from __future__ import annotations

import asyncio
from asyncio import StreamReader, StreamWriter, create_task
from collections.abc import Callable
from datetime import datetime
import ipaddress
import logging
import socket
import struct
import time
from typing import Any
from urllib.parse import parse_qsl, urlparse

from async_timeout import timeout

from .const import (
    FALLBACK_CODECS,
    FALLBACK_MODEL,
    FALLBACK_SAMPLE_RATE,
    FALLLBACK_FIRMWARE,
    HEARTBEAT_INTERVAL,
)
from .display import SlimProtoDisplay
from .errors import UnsupportedContentType
from .models import (
    CODEC_MAPPING,
    DEVICE_TYPE,
    FORMAT_BYTE,
    PCM_SAMPLE_RATE,
    PCM_SAMPLE_SIZE,
    ButtonCode,
    EventType,
    MediaDetails,
    MediaMetadata,
    PlayerState,
    Preset,
    RemoteCode,
    TransitionType,
    VisualisationType,
)
from .util import parse_capabilities, parse_headers, parse_status
from .volume import SlimProtoVolume

# ruff: noqa: ARG002,FBT001,FBT002,RUF006


class SlimClient:
    """SLIMProto socket client."""

    def __init__(
        self,
        reader: StreamReader,
        writer: StreamWriter,
        callback: Callable[[SlimClient, EventType, Any], None],
    ) -> None:
        """Initialize the socket client."""
        self.callback = callback
        self.logger = logging.getLogger(__name__)
        self.volume_control = SlimProtoVolume()
        self.display_control = SlimProtoDisplay(self)
        # extra_data is used by the cli to exchange data
        # it will be sent as-is in in the players data
        self.extra_data: dict[str, Any] = {
            "can_seek": 0,
            "digital_volume_control": 1,
            "playlist_timestamp": int(time.time()),
            "playlist repeat": 0,
            "playlist shuffle": 0,
            "playlist mode": "off",
            "rate": 1,
            "seq_no": 0,
            "sleep": 0,
            "will_sleep_in": 0,
            "uuid": None,
        }
        self._reader = reader
        self._writer = writer
        self._player_id: str = ""
        self._device_type: str = ""
        self._capabilities: dict[str, str] = {}
        self._device_name: str = ""
        self._powered: bool = False
        self._muted: bool = False
        self._state = PlayerState.STOPPED
        self._jiffies: int = 0
        self._last_timestamp: float = 0
        self._elapsed_milliseconds: float = 0
        self._current_media: MediaDetails | None = None
        self._next_media: MediaDetails | None = None
        self._connected: bool = False
        self._last_heartbeat = 0
        self._auto_play: bool = False
        self._reader_task = create_task(self._socket_reader())
        self._heartbeat_task: asyncio.Task | None = None
        self._presets: list[Preset] = []

    def disconnect(self) -> None:
        """Disconnect and/or cleanup socket client."""
        self._connected = False
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()

        if self._writer.can_write_eof():
            self._writer.write_eof()
        if not self._writer.is_closing():
            self._writer.close()

    async def configure_display(
        self,
        width: int | None = None,
        visualisation: VisualisationType | None = None,
        disabled: bool | None = None,
    ) -> None:
        """Configure display settings for this client (optional feature)."""
        if width is not None:
            self.display_control.width = width
        if visualisation is not None:
            self.display_control.visualisation_type = visualisation
        if disabled is not None:
            self.display_control.disabled = disabled
        await self._render_display()

    @property
    def connected(self) -> bool:
        """Return connection state of the socket."""
        return self._connected

    @property
    def player_id(self) -> str:
        """Return mac address of the player (used as player id)."""
        return self._player_id

    @property
    def device_type(self) -> str:
        """Return device type of the player."""
        return self._device_type

    @property
    def device_model(self) -> str:
        """Return device model of the player."""
        return self._capabilities.get(
            "ModelName",
            self._capabilities.get("Model", FALLBACK_MODEL),
        )

    @property
    def max_sample_rate(self) -> int:
        """Return max sample rate supported by the player."""
        return self._capabilities.get("MaxSampleRate", FALLBACK_SAMPLE_RATE)

    @property
    def supported_codecs(self) -> list[str]:
        """Return supported codecs by the player."""
        return self._capabilities.get("SupportedCodecs", FALLBACK_CODECS)

    @property
    def firmware(self) -> str:
        """Return firmware version string for the player."""
        return self._capabilities.get("Firmware", FALLLBACK_FIRMWARE)

    @property
    def device_address(self) -> str:
        """Return device IP address of the player."""
        dev_address = self._writer.get_extra_info("peername")
        return dev_address[0] if dev_address else ""

    @property
    def name(self) -> str:
        """Return name of the player."""
        if self._device_name:
            return self._device_name
        return f"{self.device_type}: {self.player_id}"

    @property
    def volume_level(self) -> int:
        """Return current volume level of player."""
        return self.volume_control.volume

    @property
    def powered(self) -> bool:
        """Return current power state of player."""
        return self._powered

    @property
    def muted(self) -> bool:
        """Return current mute state of player."""
        return self._muted

    @property
    def state(self) -> PlayerState:
        """Return current state of player."""
        return self._state

    @property
    def elapsed_seconds(self) -> float:
        """Return elapsed_time of current playing track in (fractions of) seconds."""
        return self.elapsed_milliseconds / 1000

    @property
    def elapsed_milliseconds(self) -> int:
        """Return (realtime) elapsed time of current playing media in milliseconds."""
        if self.state != PlayerState.PLAYING:
            return self._elapsed_milliseconds
        # if the player is playing we return a very accurate timestamp
        # which in turn can be used by consumers to sync players etc.
        return self._elapsed_milliseconds + int(
            (time.time() - self._last_timestamp) * 1000,
        )

    @property
    def jiffies(self) -> int:
        """Return (realtime) epoch timestamp from player."""
        return self._jiffies + int((time.time() - self._last_timestamp) * 1000)

    @property
    def current_url(self) -> str | None:
        """Return currently playing url.

        NOTE: Deprecated, use current_media instead.
        """
        return self.current_media.url if self.current_media else None

    @property
    def current_media(self) -> MediaDetails | None:
        """Return the currently playing media(details)."""
        return self._current_media

    @property
    def next_media(self) -> MediaDetails | None:
        """Return the next/enqueued media(details), if any."""
        return self._next_media

    @property
    def presets(self) -> list[Preset]:
        """Get/set the player presets."""
        return self._presets

    @presets.setter
    def presets(self, presets: list[Preset]) -> None:
        """Get/set the player presets."""
        self._presets = presets[:9]
        self.callback(self, EventType.PLAYER_PRESETS_UPDATED)

    async def stop(self) -> None:
        """Send stop command to player."""
        if self._state == PlayerState.STOPPED:
            return
        await self._send_strm(b"q", flags=0)
        # some players do not update their state by event so we force it here
        self._state = PlayerState.STOPPED
        self.signal_update()
        await self._render_display("playback_stop")

    async def play(self) -> None:
        """Send play/unpause command to player."""
        if self._state != PlayerState.PAUSED:
            return
        await self._send_strm(b"u", flags=0)
        # some players do not update their state by event so we force it here
        self._state = PlayerState.PLAYING
        self.signal_update()

    async def pause(self) -> None:
        """Send pause command to player."""
        if self._state not in (PlayerState.PLAYING, PlayerState.BUFFERING):
            return
        await self._send_strm(b"p")
        # some players do not update their state by event so we force it here
        self._state = PlayerState.PAUSED
        self.signal_update()

    async def toggle_pause(self) -> None:
        """Toggle play/pause command."""
        if self.state == PlayerState.PLAYING:
            await self.pause()
        else:
            await self.play()

    async def power(self, powered: bool = True) -> None:
        """Send power command to player."""
        if self.powered == powered:
            return
        if not powered:
            await self.stop()
        power_int = 1 if powered else 0
        await self.send_frame(b"aude", struct.pack("2B", power_int, 1))
        self._powered = powered
        self.signal_update()
        await self._render_display()

    async def toggle_power(self) -> None:
        """Toggle power command."""
        await self.power(not self.powered)

    async def volume_set(self, volume_level: int) -> None:
        """Send new volume level command to player."""
        if volume_level == self.volume_control.volume:
            return
        self.volume_control.volume = volume_level
        old_gain = self.volume_control.old_gain()
        new_gain = self.volume_control.new_gain()
        await self.send_frame(
            b"audg",
            struct.pack("!LLBBLL", old_gain, old_gain, 1, 255, new_gain, new_gain),
        )
        self.signal_update()
        await self._render_display("show_volume")

    async def volume_up(self) -> None:
        """Send volume up command to player."""
        self.volume_control.increment()
        old_gain = self.volume_control.old_gain()
        new_gain = self.volume_control.new_gain()
        await self.send_frame(
            b"audg",
            struct.pack("!LLBBLL", old_gain, old_gain, 1, 255, new_gain, new_gain),
        )
        self.signal_update()
        await self._render_display("show_volume")

    async def volume_down(self) -> None:
        """Send volume down command to player."""
        self.volume_control.decrement()
        old_gain = self.volume_control.old_gain()
        new_gain = self.volume_control.new_gain()
        await self.send_frame(
            b"audg",
            struct.pack("!LLBBLL", old_gain, old_gain, 1, 255, new_gain, new_gain),
        )
        self.signal_update()
        await self._render_display("show_volume")

    async def mute(self, muted: bool = False) -> None:
        """Send mute command to player."""
        if self._muted == muted:
            return
        muted_int = 0 if muted else 1
        await self.send_frame(b"aude", struct.pack("2B", muted_int, 0))
        self._muted = muted
        self.signal_update()

    async def next(self) -> None:
        """Play next URL on the player (if a next url is enqueued)."""
        if not self._next_media:
            return
        await self.play_url(
            url=self._next_media.url,
            mime_type=self._next_media.mime_type,
            metadata=self._next_media.metadata,
            enqueue=False,
            autostart=True,
            send_flush=True,
        )

    async def play_url(
        self,
        url: str,
        mime_type: str | None = None,
        metadata: MediaMetadata | None = None,
        transition: TransitionType = TransitionType.NONE,
        transition_duration: int = 0,
        enqueue: bool = False,
        autostart: bool = True,
        send_flush: bool = True,
    ) -> None:
        """
        Request player to start playing a single url.

        Parameters:
        - url: the (http) URL to the media that needs to be played.
        - mime_type: optionally provide the mimetype,
          will be derived from url if omitted.
        - metadata: optionally provide metadata of the url that is going to be played.
        - transition: optionally specify a transition, such as fade-in.
        - transition_duration: optionally specify a transition duration.
        - enqueue: enqueue this url to play after the current URL finished.
        - autostart: advanced option to not auto start playback,
          but wait for the buffer to be full.
        - send_flush: advanced option to flush the buffer before playback.
        """
        self.logger.debug("play url (enqueue: %s): %s", enqueue, url)
        if not url.startswith("http"):
            raise UnsupportedContentType(f"Invalid URL: {url}")  # noqa: TRY003

        if send_flush:
            # flush buffers before playback of a new track
            await self._send_strm(b"f", autostart=b"0")
            await self._send_strm(b"q", flags=0)

        self._next_media = MediaDetails(
            url=url,
            mime_type=mime_type,
            metadata=metadata or {},
            transition=transition,
            transition_duration=transition_duration,
        )
        self.extra_data["playlist_timestamp"] = int(time.time())
        self.signal_update()
        if enqueue:
            return
        # power on if we're not already powered
        if not self._powered:
            await self.power(powered=True)
        # set state to buffering when we send the play request
        self._state = PlayerState.BUFFERING

        # extract host and port from uri
        parsed_uri = urlparse(url)
        scheme = parsed_uri.scheme
        host = parsed_uri.hostname
        port = parsed_uri.port
        path = parsed_uri.path
        if parsed_uri.query:
            path += f"?{parsed_uri.query}"

        ipaddr = socket.gethostbyname(host)

        if port is None and scheme == "https":
            port = 443
        elif port is None:
            port = 80

        if scheme == "https" and not self._capabilities.get("CanHTTPS"):
            # most stream urls are available on HTTP too, try to use that instead
            self.logger.warning(
                "HTTPS stream requested but player does not support HTTPS, "
                "trying HTTP instead but playback may fail.",
            )
            self._next_media.url = url.replace("https", "http")
            scheme = "http"
            port = 80

        if mime_type is None:
            # try to get the audio format from file extension
            for ext in (url[-3:], url.split(".")[-1]):
                mime = f"audio/{ext}"
                if mime in CODEC_MAPPING:
                    mime_type = mime
                    break

        codec_details = self._parse_codc(mime_type) if mime_type else b"?????"

        if port not in (80, 443, "80", "443"):
            host += f":{port}"
        httpreq = (
            b"GET %s HTTP/1.0\r\n"
            b"Host: %s\r\n"
            b"Connection: close\r\n"
            b"Accept: */*\r\n"
            b"Cache-Control: no-cache\r\n"
            b"User-Agent: VLC/3.0.9 LibVLC/3.0.9\r\n"
            b"Range: bytes=0-\r\n"
            b"\r\n" % (path.encode(), host.encode())
        )
        self._auto_play = autostart
        await self._send_strm(
            command=b"s",
            codec_details=codec_details,
            autostart=b"3" if autostart else b"0",
            server_port=port,
            server_ip=int(ipaddress.ip_address(ipaddr)),
            threshold=200,
            output_threshold=20,
            trans_duration=transition_duration,
            trans_type=transition.value,
            flags=0x20 if scheme == "https" else 0x00,
            httpreq=httpreq,
        )

    async def pause_for(self, millis: int) -> None:
        """Handle pause for x amount of time to help with syncing."""
        # https://wiki.slimdevices.com/index.php/SlimProto_TCP_protocol.html#u.2C_p.2C_a_.26_t_commands_and_replay_gain_field§
        await self._send_strm(b"p", replay_gain=millis)

    async def skip_over(self, millis: int) -> None:
        """Handle skip for x amount of time to help with syncing."""
        # https://wiki.slimdevices.com/index.php/SlimProto_TCP_protocol.html#u.2C_p.2C_a_.26_t_commands_and_replay_gain_field
        await self._send_strm(b"a", replay_gain=millis)

    async def unpause_at(self, timestamp: int) -> None:
        """Unpause at given timestamp to help with syncing."""
        # https://wiki.slimdevices.com/index.php/SlimProto_TCP_protocol.html#u.2C_p.2C_a_.26_t_commands_and_replay_gain_field
        await self._send_strm(b"u", replay_gain=timestamp)

    def signal_update(self) -> None:
        """Signal a player updated event to listeners."""
        self.callback(self, EventType.PLAYER_UPDATED)

    async def send_frame(self, command: bytes, data: bytes) -> None:
        """Send (raw) command to Squeeze player."""
        if self._reader.at_eof() or self._writer.is_closing():
            self.logger.debug("Socket is disconnected.")
            self.disconnect()
            return
        packet = struct.pack("!H", len(data) + 4) + command + data
        try:
            self._writer.write(packet)
            await self._writer.drain()
        except ConnectionResetError:
            self.disconnect()

    async def _render_display(self, action: str | None = None) -> None:
        """Set display based on the current state."""
        if self.display_control.disabled:
            return
        # set display brightness based on power state
        await self.display_control.set_brightness(4 if self.powered else 1)

        # set the visualization on playback start/stop
        if action == "playback_start":
            await self.display_control.set_visualization(True)
        elif action == "playback_stop":
            # clear screen entirely on playback stop
            await self.display_control.clear()
            await self.display_control.set_visualization(False)

        # temporary volume display
        if action == "show_volume":
            volume_str = f"VOLUME {self.volume_level}"
            await self.display_control.set_lines(fullscreen=volume_str)
        # temporary paused state display
        elif self.state == PlayerState.PAUSED:
            await self.display_control.set_lines(fullscreen="PAUSED")
        # metadata while playing
        elif self.powered and self.current_media:
            first_line = self.state.value
            second_line = self.current_media.url
            if artist := self.current_media.metadata.get("artist"):
                first_line = artist
            if title := self.current_media.metadata.get("title"):
                second_line = title
            await self.display_control.set_lines(first_line, second_line)
        # player off/idle: show clock
        else:
            time_str = datetime.now().strftime("%X")  # noqa: DTZ005
            # display clock in local time format without seconds
            if len(time_str.split(":")) > 2:
                time_str = time_str[:-3]
            await self.display_control.set_lines(fullscreen=time_str)

    async def _send_heartbeat(self) -> None:
        """Send periodic heartbeat message to player."""
        while self.connected:
            self._last_heartbeat = heartbeat_id = self._last_heartbeat + 1
            await self._send_strm(
                b"t",
                autostart=b"0",
                flags=0,
                replay_gain=heartbeat_id,
            )
            await self._render_display()
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def _socket_reader(self) -> None:
        """Handle incoming data from socket."""
        buffer = b""
        # keep reading bytes from the socket
        while not (self._reader.at_eof() or self._writer.is_closing()):
            try:
                async with timeout(HEARTBEAT_INTERVAL * 2):
                    data = await self._reader.read(64)
            except (TimeoutError, ConnectionResetError):
                break
            # handle incoming data from socket
            buffer = buffer + data
            del data
            if len(buffer) > 8:
                # construct operation and
                operation, length = buffer[:4], buffer[4:8]
                plen = struct.unpack("!I", length)[0] + 8
                if len(buffer) >= plen:
                    packet, buffer = buffer[8:plen], buffer[plen:]
                    operation = operation.strip(b"!").strip().decode().lower()
                    if operation == "bye!":
                        break
                    handler = getattr(self, f"_process_{operation}", None)
                    if handler is None:
                        self.logger.debug("No handler for %s", operation)
                    elif asyncio.iscoroutinefunction(handler):
                        create_task(handler(packet))
                    else:
                        asyncio.get_running_loop().call_soon(handler, packet)
        # EOF reached: socket is disconnected
        self._connected = False
        self.logger.debug(
            "Socket disconnected: %s",
            self._writer.get_extra_info("peername"),
        )
        self.callback(self, EventType.PLAYER_DISCONNECTED)
        self.disconnect()

    async def _send_strm(  # noqa: PLR0913
        self,
        command: bytes = b"q",
        autostart: bytes = b"0",
        codec_details: bytes = b"p1321",
        threshold: int = 0,
        spdif: bytes = b"0",
        trans_duration: int = 0,
        trans_type: bytes = b"0",
        flags: int = 0x20,
        output_threshold: int = 0,
        replay_gain: int = 0,
        server_port: int = 0,
        server_ip: int = 0,
        httpreq: bytes = b"",
    ) -> None:
        """Create stream request message based on given arguments."""
        data = struct.pack(
            "!cc5sBcBcBBBLHL",
            command,
            autostart,
            codec_details,
            threshold,
            spdif,
            trans_duration,
            trans_type,
            flags,
            output_threshold,
            0,
            replay_gain,
            server_port,
            server_ip,
        )
        await self.send_frame(b"strm", data + httpreq)

    async def _process_helo(self, data: bytes) -> None:
        """Process incoming HELO event from player (player connected)."""
        self.logger.debug("HELO received: %s", data)
        # player connected, sends helo info message
        (dev_id, _, mac) = struct.unpack("BB6s", data[:8])
        device_mac = ":".join(f"{x:02x}" for x in mac)
        self._player_id = str(device_mac).lower()
        self._device_type = DEVICE_TYPE.get(dev_id, "unknown device")
        self._capabilities = parse_capabilities(data)
        self.logger.debug("Player connected: %s", self.player_id)
        # Set some startup settings for the player
        await self.send_frame(b"vers", b"7.9")
        # request player to send the player name
        await self.send_frame(b"setd", struct.pack("B", 0xFE))
        await self.send_frame(b"setd", struct.pack("B", 0))
        # restore last power and volume levels
        # NOTE: this can be improved by storing the previous volume/power levels
        # so they can be restored when the player (re)connects.
        await self.power(self._powered)
        await self.volume_set(self.volume_level)
        self._connected = True
        self._heartbeat_task = asyncio.create_task(self._send_heartbeat())
        self.callback(self, EventType.PLAYER_CONNECTED)

    def _process_butn(self, data: bytes) -> None:
        """Handle 'butn' command from client."""
        timestamp, button = struct.unpack("!LL", data)
        self.logger.debug(
            "butn received - timestamp: %s - button: %s",
            timestamp,
            button,
        )
        # handle common buttons
        if button == ButtonCode.POWER:
            asyncio.create_task(self.toggle_power())
            return
        if button == ButtonCode.PAUSE:
            asyncio.create_task(self.toggle_pause())
            return
        if button == ButtonCode.PLAY:
            asyncio.create_task(self.play())
            return
        if button == ButtonCode.VOLUME_DOWN:
            asyncio.create_task(self.volume_down())
            return
        # forward all other
        self.callback(
            self,
            EventType.PLAYER_BTN_EVENT,
            {
                "type": "butn",
                "timestamp": timestamp,
                "button": button,
            },
        )

    def _process_knob(self, data: bytes) -> None:
        """Handle 'knob' command from client."""
        timestamp, position, sync = struct.unpack("!LLB", data)
        self.logger.debug(
            "knob received - position: %s - button: %s - sync: %s",
            timestamp,
            position,
            sync,
        )
        self.callback(
            self,
            EventType.PLAYER_BTN_EVENT,
            {
                "type": "knob",
                "timestamp": timestamp,
                "position": position,
                "sync": sync,
            },
        )

    def _process_ir(self, data: bytes) -> None:
        """Handle 'ir' command from client."""
        # format for IR:
        # [4]   time since startup in ticks (1KHz)
        # [1]	code format
        # [1]	number of bits
        # [4]   the IR code, up to 32 bits
        timestamp, code = struct.unpack("!LxxL", data)
        self.logger.debug("IR received - position: %s - code: %s", timestamp, code)
        # handle common buttons
        if code == RemoteCode.POWER:
            asyncio.create_task(self.toggle_power())
            return
        if code == RemoteCode.PAUSE:
            asyncio.create_task(self.toggle_pause())
            return
        if code == RemoteCode.PLAY:
            asyncio.create_task(self.play())
            return
        if code == RemoteCode.VOLUME_DOWN:
            asyncio.create_task(self.volume_down())
            return
        if code == RemoteCode.VOLUME_UP:
            asyncio.create_task(self.volume_up())
            return
        # forward all other
        self.callback(
            self,
            EventType.PLAYER_BTN_EVENT,
            {
                "type": "ir",
                "timestamp": timestamp,
                "code": code,
            },
        )

    def _process_dsco(self, data: bytes) -> None:
        """Process incoming stat DSCO message (data stream disconnected)."""
        self.logger.debug("DSCO received - data stream disconnected.")
        # Some players may send this to indicate they have disconnected
        # from the data stream either because the stream ended, or because
        # they have finished buffering the current file

    def _process_stat(self, data: bytes) -> None:
        """Redirect incoming STAT event from player to correct method."""
        event = data[:4].decode()
        event_data = data[4:]
        if event == b"\x00\x00\x00\x00":
            # Presumed informational stat message
            return
        event_handler = getattr(self, f"_process_stat_{event.lower()}", None)
        if event_handler is None:
            self.logger.debug("Unhandled event: %s - event_data: %s", event, event_data)
        elif asyncio.iscoroutinefunction(event_handler):
            create_task(event_handler(data[4:]))
        else:
            asyncio.get_running_loop().call_soon(event_handler, data[4:])

    async def _process_stat_aude(self, data: bytes) -> None:
        """Process incoming stat AUDe message (power level and mute)."""
        self.logger.debug("AUDe received - %s", data)
        # ignore this event (and use optimistic state instead),
        # because it is flaky across players
        await self._render_display()

    def _process_stat_audg(self, data: bytes) -> None:
        """Process incoming stat AUDg message."""
        self.logger.debug("AUDg received - %s", data)
        # Some players may send this as acknowledge of volume change (audg command).

    def _process_stat_stmc(self, data: bytes) -> None:
        """Process incoming stat STMc message (connected)."""
        self.logger.debug("STMc received - connected.")
        # srtm-s command received. Guaranteed to be the first response to an strm-s.
        self._state = PlayerState.BUFFERING
        self._current_media = self._next_media
        self._next_media = None
        self.extra_data["playlist_timestamp"] = int(time.time())
        self.signal_update()

    def _process_stat_stmd(self, data: bytes) -> None:
        """Process incoming stat STMd message (decoder ready)."""
        self.logger.debug("STMd received - decoder ready.")
        if self._next_media:
            # a next url has been enqueued
            asyncio.create_task(
                self.play_url(
                    url=self._next_media.url,
                    mime_type=self._next_media.mime_type,
                    metadata=self._next_media.metadata,
                    transition=self._next_media.transition,
                    transition_duration=self._next_media.transition_duration,
                    enqueue=False,
                    autostart=True,
                    send_flush=False,
                ),
            )
            return
        self.callback(self, EventType.PLAYER_DECODER_READY)

    def _process_stat_stmf(self, data: bytes) -> None:
        """Process incoming stat STMf message (connection closed)."""
        self.logger.debug("STMf received - connection closed.")

    def _process_stat_stmo(self, data: bytes) -> None:
        """
        Process incoming stat STMo message.

        No more decoded (uncompressed) data to play; triggers rebuffering.
        """
        self.logger.debug("STMo received - output underrun.")
        if self.state == PlayerState.BUFFERING:
            return  # transitioning
        self._state = PlayerState.BUFFERING
        if self._auto_play:
            asyncio.create_task(self.play())
        else:
            self.callback(self, EventType.PLAYER_OUTPUT_UNDERRUN)

    async def _process_stat_stmp(self, data: bytes) -> None:
        """Process incoming stat STMp message: Pause confirmed."""
        self.logger.debug("STMp received - pause confirmed.")
        self._state = PlayerState.PAUSED
        self.signal_update()
        await self._render_display()

    async def _process_stat_stmr(self, data: bytes) -> None:
        """Process incoming stat STMr message: Resume confirmed."""
        self.logger.debug("STMr received - resume confirmed.")
        self._state = PlayerState.PLAYING
        self.signal_update()
        await self._render_display()

    async def _process_stat_stms(self, data: bytes) -> None:
        """Process incoming stat STMs message: Playback of new track has started."""
        self.logger.debug("STMs received - playback of new track has started")
        self._state = PlayerState.PLAYING
        self.signal_update()
        await self._render_display("playback_start")

    def _process_stat_stmt(self, data: bytes) -> None:
        """Process incoming stat STMt message: heartbeat from client."""
        (
            num_crlf,
            mas_initialized,
            mas_mode,
            rptr,
            wptr,
            bytes_received_h,
            bytes_received_l,
            signal_strength,
            jiffies,
            output_buffer_size,
            output_buffer_readyness,
            elapsed_seconds,
            voltage,
            elapsed_milliseconds,
            server_heartbeat,
        ) = struct.unpack("!BBBLLLLHLLLLHLL", data[:47])

        self._jiffies = jiffies
        self._elapsed_milliseconds = elapsed_milliseconds
        self._last_timestamp = time.time()
        self.callback(self, EventType.PLAYER_HEARTBEAT)

    async def _process_stat_stmu(self, data: bytes) -> None:
        """Process stat STMu message: Buffer underrun: Normal end of playback."""
        self.logger.debug("STMu received - end of playback.")
        self._state = PlayerState.STOPPED
        # invalidate url/metadata
        self._current_media = None
        self._next_media = None
        self.extra_data["playlist_timestamp"] = int(time.time())
        self.signal_update()
        await self._render_display("playback_stop")

    def _process_stat_stml(self, data: bytes) -> None:
        """Process incoming stat STMl message: Buffer threshold reached."""
        self.logger.debug("STMl received - Buffer threshold reached.")
        # this is only used when autostart < 2 on strm-s commands
        # send an event for lib consumers to handle
        self._state = PlayerState.BUFFER_READY
        self.callback(self, EventType.PLAYER_BUFFER_READY)

    def _process_stat_stmn(self, data: bytes) -> None:
        """Process incoming stat STMn message: player couldn't decode stream."""
        self.logger.debug("STMn received - player couldn't decode stream.")
        self.callback(self, EventType.PLAYER_DECODER_ERROR)

    async def _process_resp(self, data: bytes) -> None:
        """Process incoming RESP message: Response received at player."""
        self.logger.debug("RESP received - Response received at player.")
        _, status_code, status = parse_status(data)
        headers = parse_headers(data)

        if "location" in headers:
            # handle redirect
            location = headers["location"]
            self.logger.debug("Received redirect to %s", location)
            await self.play_url(
                location,
                self.next_media.mime_type,
                self.next_media.metadata,
                self.next_media.transition,
                self.next_media.transition_duration,
            )
            return

        if status_code > 300:
            self.logger.error("Server responds with status %s %s", status_code, status)
            return

        if "content-type" in headers:
            content_type = headers.get("content-type")
            codc_msg = self._parse_codc(content_type)

            # send the codc message to the player to inform
            # about the codec that needs to be used
            self.logger.debug(
                "send CODC for contenttype %s: %s",
                content_type,
                codc_msg,
            )
            await self.send_frame(b"codc", codc_msg)

        # parse ICY metadata
        if (
            "icy-name" in headers
            and self._next_media
            and not self._next_media.metadata.get("title")
        ):
            self._next_media.metadata["title"] = headers["icy-name"]

        # send continue (used when autoplay 1 or 3)
        if self._auto_play:
            await self.send_frame(b"cont", b"1")

    def _process_setd(self, data: bytes) -> None:
        """Process incoming SETD message: Get/set player firmware settings."""
        self.logger.debug("SETD received - %s", data)
        data_id = data[0]
        if data_id == 0:
            # received player name
            self._device_name = data[1:-1].decode()
            self.callback(self, EventType.PLAYER_NAME_RECEIVED, self._device_name)
            self.logger = logging.getLogger(__name__).getChild(self._device_name)
        if data_id == 0xFE:
            # received display config (squeezebox2/squeezebox32)
            display_height = 0
            if len(data) == 7:
                display_width, display_height, _led_config = struct.unpack(
                    "!HHH",
                    data[1:],
                )
            elif len(data) == 5:
                display_width, display_height = struct.unpack("!HH", data[1:])
            else:
                display_width = struct.unpack("!H", data[1:])
            if display_width:
                self.display_control.width = display_width
            if display_height:
                self.display_control.height = display_height

            # Trigger an event callback for "PLAYER_DISPLAY_RESOLUTION"
            resolution = f"{display_width} x {display_height}"
            self.callback(self, EventType.PLAYER_DISPLAY_RESOLUTION, resolution)

            # If player reports a display resolution with a value of 0
            if display_width == 0 or display_height == 0:
                self.display_control.disabled(True)  # noqa: FBT003
                # Disable the display
            elif self.display_control.image.width != display_width:
                # If the display resolution reported by the player doesn't match
                # the display resolution we're currently using
                self.display_control = None
                self.display_control = SlimProtoDisplay(self, display_width)
                # Re-instanciate SlimProtoDisplay with the correct resolution parameters

    def _parse_codc(self, content_type: str) -> bytes:
        """Parse CODEC details from mime/content type string."""
        if "wav" in content_type or "pcm" in content_type:
            # wave header may contain info about sample rate etc
            # https://www.dialogic.com/webhelp/CSP1010/VXML1.1CI/WebHelp/standards_defaults%20-%20MIME%20Type%20Mapping.htm
            params = (
                dict(parse_qsl(content_type.replace(";", "&")))
                if ";" in content_type
                else {}
            )
            sample_rate = int(params.get("rate", 44100))
            sample_size = int(params.get("bitrate", 16))
            channels = int(params.get("channels", 2))
            codc_msg = (
                b"p"
                + PCM_SAMPLE_SIZE[sample_size]
                + PCM_SAMPLE_RATE[sample_rate]
                + str(channels).encode()
                + b"1"  # endianness
            )
            codc_msg = struct.pack(
                "ccccc",
                b"p",
                PCM_SAMPLE_SIZE[sample_size],
                PCM_SAMPLE_RATE[sample_rate],
                str(channels).encode(),
                b"1",
            )
        elif content_type not in CODEC_MAPPING:
            # use m as default/fallback
            self.logger.debug(
                "Unable to parse mime type %s, using mp3 as default codec",
                content_type,
            )
            codc_msg = b"m????"
        else:
            # regular contenttype
            codec = CODEC_MAPPING[content_type]
            if codec not in self.supported_codecs:
                self.logger.warning(
                    "Player did not report support for content_type %s, "
                    "playback might fail",
                    content_type,
                )
            if content_type in ("audio/aac", "audio/aacp"):
                # https://wiki.slimdevices.com/index.php/SlimProto_TCP_protocol.html#AAC-specific_notes
                codc_msg = b"a2???"
            else:
                codc_msg = FORMAT_BYTE[codec] + b"????"
        return codc_msg
