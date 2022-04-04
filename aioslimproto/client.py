"""
Socketclient implementation for SLIMproto client (e.g. Squeezebox).

Large parts of this code are based on code by Doug Winter, all rights reserved.
https://github.com/winjer/squeal/blob/master/src/squeal/net/slimproto.py
"""
from __future__ import annotations

import asyncio
from asyncio import StreamWriter, StreamReader
import logging
from multiprocessing.sharedctypes import Value
import urllib.request
import socket
from urllib.parse import urlparse, parse_qsl
import struct
import time
from enum import Enum
from typing import Callable, TYPE_CHECKING

from .util import run_periodic
from .const import EventType

LOGGER = logging.getLogger(__name__)


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
    11000: b"0",
    22000: b"1",
    44100: b"3",
    48000: b"4",
    8000: b"5",
    12000: b"6",
    16000: b"7",
    24000: b"8",
    96000: b"9",
    0: b"?",
}

AUDIO_FORMAT_BYTE = {
    # map with audio formats used in slimproto."""
    # https://wiki.slimdevices.com/index.php/SlimProto_TCP_protocol.html#Command:_.22strm.22
    "pcm": b"p",
    "mp3": b"m",
    "flac": b"f",
    "wma": b"w",
    "ogg": b"0",
    "aac": b"a",
    "alac": b"l",
    "wav": b"p",
    "auto": b"?",
}


