"""
Socketclient implementation for SLIMproto client (e.g. Squeezebox).

Large parts of this code are based on code by Doug Winter, all rights reserved.
https://github.com/winjer/squeal/blob/master/src/squeal/net/slimproto.py
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
from asyncio import StreamReader, StreamWriter, create_task
from collections import deque
from enum import Enum
from typing import Callable, Dict, List, TypedDict
from urllib.parse import parse_qsl, urlparse

from .const import EventType
from .errors import UnsupportedContentType
from .util import parse_capabilities, parse_headers

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
FALLBACK_CODECS = ["pcm"]
FALLBACK_SAMPLE_RATE = 96000


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
        self._volume_control = PySqueezeVolume()
        self._powered: bool = False
        self._muted: bool = False
        self._state = PlayerState.STOPPED
        self._last_timestamp: float = 0
        self._elapsed_milliseconds: float = 0
        self._next_url: str | None = None
        self._next_metadata: Metadata | None = None
        self._connected: bool = False
        self._last_heartbeat = (0, 0)
        self._packet_latency = deque(maxlen=10)
        self._reader_task = create_task(self._socket_reader())
        self._send_heartbeat()

    def disconnect(self) -> None:
        """Disconnect socket client."""
        if self._reader_task and not self._reader_task.cancelled():
            self._reader_task.cancel()

        if self._connected:
            self._connected = False
            if self._writer.can_write_eof():
                self._writer.write_eof()
            self._writer.close()
            self.callback(EventType.PLAYER_DISCONNECTED, self)

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
    def packet_latency(self) -> float:
        """Return (averaged) packet latency in seconds."""
        if not self._packet_latency:
            return 5  # return a safe default of 5ms
        return sum(self._packet_latency) / len(self._packet_latency)

    async def stop(self) -> None:
        """Send stop command to player."""
        await self.send_strm(b"q")

    async def play(self) -> None:
        """Send play/unpause command to player."""
        await self.send_strm(b"u")

    async def pause(self) -> None:
        """Send pause command to player."""
        await self.send_strm(b"p")

    async def power(self, powered: bool = True) -> None:
        """Send power command to player."""
        # mute is the same as power
        if not powered:
            await self.stop()
        power_int = 1 if powered else 0
        await self._send_frame(b"aude", struct.pack("2B", power_int, 1))
        self._powered = powered
        self.callback(EventType.PLAYER_UPDATED, self)

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

    async def mute(self, muted: bool = False) -> None:
        """Send mute command to player."""
        muted_int = 0 if muted else 1
        await self._send_frame(b"aude", struct.pack("2B", muted_int, 0))
        self.muted = muted
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
        ipaddr_b = socket.inet_aton(ipaddr)

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

        codec = CODEC_MAPPING.get(mime_type)
        if codec is not None and codec not in self.supported_codecs:
            raise UnsupportedContentType(
                f"Player does not support content type: {mime_type}"
            )

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
            formatbyte=FORMAT_BYTE.get(codec, b"?"),
            autostart=b"3",
            server_port=port,
            server_ip=ipaddr_b,
            threshold=200,
            output_threshold=10,
            trans_duration=transition_duration,
            trans_type=transition.value,
            flags=0x20 if scheme == "https" else 0x00,
            httpreq=httpreq,
        )

    def _send_heartbeat(self) -> None:
        """Send (periodic) heartbeat message to player."""

        async def async_send_heartbeat():
            heartbeat_id = self._last_heartbeat[0] + 1
            self._last_heartbeat = (heartbeat_id, time.time())
            await self.send_strm(b"t", flags=0, replay_gain=heartbeat_id)

        asyncio.create_task(async_send_heartbeat())

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
            data = await self._reader.read(64)
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
        self.disconnect()

    async def send_strm(
        self,
        command=b"q",
        formatbyte=b"?",
        autostart=b"0",
        samplesize=b"?",
        samplerate=b"?",
        channels=b"?",
        endian=b"?",
        threshold=0,
        spdif=b"0",
        trans_duration=0,
        trans_type=b"0",
        flags=0x20,
        output_threshold=0,
        replay_gain=0,
        server_port=0,
        server_ip=b"0",
        httpreq=b"",
    ) -> None:
        """Create stream request message based on given arguments."""
        data = struct.pack(
            "!cccccccBcBcBBBLH",
            command,
            autostart,
            formatbyte,
            samplesize,
            samplerate,
            channels,
            endian,
            threshold,
            spdif,
            trans_duration,
            trans_type,
            flags,
            output_threshold,
            0,
            replay_gain,
            server_port,
        )
        await self._send_frame(b"strm", data + server_ip + httpreq)

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
        await self._send_frame(b"vers", b"7.8")
        await self._send_frame(b"setd", struct.pack("B", 0))
        await self._send_frame(b"setd", struct.pack("B", 4))
        await self.stop()
        # restore last power and volume levels
        # NOTE: this can be improved by storing the previous volume/power levels
        # so they can be restored when the player (re)connects.
        await self.power(self._powered)
        await self.volume_set(self.volume_level)
        self._connected = True
        self.callback(EventType.PLAYER_CONNECTED, self)

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
        (spdif_enable, dac_enable) = struct.unpack("2B", data[:2])
        powered = spdif_enable or dac_enable
        self._powered = powered
        self._muted = not powered
        self.callback(EventType.PLAYER_UPDATED, self)

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

        # handle heartbeat response (used to measure roundtrip/latency and ping/pong)
        if server_heartbeat == self._last_heartbeat[0]:
            # consider latency as half of the roundtrip
            latency = (time.time() - self._last_heartbeat[1]) / 2
            self._packet_latency.append(latency)
            # schedule heartbeat
            # if playback busy we want high accuracy
            # but otherwise every 5 seconds is good enough
            if self.state == PlayerState.PLAYING:
                heartbeat_delay = 0.1
            elif self.powered:
                heartbeat_delay = 1
            else:
                heartbeat_delay = 5
            asyncio.get_event_loop().call_later(heartbeat_delay, self._send_heartbeat)

        self._elapsed_milliseconds = elapsed_milliseconds
        # consider latency when calculating the elapsed time
        self._last_timestamp = time.time() - self.packet_latency
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
            if "wav" in content_type:
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
                    + b"1"
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
                    raise UnsupportedContentType(
                        f"Player does not support content type: {content_type}"
                    )
                if content_type in ("audio/aac", "audio/aacp"):
                    # https://wiki.slimdevices.com/index.php/SlimProto_TCP_protocol.html#AAC-specific_notes
                    codc_msg = b"a2???"
                else:
                    codc_msg = FORMAT_BYTE[codec] + b"????"

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


class PySqueezeVolume:
    """Represents a sound volume. This is an awful lot more complex than it sounds."""

    minimum = 0
    maximum = 100
    step = 1

    # this map is taken from Slim::Player::Squeezebox2 in the squeezecenter source
    # i don't know how much magic it contains, or any way I can test it
    old_map = [
        0,
        1,
        1,
        1,
        2,
        2,
        2,
        3,
        3,
        4,
        5,
        5,
        6,
        6,
        7,
        8,
        9,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
        16,
        16,
        17,
        18,
        19,
        20,
        22,
        23,
        24,
        25,
        26,
        27,
        28,
        29,
        30,
        32,
        33,
        34,
        35,
        37,
        38,
        39,
        40,
        42,
        43,
        44,
        46,
        47,
        48,
        50,
        51,
        53,
        54,
        56,
        57,
        59,
        60,
        61,
        63,
        65,
        66,
        68,
        69,
        71,
        72,
        74,
        75,
        77,
        79,
        80,
        82,
        84,
        85,
        87,
        89,
        90,
        92,
        94,
        96,
        97,
        99,
        101,
        103,
        104,
        106,
        108,
        110,
        112,
        113,
        115,
        117,
        119,
        121,
        123,
        125,
        127,
        128,
    ]

    # new gain parameters, from the same place
    total_volume_range = -50  # dB
    step_point = (
        -1
    )  # Number of steps, up from the bottom, where a 2nd volume ramp kicks in.
    step_fraction = (
        1  # fraction of totalVolumeRange where alternate volume ramp kicks in.
    )

    def __init__(self):
        """Initialize class."""
        self.volume = 50

    def increment(self):
        """Increment the volume."""
        self.volume += self.step
        if self.volume > self.maximum:
            self.volume = self.maximum

    def decrement(self):
        """Decrement the volume."""
        self.volume -= self.step
        if self.volume < self.minimum:
            self.volume = self.minimum

    def old_gain(self):
        """Return the "Old" gain value as required by the squeezebox."""
        return self.old_map[self.volume]

    def decibels(self):
        """Return the "new" gain value."""
        # pylint: disable=invalid-name

        step_db = self.total_volume_range * self.step_fraction
        max_volume_db = 0  # different on the boom?

        # Equation for a line:
        # y = mx+b
        # y1 = mx1+b, y2 = mx2+b.
        # y2-y1 = m(x2 - x1)
        # y2 = m(x2 - x1) + y1
        slope_high = max_volume_db - step_db / (100.0 - self.step_point)
        slope_low = step_db - self.total_volume_range / (self.step_point - 0.0)
        x2 = self.volume
        if x2 > self.step_point:
            m = slope_high
            x1 = 100
            y1 = max_volume_db
        else:
            m = slope_low
            x1 = 0
            y1 = self.total_volume_range
        return m * (x2 - x1) + y1

    def new_gain(self):
        """Return new gainvalue of the volume control."""
        decibel = self.decibels()
        floatmult = 10 ** (decibel / 20.0)
        # avoid rounding errors somehow
        if -30 <= decibel <= 0:
            return int(floatmult * (1 << 8) + 0.5) * (1 << 8)
        return int((floatmult * (1 << 16)) + 0.5)
