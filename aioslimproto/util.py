"""Helpers and utils."""
from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any, Dict
from urllib.parse import parse_qsl


def get_ip():
    """Get primary IP-address for this host."""
    # pylint: disable=broad-except,no-member
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # doesn't even have to be reachable
        sock.connect(("10.255.255.255", 1))
        _ip = sock.getsockname()[0]
    except Exception:
        _ip = "127.0.0.1"
    finally:
        sock.close()
    return _ip


def get_hostname():
    """Get hostname for this machine."""
    # pylint:disable=no-member
    return socket.gethostname()


def is_port_in_use(port: int) -> bool:
    """Check if port is in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _sock:
        try:
            return _sock.connect_ex(("localhost", port)) == 0
        except socket.gaierror:
            return True


async def select_free_port(range_start: int, range_end: int) -> int:
    """Automatically find available port within range."""

    def _select_free_port():
        for port in range(range_start, range_end):
            if not is_port_in_use(port):
                return port

    return await asyncio.get_running_loop().run_in_executor(None, _select_free_port)


def parse_capabilities(helo_data: bytes) -> Dict[str, Any]:
    """Try to parse device capabilities from HELO string."""
    # b"\x0c\x00\xb8'\xeb:D\xa2\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\
    # x00\x00\x00@\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00Model=squeezelite,
    # AccuratePlayPoints=1,HasDigitalOut=1,HasPolarityInversion=1,Firmware=v1.9.0-1121-pCP,
    # ModelName=SqueezeLite,MaxSampleRate=192000,aac,ogg,flc,aif,pcm,mp3"
    params = {}
    try:
        info = helo_data[36:].decode()
        params = dict(parse_qsl(info.replace(",", "&")))
        # try to parse codecs which are hidden in MaxSampleRate
        if "MaxSampleRate=" in info:
            codec_parts = info.split("MaxSampleRate=")[-1].split(",")[1:]
            params["SupportedCodecs"] = codec_parts
    except Exception as exc:  # pylint: disable=broad-except
        # I have no idea if this message is the same for all device types
        # so a big try..except around it

        logging.getLogger(__name__).exception(
            "Error while parsing device info", exc_info=exc
        )
        logging.getLogger(__name__).debug(helo_data)
    return params


def parse_headers(resp_data: bytes) -> Dict[str, str]:
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
