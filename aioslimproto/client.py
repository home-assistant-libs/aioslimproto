"""
Socketclient implementation for SLIMproto client (e.g. Squeezebox).

Large parts of this code are based on code by Doug Winter, all rights reserved.
https://github.com/winjer/squeal/blob/master/src/squeal/net/slimproto.py
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import struct
import time
from asyncio import StreamReader, StreamWriter, create_task
from enum import Enum, IntEnum
from typing import Callable, Dict, List, TypedDict
from urllib.parse import parse_qsl, urlparse

from async_timeout import timeout

from aioslimproto.display import SlimProtoDisplay

from .const import FALLBACK_CODECS, EventType
from .errors import UnsupportedContentType
from .util import parse_capabilities, parse_headers
from .visualisation import SpectrumAnalyser, VisualisationType
from .volume import SlimProtoVolume

# from http://wiki.slimdevices.com/index.php/SlimProtoTCPProtocol#HELO
DEVICE_TYPE = {
    2: "squeezebox",
    3: "softsqueeze",
    4: "squeezebox2",
    5: "transporter",
    6: "softsqueeze3",
    7: "receiver",
    8: "squeezeslave",
    9: "controller",
    10: "boom",
    11: "softboom",
    12: "squeezeplay",
}


class PlayerState(Enum):
    """Enum with the possible player states."""

    PLAYING = "playing"
    STOPPED = "stopped"
    PAUSED = "paused"
    BUFFERING = "buffering"


class TransitionType(Enum):
    """Transition type enum."""

    NONE = b"0"
    CROSSFADE = b"1"
    FADE_IN = b"2"
    FADE_OUT = b"3"
    FADE_IN_OUT = b"4"


class RemoteCode(IntEnum):
    """Enum with all (known) remote ir codes."""

    SLEEP = 1988737095
    POWER = 1988706495
    REWIND = 1988739135
    PAUSE = 1988698335
    FORWARD = 1988730975
    ADD = 1988714655
    PLAY = 1988694255
    UP = 1988747295
    DOWN = 1988735055
    LEFT = 1988726895
    RIGHT = 1988743215
    VOLUME_UP = 1988722815
    VOLUME_DOWN = 1988690175
    NUM_1 = 1988751375
    NUM_2 = 1988692215
    NUM_3 = 1988724855
    NUM_4 = 1988708535
    NUM_5 = 1988741175
    NUM_6 = 1988700375
    NUM_7 = 1988733015
    NUM_8 = 1988716695
    NUM_9 = 1988749335
    NUM_0 = 1988728935
    FAVORITES = 1988696295
    SEARCH = 1988712615
    BROWSE = 1988718735
    SHUFFLE = 1988745255
    REPEAT = 1988704455
    NOW_PLAYING = 1988720775
    SIZE = 1988753415
    BRIGHTNESS = 1988691195


class ButtonCode(IntEnum):
    """Enum with all (known) button codes."""

    POWER = 65546
    PRESET_1 = 131104
    PRESET_2 = 131105
    PRESET_3 = 131106
    PRESET_4 = 131107
    PRESET_5 = 131108
    PRESET_6 = 131109
    BACK = 131085
    PLAY = 131090
    ADD = 131091
    UP = 131083
    OK = 131086
    REWIND = 131088
    PAUSE = 131095
    FORWARD = 131101
    VOLUME_DOWN = 131081
    VOLUME_UP = 131082


PCM_SAMPLE_SIZE = {
    # Map with sample sizes used in slimproto."""
    8: b"0",
    16: b"1",
    20: b"2",
    32: b"3",
    24: b"4",
    0: b"?",
}

PCM_SAMPLE_RATE = {
    # map with sample rates used in slimproto."""
    # https://wiki.slimdevices.com/index.php/SlimProto_TCP_protocol.html#Command:_.22strm.22
    # See %pcm_sample_rates in slimserver/Slim/Player/Squeezebox2.pm and
    # slimserver/Slim/Player/SqueezePlay.pm for definition of sample rates
    11000: b"0",
    22000: b"1",
    44100: b"3",
    48000: b"4",
    8000: b"5",
    12000: b"6",
    16000: b"7",
    24000: b"8",
    88200: b":",
    96000: b"9",
    176400: b";",
    192000: b"<",
    352800: b"=",
    384000: b">",
    0: b"?",
}