class SlimClient:
    """SLIMProto socket client."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        callback: Callable,
    ):
        """Initialize the socket client."""
        self.callback = callback
        self._reader = reader
        self._writer = writer
        self._player_id = ""
        self._device_type = ""
        self._device_name = ""
        self._last_volume = 0
        self._last_heartbeat = 0
        self._volume_control = PySqueezeVolume()
        self._powered = False
        self._muted = False
        self._state = PlayerState.IDLE
        self._elapsed_seconds = 0
        self._elapsed_milliseconds = 0
        self._current_url = ""
        self._connected = True
        self._tasks = [
            asyncio.create_task(self._socket_reader()),
            asyncio.create_task(self._send_heartbeat()),
        ]

    def disconnect(self) -> None:
        """Disconnect socket client."""
        for task in self._tasks:
            if not task.cancelled():
                task.cancel()

    @property
    def connected(self):
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
    def volume_level(self):
        """Return current volume level of player."""
        return self._volume_control.volume

    @property
    def powered(self):
        """Return current power state of player."""
        return self._powered

    @property
    def muted(self):
        """Return current mute state of player."""
        return self._muted

    @property
    def state(self):
        """Return current state of player."""
        return self._state

    @property
    def elapsed_seconds(self):
        """Return elapsed_time of current playing track in (fractions of) seconds."""
        return self._elapsed_seconds

    @property
    def elapsed_milliseconds(self) -> int:
        """Return (realtime) elapsed time of current playing media in milliseconds."""
        return self._elapsed_milliseconds + int(
            (time.time() * 1000) - (self._last_heartbeat * 1000)
        )

    @property
    def current_url(self):
        """Return uri of currently loaded track."""
        return self._current_url

    async def _initialize_player(self):
        """Set some startup settings for the player."""
        # send version
        await self._send_frame(b"vers", b"7.8")
        await self._send_frame(b"setd", struct.pack("B", 0))
        await self._send_frame(b"setd", struct.pack("B", 4))

    async def stop(self):
        """Send stop command to player."""
        await self.send_strm(b"q")

    async def play(self):
        """Send play (unpause) command to player."""
        await self.send_strm(b"u")

    async def pause(self):
        """Send pause command to player."""
        await self.send_strm(b"p")

    async def power(self, powered: bool = True):
        """Send power command to player."""
        # power is not supported so abuse mute instead
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
        fmt: str = "auto",
    ):
        """Request player to start playing a single url."""
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

        host = socket.gethostbyname(host)
        ip = socket.inet_aton(host)

        if port is None and scheme == "https":
            port = 443
        elif port is None:
            port = 80

        if fmt == "auto":
            # try to get the audio format from file extension
            ext = url.split(".")[-1]
            if ext in AUDIO_FORMAT_BYTE:
                fmt = ext

        headers = f"Connection: close\r\nAccept: */*\r\nHost: {host}:{port}\r\n"
        httpreq = "GET %s HTTP/1.0\r\n%s\r\n" % (path, headers)
        await self.send_strm(
            command=b"s",
            formatbyte=AUDIO_FORMAT_BYTE[fmt],
            autostart=b"3",
            endian=b"1",
            trans_type=trans_type,
            trans_duration=crossfade,
            server_port=port,
            server_ip=ip,
            httpreq=httpreq.encode("utf-8"),
        )

    @run_periodic(5)
    async def _send_heartbeat(self):
        """Send periodic heartbeat message to player."""
        if not self._connected:
            return
        timestamp = int(time.time())
        await self.send_strm(b"t", replay_gain=timestamp, flags=0)

    async def _send_frame(self, command, data):
        """Send command to Squeeze player."""
        if self._reader.at_eof() or self._writer.is_closing():
            LOGGER.debug("Socket is disconnected.")
            self._connected = False
            return
        packet = struct.pack("!H", len(data) + 4) + command + data
        try:
            self._writer.write(packet)
            await self._writer.drain()
        except ConnectionResetError:
            self._connected = False
            self.callback(EventType.PLAYER_DISCONNECTED, self)

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
                        LOGGER.warning("No handler for %s", operation)
                    else:
                        handler(packet)
        # EOF reached: socket is disconnected
        LOGGER.debug("Socket disconnected: %s", self._writer.get_extra_info("peername"))
        self._connected = False
        self.callback(EventType.PLAYER_DISCONNECTED, self)

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
        flags=0x40,
        output_threshold=0,
        replay_gain=0,
        server_port=80,
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

    def _process_helo(self, data):
        """Process incoming HELO event from player (player connected)."""
        # pylint: disable=unused-variable
        # player connected
        (dev_id, rev, mac) = struct.unpack("BB6s", data[:8])
        device_mac = ":".join("%02x" % x for x in mac)
        self._player_id = str(device_mac).lower()
        self._device_type = DEVICE_TYPE.get(dev_id, "unknown device")
        LOGGER.debug("Player connected: %s", self.name)
        asyncio.create_task(self._initialize_player())
        self.callback(EventType.PLAYER_CONNECTED, self)

    def _process_stat(self, data):
        """Redirect incoming STAT event from player to correct method."""
        event = data[:4].decode()
        event_data = data[4:]
        if event == b"\x00\x00\x00\x00":
            # Presumed informational stat message
            return
        event_handler = getattr(self, "_process_stat_%s" % event.lower(), None)
        if event_handler is None:
            LOGGER.debug("Unhandled event: %s - event_data: %s", event, event_data)
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
        """Process incoming stat AUDg message (volume level)."""
        # TODO: process volume level
        LOGGER.debug("AUDg received - Volume level: %s", data)
        self.callback(EventType.PLAYER_UPDATED, self)

    def _process_stat_stmd(self, data):
        """Process incoming stat STMd message (decoder ready)."""
        # pylint: disable=unused-argument
        LOGGER.debug("STMd received - Decoder Ready for next track.")
        self.callback(EventType.PLAYER_DECODER_READY, self)

    def _process_stat_stmf(self, data):
        """Process incoming stat STMf message (connection closed)."""
        # pylint: disable=unused-argument
        LOGGER.debug("STMf received - connection closed.")
        self._state = PlayerState.IDLE
        self._elapsed_milliseconds = 0
        self._elapsed_seconds = 0
        self.callback(EventType.PLAYER_UPDATED, self)

    @classmethod
    def _process_stat_stmo(cls, data):
        """
        Process incoming stat STMo message.

        No more decoded (uncompressed) data to play; triggers rebuffering.
        """
        # pylint: disable=unused-argument
        LOGGER.warning("STMo received - output underrun.")

    def _process_stat_stmp(self, data):
        """Process incoming stat STMp message: Pause confirmed."""
        # pylint: disable=unused-argument
        LOGGER.debug("STMp received - pause confirmed.")
        self._state = PlayerState.PAUSED
        self.callback(EventType.PLAYER_UPDATED, self)

    def _process_stat_stmr(self, data):
        """Process incoming stat STMr message: Resume confirmed."""
        # pylint: disable=unused-argument
        LOGGER.debug("STMr received - resume confirmed.")
        self._state = PlayerState.PLAYING
        self.callback(EventType.PLAYER_UPDATED, self)

    def _process_stat_stms(self, data):
        # pylint: disable=unused-argument
        """Process incoming stat STMs message: Playback of new track has started."""
        LOGGER.debug("STMs received - playback of new track has started.")
        self._state = PlayerState.PLAYING
        self.callback(EventType.PLAYER_UPDATED, self)

    def _process_stat_stmt(self, data):
        """Process incoming stat STMt message: heartbeat from client."""
        # pylint: disable=unused-variable
        self._last_heartbeat = time.time()
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
        if self.state == PlayerState.PLAYING:
            # elapsed seconds is weird when player is buffering etc.
            # only rely on it if player is playing
            self._elapsed_milliseconds = elapsed_milliseconds
            if self._elapsed_seconds != elapsed_seconds:
                self._elapsed_seconds = elapsed_seconds
                self.callback(EventType.PLAYER_UPDATED, self)

    def _process_stat_stmu(self, data):
        """Process incoming stat STMu message: Buffer underrun: Normal end of playback."""
        # pylint: disable=unused-argument
        LOGGER.debug("STMu received - end of playback.")
        self._state = PlayerState.IDLE
        self.callback(EventType.PLAYER_UPDATED, self)

    def _process_stat_stml(self, data):
        """Process incoming stat STMl message: Buffer threshold reached."""
        # pylint: disable=unused-argument
        LOGGER.debug("STMl received - Buffer threshold reached.")
        # autoplay 0 or 2: start playing by send unpause command when buffer full
        asyncio.create_task(self.send_strm(b"u"))

    def _process_stat_stmn(self, data):
        """Process incoming stat STMn message: player couldn't decode stream."""
        # pylint: disable=unused-argument
        LOGGER.debug("STMn received - player couldn't decode stream.")
        self.callback(EventType.PLAYER_DECODER_ERROR)

    def _process_resp(self, data):
        """Process incoming RESP message: Response received at player."""
        LOGGER.debug("RESP received - Response received at player.")
        headers:str = data.decode().lower()
        if "content-type" in headers:
            content_type = headers.split("content-type: ")[-1].split("\r")[0]
            if "wav" in content_type:
                # wave header may contain info about sample rate etc
                # https://www.dialogic.com/webhelp/CSP1010/VXML1.1CI/WebHelp/standards_defaults%20-%20MIME%20Type%20Mapping.htm
                params = dict(parse_qsl(content_type.replace(";", "&")))
                print(params)
                sample_rate = int(params.get("rate", 41100))
                sample_size = int(params.get("bitrate", 16))
                channels = int(params.get("channels", 2))
                print(f"pcm args detected - rate: {sample_rate} - size: {sample_size} - ch: {channels}")
                codc_msg = b"p" + PCM_SAMPLE_SIZE[sample_size] + PCM_SAMPLE_RATE[sample_rate] + str(channels).encode() + b"1"
                asyncio.create_task(self._send_frame(b"codc", codc_msg))
        
        # struct codc_packet *codc = (struct codc_packet *)pkt;
        # LOG_DEBUG("codc: %c", codc->format);
        # codec_open(codc->format, codc->pcm_sample_size, codc->pcm_sample_rate, codc->pcm_channels, codc->pcm_endianness);

        # send continue (used when autoplay 1 or 3)
        asyncio.create_task(self._send_frame(b"cont", b"0"))
        # asyncio.create_task(self.send_strm(b"u"))

    def _process_setd(self, data):
        """Process incoming SETD message: Get/set player firmware settings."""
        id = data[0]
        if id == 0:
            # received player name
            data = data[1:].decode()
            self._device_name = data
        self.callback(EventType.PLAYER_UPDATED, self)


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
