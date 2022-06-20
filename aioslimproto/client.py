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
from asyncio import StreamReader, StreamWriter, Task, create_task
from enum import Enum
from typing import Callable, Dict, List, Optional
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
    IDLE = "idle"
    PAUSED = "paused"


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
        self._reader = reader
        self._writer = writer
        self._player_id: str = ""
        self._device_type: str = ""
        self._capabilities: Dict[str, str] = {}
        self._device_name: str = ""
        self._volume_control = PySqueezeVolume()
        self._powered: bool = False
        self._muted: bool = False
        self._state = PlayerState.IDLE
        self._last_timestamp: float = 0
        self._elapsed_milliseconds: float = 0
        self._last_report: int = 0
        self._current_url: str = ""
        self._connected: bool = False
        self._tasks: List[Task] = [
            create_task(self._socket_reader()),
            create_task(self._send_heartbeat()),
        ]

    def disconnect(self) -> None:
        """Disconnect socket client."""
        for task in self._tasks:
            if not task.cancelled():
                task.cancel()
        self._tasks = []
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
    def device_model(self) -> str | None:
        """Return device model of the player."""
        return self._capabilities.get("ModelName", self._capabilities.get("Model"))

    @property
    def max_sample_rate(self) -> int | None:
        """Return max sample rate supported by the player."""
        return self._capabilities.get("MaxSampleRate")

    @property
    def supported_codecs(self) -> List[str]:
        """Return supported codecs by the player."""
        return self._capabilities.get("SupportedCodecs", ["pcm"])

    @property
    def firmware(self) -> str | None:
        """Return firmware version string for the player."""
        return self._capabilities.get("Firmware")

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
        return self._elapsed_milliseconds + (
            (time.time() - self._last_timestamp) * 1000
        )

    @property
    def current_url(self):
        """Return uri of currently loaded track."""
        return self._current_url

    async def stop(self):
        """Send stop command to player."""
        self._current_url = ""
        await self.send_strm(b"q")

    async def play(self):
        """Send play/unpause command to player."""
        await self.send_strm(b"u")

    async def pause(self):
        """Send pause command to player."""
        await self.send_strm(b"p")

    async def power(self, powered: bool = True):
        """Send power command to player."""
        # power is not supported so abuse mute instead
        if not powered:
            await self.stop()
        power_int = 1 if powered else 0
        await self._send_frame(b"aude", struct.pack("2B", power_int, 1))
        self._powered = powered
        self.callback(EventType.PLAYER_UPDATED, self)

    async def volume_set(self, volume_level: int):
        """Send new volume level command to player."""
        self._volume_control.volume = volume_level
        old_gain = self._volume_control.old_gain()
        new_gain = self._volume_control.new_gain()
        await self._send_frame(
            b"audg",
            struct.pack("!LLBBLL", old_gain, old_gain, 1, 255, new_gain, new_gain),
        )
        self.callback(EventType.PLAYER_UPDATED, self)

    async def mute(self, muted: bool = False):
        """Send mute command to player."""
        muted_int = 0 if muted else 1
        await self._send_frame(b"aude", struct.pack("2B", muted_int, 0))
        self.muted = muted
        self.callback(EventType.PLAYER_UPDATED, self)

    async def play_url(
        self,
        url: str,
        crossfade: int = 0,
        mime_type: Optional[str] = None,
        send_flush: bool = True,
    ):
        """Request player to start playing a single url."""
        if send_flush:
            await self.send_strm(b"f", autostart=b"0")
        self.logger.debug("play url: %s", url)
        if not url.startswith("http"):
            raise UnsupportedContentType(f"Invalid URL: {url}")
        self._current_url = url
        self._powered = True
        enable_crossfade = crossfade > 0
        trans_type = b"1" if enable_crossfade else b"0"

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
            raise UnsupportedContentType("Player does not support HTTPS")

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
            trans_type=trans_type,
            trans_duration=crossfade,
            server_port=port,
            server_ip=ipaddr_b,
            threshold=200,
            output_threshold=10,
            flags=0x20 if scheme == "https" else 0x00,
            httpreq=httpreq,
        )

    async def _send_heartbeat(self):
        """Send periodic heartbeat message to player."""
        while True:
            await self.send_strm(b"t", flags=0)
            await asyncio.sleep(5)

    async def _send_frame(self, command, data):
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

    async def _socket_reader(self):
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
    ):
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

    async def _process_helo(self, data: bytes):
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

    def _process_stat(self, data):
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

    def _process_stat_aude(self, data):
        """Process incoming stat AUDe message (power level and mute)."""
        (spdif_enable, dac_enable) = struct.unpack("2B", data[:4])
        powered = spdif_enable or dac_enable
        self._powered = powered
        self._muted = not powered
        self.callback(EventType.PLAYER_UPDATED, self)

    def _process_stat_audg(self, data):
        """Process incoming stat AUDg message."""
        # srtm-s command received.
        # Some players may send this as aknowledge of volume change (audg command).

    def _process_stat_stmc(self, data):
        """Process incoming stat STMc message (connected)."""
        # srtm-s command received. Guaranteed to be the first response to an strm-s.

    def _process_stat_stmd(self, data):
        """Process incoming stat STMd message (decoder ready)."""
        # pylint: disable=unused-argument
        self.callback(EventType.PLAYER_DECODER_READY, self)

    def _process_stat_stmf(self, data):
        """Process incoming stat STMf message (connection closed)."""
        # pylint: disable=unused-argument
        self._state = PlayerState.IDLE
        self.callback(EventType.PLAYER_UPDATED, self)

    def _process_stat_stmo(self, data):
        """
        Process incoming stat STMo message.

        No more decoded (uncompressed) data to play; triggers rebuffering.
        """
        # pylint: disable=unused-argument
        self.logger.debug("STMo received - output underrun.")

    def _process_stat_stmp(self, data):
        """Process incoming stat STMp message: Pause confirmed."""
        # pylint: disable=unused-argument
        self.logger.debug("STMp received - pause confirmed.")
        self._state = PlayerState.PAUSED
        self.callback(EventType.PLAYER_UPDATED, self)

    def _process_stat_stmr(self, data):
        """Process incoming stat STMr message: Resume confirmed."""
        # pylint: disable=unused-argument
        self.logger.debug("STMr received - resume confirmed.")
        self._state = PlayerState.PLAYING
        self.callback(EventType.PLAYER_UPDATED, self)

    def _process_stat_stms(self, data):
        # pylint: disable=unused-argument
        """Process incoming stat STMs message: Playback of new track has started."""
        self.logger.debug("STMs received - playback of new track has started")
        self._state = PlayerState.PLAYING
        self.callback(EventType.PLAYER_UPDATED, self)

    def _process_stat_stmt(self, data):
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
            output_buffer_fullness,
            elapsed_seconds,
            voltage,
            elapsed_milliseconds,
            timestamp,
            error_code,
        ) = struct.unpack("!BBBLLLLHLLLLHLLH", data)

        self._elapsed_milliseconds = elapsed_milliseconds
        # formally we should use the timestamp field to calculate roundtrip time
        # but I have not seen any need for that so far and just assume that each player
        # more or less has the same latency
        cur_timestamp = time.time()
        self._last_timestamp = cur_timestamp
        if abs(elapsed_seconds - self._last_report) >= 1:
            self._last_report = elapsed_seconds
            # send report with elapsed time only every second while playing
            # note that the (very) accurate elapsed/current is always available in the property
            self.callback(EventType.PLAYER_TIME_UPDATED, self)

    def _process_stat_stmu(self, data):
        """Process incoming stat STMu message: Buffer underrun: Normal end of playback."""
        # pylint: disable=unused-argument
        self.logger.debug("STMu received - end of playback.")
        self._state = PlayerState.IDLE
        self.callback(EventType.PLAYER_UPDATED, self)

    def _process_stat_stml(self, data):
        """Process incoming stat STMl message: Buffer threshold reached."""
        # pylint: disable=unused-argument
        self.logger.debug("STMl received - Buffer threshold reached.")
        # autoplay 0 or 2: start playing by send unpause command when buffer full
        asyncio.create_task(self.send_strm(b"u"))

    def _process_stat_stmn(self, data):
        """Process incoming stat STMn message: player couldn't decode stream."""
        # pylint: disable=unused-argument
        self.logger.debug("STMn received - player couldn't decode stream.")
        self.callback(EventType.PLAYER_DECODER_ERROR)

    async def _process_resp(self, data):
        """Process incoming RESP message: Response received at player."""
        self.logger.debug("RESP received - Response received at player.")
        headers = parse_headers(data)

        if "location" in headers:
            # handle redirect
            location = headers["location"]
            self.logger.debug("Received redirect to %s", location)
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

    def _process_setd(self, data):
        """Process incoming SETD message: Get/set player firmware settings."""
        data_id = data[0]
        if data_id == 0:
            # received player name
            self._device_name = data[1:-1].decode()
            self.callback(EventType.PLAYER_NAME_RECEIVED, self)


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