CODEC_MAPPING = {
    # map with common audio mime types to type used in squeezebox players
    "audio/mp3": "mp3",
    "audio/mpeg": "mp3",
    "audio/flac": "flc",
    "audio/x-flac": "flc",
    "audio/wma": "wma",
    "audio/ogg": "ogg",
    "audio/oga": "ogg",
    "audio/aac": "aac",
    "audio/aacp": "aac",
    "audio/alac": "alc",
    "audio/wav": "pcm",
    "audio/x-wav": "pcm",
    "audio/dsf": "dsf",
    "audio/pcm,": "pcm",
}

FORMAT_BYTE = {
    # map with audio formats used in slimproto to formatbyte
    # https://wiki.slimdevices.com/index.php/SlimProto_TCP_protocol.html#Command:_.22strm.22
    "pcm": b"p",
    "mp3": b"m",
    "flc": b"f",
    "wma": b"w",
    "ogg": b"0",
    "aac": b"a",
    "alc": b"l",
    "dsf": b"p",
    "dff": b"p",
    "aif": b"p",
}

FALLBACK_MODEL = "Squeezebox"
FALLLBACK_FIRMWARE = "Unknown"
FALLBACK_SAMPLE_RATE = 96000
HEARTBEAT_INTERVAL = 5


class Metadata(TypedDict):
    """Optional metadata for playback."""

    item_id: str  # optional
    artist: str  # optional
    album: str  # optional
    title: str  # optional
    image_url: str  # optional


