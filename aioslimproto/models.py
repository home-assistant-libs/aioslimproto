"""Models used for the JSON-RPC API."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, TypedDict


class EventType(Enum):
    """Enum with possible slim proto client events."""

    PLAYER_UPDATED = "player_updated"
    PLAYER_HEARTBEAT = "player_heartbeat"
    PLAYER_CONNECTED = "player_connected"
    PLAYER_DISCONNECTED = "player_disconnected"
    PLAYER_NAME_RECEIVED = "player_name_received"
    PLAYER_DISPLAY_RESOLUTION = "player_display_resolution"
    PLAYER_DECODER_READY = "player_decoder_ready"
    PLAYER_DECODER_ERROR = "player_decoder_error"
    PLAYER_OUTPUT_UNDERRUN = "player_output_underrun"
    PLAYER_BUFFER_READY = "player_buffer_ready"
    PLAYER_CLI_EVENT = "player_cli_event"
    PLAYER_BTN_EVENT = "player_btn_event"
    PLAYER_PRESETS_UPDATED = "player_presets_updated"


@dataclass
class SlimEvent:
    """Representation of an Event emitted by/on behalf of a player."""

    type: EventType
    player_id: str
    data: dict[str, Any] | None = None


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
    100: "squeezeesp32",
}


class PlayerState(Enum):
    """Enum with the possible player states."""

    PLAYING = "playing"
    STOPPED = "stopped"
    PAUSED = "paused"
    BUFFERING = "buffering"
    BUFFER_READY = "buffer_ready"


class TransitionType(Enum):
    """Transition type enum."""

    NONE = b"0"
    CROSSFADE = b"1"
    FADE_IN = b"2"
    FADE_OUT = b"3"
    FADE_IN_OUT = b"4"


class VisualisationType(Enum):
    """Visualisation Type enum."""

    NONE = "none"
    SPECTRUM_ANALYZER = "spectrum_analyzer"
    VU_METER_ANALOG = "vu_meter_analog"
    VU_METER_DIGITAL = "vu_meter_digital"
    WAVEFORM = "waveform"


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
    POWER_RELEASE = 131082


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
    "ogg": b"o",
    "aac": b"a",
    "alc": b"l",
    "dsf": b"p",
    "dff": b"p",
    "aif": b"p",
}


PLAYMODE_MAP = {
    PlayerState.STOPPED: "stop",
    PlayerState.PLAYING: "play",
    PlayerState.BUFFER_READY: "play",
    PlayerState.BUFFERING: "play",
    PlayerState.PAUSED: "pause",
}


class MediaMetadata(TypedDict):
    """Optional metadata for playback."""

    item_id: str  # optional
    artist: str  # optional
    album: str  # optional
    title: str  # optional
    image_url: str  # optional
    duration: int  # optional


@dataclass
class MediaDetails:
    """Details of an (media) URL that can be played by a slimproto player."""

    url: str
    mime_type: str | None = None
    metadata: MediaMetadata = field(default_factory=dict)
    transition: TransitionType = TransitionType.NONE
    transition_duration: int = 0


@dataclass
class Preset:
    """Details of a (media) preset for a (supported) slimproto player."""

    uri: str  # may be url or uri
    text: str
    icon: str


class CommandMessage(TypedDict):
    """Representation of Base JSON RPC Command Message."""

    # https://www.jsonrpc.org/specification

    id: int | str
    method: str
    params: tuple[str, list[str | int]]


class CommandResultMessage(CommandMessage):
    """Representation of JSON RPC Result Message."""

    result: Any


class ErrorDetails(TypedDict):
    """Representation of JSON RPC ErrorDetails."""

    code: int
    message: str


class CommandErrorMessage(CommandMessage, TypedDict):
    """Base Representation of JSON RPC Command Message."""

    id: int | str | None
    error: ErrorDetails


class CometDResponse(TypedDict):
    """CometD Response Message."""

    channel: str
    id: str
    data: dict[str, Any]


class SlimSubscribeData(CometDResponse):
    """CometD SlimSubscribe Data."""

    response: str  # e.g. '/slim/serverstatus',  the channel all messages should be sent back on  # noqa: E501
    request: tuple[
        str,
        list[str | int],
    ]  # [ '', [ 'serverstatus', 0, 50, 'subscribe:60' ]
    priority: int  # # optional priority value, is passed-through with the response


class SlimSubscribeMessage(CometDResponse):
    """CometD SlimSubscribe Message."""

    channel: str
    id: str
    data: SlimSubscribeData


class PlayerItem(TypedDict):
    """PlayerItem Params TypedDict definition."""

    playerindex: str
    playerid: str
    name: str
    modelname: str
    connected: int
    isplaying: int
    power: int
    model: str
    canpoweroff: int
    firmware: str
    isplayer: int
    displaytype: str
    uuid: str | None
    seq_no: str
    ip: str


class PlayersResponse(TypedDict):
    """PlayerItem Params TypedDict definition."""

    count: int
    players_loop: list[PlayerItem]


PlaylistItem = TypedDict(
    "PlaylistItem",
    {
        "playlist index": int,
        "id": str,
        "title": str,
        "artist": str,
        "remote": int,
        "remote_title": str,
        "artwork_url": str,
        "bitrate": str,
        "samplerate": str,
        "samplesize": str,
        "duration": str | int | None,
        "coverid": str,
        "params": dict,
    },
)


class MenuItemParams(TypedDict):
    """MenuItems Params TypedDict definition."""

    track_id: str | int
    playlist_index: int


class SlimMenuItem(TypedDict):
    """Representation of a SlimMenuItem."""

    style: str
    track: str
    album: str
    trackType: str
    icon: str
    artist: str
    text: str
    params: MenuItemParams
    type: str
    actions: dict  # optional


PlayerStatusResponse = TypedDict(
    "PlayerStatusResponse",
    {
        "time": int,
        "mode": str,
        "sync_slaves": str,
        "playlist_cur_index": int | None,
        "player_name": str,
        "sync_master": str,
        "player_connected": int,
        "power": int,
        "mixer volume": int,
        "playlist repeat": int,
        "playlist shuffle": int,
        "playlist mode": str,
        "player_ip": str,
        "remoteMeta": dict | None,
        "digital_volume_control": int,
        "playlist_timestamp": float,
        "current_title": str,
        "duration": int,
        "seq_no": int,
        "remote": int,
        "can_seek": int,
        "signalstrength": int,
        "rate": int,
        "uuid": str,
        "playlist_tracks": int,
        "item_loop": list[PlaylistItem],
    },
)


ServerStatusResponse = TypedDict(
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
        "players_loop": list[PlayerItem],
        "player count": int,
        "other player count": int,
        "other_players_loop": list[PlayerItem],
    },
)
