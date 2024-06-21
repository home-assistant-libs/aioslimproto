"""Helpers and utils."""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any
from urllib.parse import parse_qsl

from .const import FALLBACK_CODECS


def get_ip() -> str:
    """Get primary IP-address for this host."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # doesn't even have to be reachable
        sock.connect(("10.255.255.255", 1))
        _ip = sock.getsockname()[0]
    except Exception:  # noqa: BLE001
        _ip = "127.0.0.1"
    finally:
        sock.close()
    return _ip


def get_hostname() -> str:
    """Get hostname for this machine."""
    return socket.gethostname()


async def select_free_port(range_start: int, range_end: int) -> int:
    """Automatically find available port within range."""

    def is_port_in_use(port: int) -> bool:
        """Check if port is in use."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _sock:
            try:
                _sock.bind(("0.0.0.0", port))  # noqa: S104
            except OSError:
                return True
        return False

    def _select_free_port() -> int:
        for port in range(range_start, range_end):
            if not is_port_in_use(port):
                return port
        msg = "No free port available"
        raise OSError(msg)

    return await asyncio.to_thread(_select_free_port)


def parse_capabilities(helo_data: bytes) -> dict[str, Any]:
    """Try to parse device capabilities from HELO string."""
    # ruff: noqa: E501, ERA001
    # b"\x0c\x00\xb8'\xeb:D\xa2\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\
    # x00\x00\x00@\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00Model=squeezelite,
    # AccuratePlayPoints=1,HasDigitalOut=1,HasPolarityInversion=1,Firmware=v1.9.0-1121-pCP,
    # ModelName=SqueezeLite,MaxSampleRate=192000,aac,ogg,flc,aif,pcm,mp3"

    # \x0c\x00\xac\xdeH\x00\x11"m\xca&\x15j\x1e9\xc3\x02zr\xdb\xf9\xf0\xda\xb5\x00\
    # x00\x00\x00\x00\x00\x00\x00\x00\x00NLModel=squeezeplay,ModelName=SqueezePlay,
    # Firmware=8.0.1-r1382,Balance=1,alc,aac,ogg,ogf,flc,aif,pcm,mp3,MaxSampleRate=384000,
    # AccuratePlayPoints,ImmediateCrossfade,test,Rtmp=2
    params = {}
    try:
        info = helo_data[36:].decode()
        params = dict(parse_qsl(info.replace(",", "&")))
        # try to parse codecs which are hidden in the connection string
        params["SupportedCodecs"] = [
            codec
            for codec in ("alc", "aac", "ogg", "ogf", "flc", "aif", "pcm", "mp3")
            if codec in info
        ] or FALLBACK_CODECS
    except Exception as exc:
        # I have no idea if this message is the same for all device types
        # so a big try..except around it

        logging.getLogger(__name__).exception(
            "Error while parsing device info",
            exc_info=exc,
        )
        logging.getLogger(__name__).debug(helo_data)
    return params


def parse_headers(resp_data: bytes) -> dict[str, str]:
    """Parse headers from raw (HTTP) response message."""
    result = {}
    raw_headers: str = resp_data.decode().split("\r\n")[1:]
    for header_part in raw_headers:
        subparts = header_part.split(": ")
        if len(subparts) < 2:
            continue
        key = subparts[0].lower()
        value = subparts[1]
        result[key.lower()] = value
    return result


def parse_status(resp_data: bytes) -> tuple[str, int, str]:
    """Parse HTTP status from raw (HTTP) response message."""
    http_version = "HTTP/1.0"
    http_status_code = 200
    http_status_str = ""
    raw_status_line = resp_data.decode().split("\r\n")[0]
    status_line_parts = raw_status_line.split(" ", 2)
    http_version = status_line_parts[0]
    http_status_code = int(status_line_parts[1])
    if len(status_line_parts) > 2:
        http_status_str = status_line_parts[2]
    return (http_version, http_status_code, http_status_str)


def empty_queue(q: asyncio.Queue) -> None:
    """Empty an asyncio Queue."""
    for _ in range(q.qsize()):
        try:
            q.get_nowait()
            q.task_done()
        except (asyncio.QueueEmpty, ValueError):
            pass