class SlimClient:
    """SLIMProto socket client."""

    def __init__(
        self,
        reader: StreamReader,
        writer: StreamWriter,
        callback: Callable,
    ):
        """Initialize the socket client."""
        self.callback = callback
        self.logger = logging.getLogger(__name__)
        self.current_url: str | None = None
        self.current_metadata: Metadata | None = None
        self._reader = reader
        self._writer = writer
        self._player_id: str = ""
        self._device_type: str = ""
        self._capabilities: Dict[str, str] = {}
        self._device_name: str = ""
        self._volume_control = SlimProtoVolume()
        self._display_control = SlimProtoDisplay()
        self._powered: bool = False
        self._muted: bool = False
        self._state = PlayerState.STOPPED
        self._jiffies: int = 0
        self._last_timestamp: float = 0
        self._elapsed_milliseconds: float = 0
        self._next_url: str | None = None
        self._next_metadata: Metadata | None = None
        self._connected: bool = False
        self._last_heartbeat = 0
        self._reader_task = create_task(self._socket_reader())
        self._heartbeat_task: asyncio.Task | None = None

    def disconnect(self) -> None:
        """Disconnect and/or cleanup socket client."""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()

        if self._connected:
            self._connected = False
            if self._writer.can_write_eof():
                self._writer.write_eof()
            self._writer.close()

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
            "ModelName", self._capabilities.get("Model", FALLBACK_MODEL)
        )

    @property
    def max_sample_rate(self) -> int:
        """Return max sample rate supported by the player."""
        return self._capabilities.get("MaxSampleRate", FALLBACK_SAMPLE_RATE)

    @property
    def supported_codecs(self) -> List[str]:
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
        return self._volume_control.volume

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
        if not self.state == PlayerState.PLAYING:
            return self._elapsed_milliseconds
        # if the player is playing we return a very accurate timestamp
        # which in turn can be used by consumers to sync players etc.
        return self._elapsed_milliseconds + int(
            (time.time() - self._last_timestamp) * 1000
        )

    @property
    def jiffies(self) -> int:
        """Return (realtime) epoch timestamp from player."""
        return self._jiffies + int((time.time() - self._last_timestamp) * 1000)

    async def stop(self) -> None:
        """Send stop command to player."""
        await self.send_strm(b"q", flags=0)
        await self.set_display()
        # some players do not update their state by event so we force it here
        if self._state != PlayerState.STOPPED:
            self._state = PlayerState.STOPPED
            self.callback(EventType.PLAYER_UPDATED, self)

    async def play(self) -> None:
        """Send play/unpause command to player."""
        await self.send_strm(b"u", flags=0)
        # some players do not update their state by event so we force it here
        if self._state == PlayerState.PAUSED:
            self._state = PlayerState.PLAYING
            self.callback(EventType.PLAYER_UPDATED, self)

    async def pause(self) -> None:
        """Send pause command to player."""
        await self.send_strm(b"p")
        # some players do not update their state by event so we force it here
        if self._state == PlayerState.PLAYING:
            self._state = PlayerState.PAUSED
            self.callback(EventType.PLAYER_UPDATED, self)

    async def toggle_pause(self) -> None:
        """Toggle play/pause command."""
        if self.state == PlayerState.PLAYING:
            await self.pause()
        else:
            await self.play()

    async def power(self, powered: bool = True) -> None:
        """Send power command to player."""
        # mute is the same as power
        if not powered:
            await self.stop()
        power_int = 1 if powered else 0
        await self._send_frame(b"aude", struct.pack("2B", power_int, 1))
        self._powered = powered
        self.callback(EventType.PLAYER_UPDATED, self)
        await self.set_display()

    async def toggle_power(self) -> None:
        """Toggle power command."""
        await self.power(not self.powered)

    async def volume_set(self, volume_level: int) -> None:
        """Send new volume level command to player."""
        self._volume_control.volume = volume_level
        old_gain = self._volume_control.old_gain()
        new_gain = self._volume_control.new_gain()
        await self._send_frame(
            b"audg",
            struct.pack("!LLBBLL", old_gain, old_gain, 1, 255, new_gain, new_gain),
        )
        self.callback(EventType.PLAYER_UPDATED, self)

    async def volume_up(self) -> None:
        """Send volume up command to player."""
        self._volume_control.increment()
        old_gain = self._volume_control.old_gain()
        new_gain = self._volume_control.new_gain()
        await self._send_frame(
            b"audg",
            struct.pack("!LLBBLL", old_gain, old_gain, 1, 255, new_gain, new_gain),
        )
        self.callback(EventType.PLAYER_UPDATED, self)

    async def volume_down(self) -> None:
        """Send volume down command to player."""
        self._volume_control.decrement()
        old_gain = self._volume_control.old_gain()
        new_gain = self._volume_control.new_gain()
        await self._send_frame(
            b"audg",
            struct.pack("!LLBBLL", old_gain, old_gain, 1, 255, new_gain, new_gain),
        )
        self.callback(EventType.PLAYER_UPDATED, self)

    async def mute(self, muted: bool = False) -> None:
        """Send mute command to player."""
        muted_int = 0 if muted else 1
        await self._send_frame(b"aude", struct.pack("2B", muted_int, 0))
        self._muted = muted
        self.callback(EventType.PLAYER_UPDATED, self)

    async def play_url(
        self,
        url: str,
        mime_type: str | None = None,
        metadata: Metadata | None = None,
        send_flush: bool = True,
        transition: TransitionType = TransitionType.NONE,
        transition_duration: int = 0,
    ) -> None:
        """Request player to start playing a single url."""
        self.logger.debug("play url: %s", url)
        if not url.startswith("http"):
            raise UnsupportedContentType(f"Invalid URL: {url}")

        if send_flush:
            # flush buffers before playback
            await self.send_strm(b"f", autostart=b"0")
        self._next_url = url
        self._next_metadata = metadata
        # power on if we're not already powered
        if not self._powered:
            await self.power(True)
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
                "trying HTTP instead but playback may fail."
            )
            self.current_url = url.replace("https", "http")
            scheme = "http"
            port = 80

        if mime_type is None:
            # try to get the audio format from file extension
            ext = f'audio/{url.split(".")[-1]}'
            if ext in CODEC_MAPPING:
                mime_type = ext

        codec_details = self._parse_codc(mime_type)

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

        await self.send_strm(
            command=b"s",
            codec_details=codec_details,
            autostart=b"1",
            server_port=port,
            server_ip=int(ipaddress.ip_address(ipaddr)),
            threshold=200,
            output_threshold=10,
            trans_duration=transition_duration,
            trans_type=transition.value,
            flags=0x20 if scheme == "https" else 0x00,
            httpreq=httpreq,
        )
        await self.set_display()

    async def set_brightness(self, level=4):
        """Set brightness command on (supported) display."""
        assert 0 <= level <= 4
        await self._send_frame(b"grfb", struct.pack("!H", level))

    async def set_visualisation(
        self, visualisation: VisualisationType | None = None
    ) -> None:
        """Set Visualisation engine on player."""
        if visualisation is None:
            visualisation = SpectrumAnalyser()

        def _handle():
            return visualisation.pack()

        data = await asyncio.get_running_loop().run_in_executor(None, _handle)
        await self._send_frame(b"visu", data)

    async def render(
        self,
        text: str,
        size: int = 16,
        position: tuple[int, int] = (0, 0),
        font: str = "DejaVu-Sans",
    ) -> None:
        """Render given text on display of (supported) slimproto client."""

        def _render():
            self._display_control.clear()
            self._display_control.renderText(text, font, size, position)
            return self._display_control.frame()

        bitmap = await asyncio.get_running_loop().run_in_executor(None, _render)
        await self._update_display(bitmap)

    async def set_display(self) -> None:
        """Render default text on player display."""
        await self.set_visualisation()
        if not self.powered:
            await self.render("")
        elif self._next_metadata and "title" in self._next_metadata:
            await self.render(self._next_metadata["title"])
        else:
            await self.render(self.name)

    async def _update_display(
        self, bitmap: bytes, transition: str = "c", offset: int = 0, param: int = 0
    ) -> None:
        """Update display of (supported) slimproto client."""
        frame = struct.pack("!Hcb", offset, transition.encode(), param) + bitmap
        await self._send_frame(b"grfe", frame)

    async def _send_heartbeat(self) -> None:
        """Send periodic heartbeat message to player."""
        while self.connected:
            self._last_heartbeat = heartbeat_id = self._last_heartbeat + 1
            await self.send_strm(
                b"t", autostart=b"1", flags=0, replay_gain=heartbeat_id
            )
            await asyncio.sleep(5)

    async def _send_frame(self, command: bytes, data: bytes) -> None:
        """Send command to Squeeze player."""
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

    async def _socket_reader(self) -> None:
        """Handle incoming data from socket."""
        buffer = b""
        # keep reading bytes from the socket
        while not (self._reader.at_eof() or self._writer.is_closing()):
            try:
                async with timeout(HEARTBEAT_INTERVAL * 2):
                    data = await self._reader.read(64)
            except asyncio.TimeoutError:
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
                    handler = getattr(self, f"_process_{operation}", None)
                    if handler is None:
                        self.logger.debug("No handler for %s", operation)
                    elif asyncio.iscoroutinefunction(handler):
                        create_task(handler(packet))
                    else:
                        asyncio.get_running_loop().call_soon(handler, packet)
        # EOF reached: socket is disconnected
        self.logger.debug(
            "Socket disconnected: %s", self._writer.get_extra_info("peername")
        )
        self.callback(EventType.PLAYER_DISCONNECTED, self)
        self.disconnect()

    async def send_strm(
        self,
        command=b"q",
        autostart=b"0",
        codec_details=b"p1321",
        threshold=255,
        spdif=b"0",
        trans_duration=0,
        trans_type=b"0",
        flags=0x40,
        output_threshold=0,
        replay_gain=0,
        server_port=0,
        server_ip=0,
        httpreq=b"",
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
        await self._send_frame(b"strm", data + httpreq)

    async def _process_helo(self, data: bytes) -> None:
        """Process incoming HELO event from player (player connected)."""
        self.logger.debug("HELO received: %s", data)
        # player connected, sends helo info message
        (dev_id, _, mac) = struct.unpack("BB6s", data[:8])
        # pylint: disable = consider-using-f-string
        device_mac = ":".join("%02x" % x for x in mac)
        self._player_id = str(device_mac).lower()
        self._device_type = DEVICE_TYPE.get(dev_id, "unknown device")
        self._capabilities = parse_capabilities(data)
        self.logger.debug("Player connected: %s", self.player_id)
        # Set some startup settings for the player
        await self._send_frame(b"vers", b"7.999.999")
        await self.stop()
        await self.set_brightness()
        await self.set_visualisation()
        await self._send_frame(b"setd", struct.pack("B", 0))
        await self._send_frame(b"setd", struct.pack("B", 4))
        await self.stop()
        # restore last power and volume levels
        # NOTE: this can be improved by storing the previous volume/power levels
        # so they can be restored when the player (re)connects.
        await self.power(self._powered)
        await self.volume_set(self.volume_level)
        self._connected = True
        await self.set_display()
        self._heartbeat_task = asyncio.create_task(self._send_heartbeat())
        self.callback(EventType.PLAYER_CONNECTED, self)

    def _process_butn(self, data: bytes) -> None:
        """Handle 'butn' command from client."""
        timestamp, button = struct.unpack("!LL", data)
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
        if button == ButtonCode.VOLUME_UP:
            asyncio.create_task(self.volume_up())
            return
        # forward all other
        self.callback(
            EventType.PLAYER_BTN_EVENT,
            self,
            {
                "type": "butn",
                "timestamp": timestamp,
                "button": button,
            },
        )

    def _process_knob(self, data: bytes) -> None:
        """Handle 'knob' command from client."""
        timestamp, position, sync = struct.unpack("!LLB", data)
        self.callback(
            EventType.PLAYER_BTN_EVENT,
            self,
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
            EventType.PLAYER_BTN_EVENT,
            self,
            {
                "type": "ir",
                "timestamp": timestamp,
                "code": code,
            },
        )

    def _process_stat(self, data: bytes) -> None:
        """Redirect incoming STAT event from player to correct method."""
        event = data[:4].decode()
        event_data = data[4:]
        # pylint: disable = consider-using-f-string
        if event == b"\x00\x00\x00\x00":
            # Presumed informational stat message
            return
        event_handler = getattr(self, "_process_stat_%s" % event.lower(), None)
        if event_handler is None:
            self.logger.debug("Unhandled event: %s - event_data: %s", event, event_data)
        else:
            asyncio.get_running_loop().call_soon(event_handler, data[4:])

    def _process_stat_aude(self, data: bytes) -> None:
        """Process incoming stat AUDe message (power level and mute)."""
        # ignore this event (and use optimistic state instead), is is flaky across players

    def _process_stat_audg(self, data: bytes) -> None:
        """Process incoming stat AUDg message."""
        # srtm-s command received.
        # Some players may send this as aknowledge of volume change (audg command).

    def _process_stat_stmc(self, data: bytes) -> None:
        """Process incoming stat STMc message (connected)."""
        self.logger.debug("STMc received - connected.")
        # srtm-s command received. Guaranteed to be the first response to an strm-s.
        self._state = PlayerState.BUFFERING

    def _process_stat_stmd(self, data: bytes) -> None:
        """Process incoming stat STMd message (decoder ready)."""
        # pylint: disable=unused-argument
        self.logger.debug("STMd received - decoder ready.")
        self.callback(EventType.PLAYER_DECODER_READY, self)

    def _process_stat_stmf(self, data: bytes) -> None:
        """Process incoming stat STMf message (connection closed)."""
        # pylint: disable=unused-argument
        self.logger.debug("STMf received - connection closed.")
        self._state = PlayerState.STOPPED
        self.callback(EventType.PLAYER_UPDATED, self)

    def _process_stat_stmo(self, data: bytes) -> None:
        """
        Process incoming stat STMo message.

        No more decoded (uncompressed) data to play; triggers rebuffering.
        """
        # pylint: disable=unused-argument
        self.logger.debug("STMo received - output underrun.")

    def _process_stat_stmp(self, data: bytes) -> None:
        """Process incoming stat STMp message: Pause confirmed."""
        # pylint: disable=unused-argument
        self.logger.debug("STMp received - pause confirmed.")
        self._state = PlayerState.PAUSED
        self.callback(EventType.PLAYER_UPDATED, self)

    def _process_stat_stmr(self, data: bytes) -> None:
        """Process incoming stat STMr message: Resume confirmed."""
        # pylint: disable=unused-argument
        self.logger.debug("STMr received - resume confirmed.")
        self._state = PlayerState.PLAYING
        self.callback(EventType.PLAYER_UPDATED, self)

    def _process_stat_stms(self, data: bytes) -> None:
        # pylint: disable=unused-argument
        """Process incoming stat STMs message: Playback of new track has started."""
        self.logger.debug("STMs received - playback of new track has started")
        self._state = PlayerState.PLAYING
        self.current_metadata = self._next_metadata
        self.current_url = self._next_url
        self._next_metadata = None
        self._next_url = None
        self.callback(EventType.PLAYER_UPDATED, self)

    def _process_stat_stmt(self, data: bytes) -> None:
        """Process incoming stat STMt message: heartbeat from client."""
        # pylint: disable=unused-variable
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
        self.callback(EventType.PLAYER_HEARTBEAT, self)

    def _process_stat_stmu(self, data: bytes) -> None:
        """Process incoming stat STMu message: Buffer underrun: Normal end of playback."""
        # pylint: disable=unused-argument
        self.logger.debug("STMu received - end of playback.")
        self._state = PlayerState.STOPPED
        # invalidate url/metadata
        self.current_metadata = None
        self.current_url = None
        self._next_metadata = None
        self._next_url = None
        self.callback(EventType.PLAYER_UPDATED, self)

    def _process_stat_stml(self, data: bytes) -> None:
        """Process incoming stat STMl message: Buffer threshold reached."""
        # pylint: disable=unused-argument
        self.logger.debug("STMl received - Buffer threshold reached.")
        # this is only used when autostart < 2 on strm-s commands
        # send an event anyway for lib consumers to handle
        self.callback(EventType.PLAYER_BUFFER_READY, self)

    def _process_stat_stmn(self, data: bytes) -> None:
        """Process incoming stat STMn message: player couldn't decode stream."""
        # pylint: disable=unused-argument
        self.logger.debug("STMn received - player couldn't decode stream.")
        self.callback(EventType.PLAYER_DECODER_ERROR, self)

    async def _process_resp(self, data: bytes) -> None:
        """Process incoming RESP message: Response received at player."""
        self.logger.debug("RESP received - Response received at player.")
        headers = parse_headers(data)

        if "location" in headers:
            # handle redirect
            location = headers["location"]
            self.logger.debug("Received redirect to %s", location)
            if location.startswith("https") and not self._capabilities.get("CanHTTPS"):
                self.logger.error("Server requires HTTPS.")
            else:
                await self.play_url(location)
            return

        if "content-type" in headers:
            content_type = headers.get("content-type")
            codc_msg = self._parse_codc(content_type)

            # send the codc message to the player to inform about the codec that needs to be used
            self.logger.debug(
                "send CODC for contenttype %s: %s", content_type, codc_msg
            )
            await self._send_frame(b"codc", codc_msg)

        # send continue (used when autoplay 1 or 3)
        await self._send_frame(b"cont", b"1")

    def _process_setd(self, data: bytes) -> None:
        """Process incoming SETD message: Get/set player firmware settings."""
        data_id = data[0]
        if data_id == 0:
            # received player name
            self._device_name = data[1:-1].decode()
            self.callback(EventType.PLAYER_NAME_RECEIVED, self)
            self.logger = logging.getLogger(__name__).getChild(self._device_name)

    def _parse_codc(self, content_type: str) -> bytes:
        """Parse CODEC details from mime/content type string."""
        if "wav" in content_type or "pcm" in content_type:
            # wave header may contain info about sample rate etc
            # https://www.dialogic.com/webhelp/CSP1010/VXML1.1CI/WebHelp/standards_defaults%20-%20MIME%20Type%20Mapping.htm
            if ";" in content_type:
                params = dict(parse_qsl(content_type.replace(";", "&")))
            else:
                params = {}
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
                    "Player did report support for content_type %s, playback might fail",
                    content_type,
                )
            if content_type in ("audio/aac", "audio/aacp"):
                # https://wiki.slimdevices.com/index.php/SlimProto_TCP_protocol.html#AAC-specific_notes
                codc_msg = b"a2???"
            else:
                codc_msg = FORMAT_BYTE[codec] + b"????"
        return codc_msg
