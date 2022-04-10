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


def run_periodic(period):
    """Run a coroutine at interval."""

    def scheduler(fcn):
        async def wrapper(*args, **kwargs):
            while True:
                asyncio.create_task(fcn(*args, **kwargs))
                await asyncio.sleep(period)

        return wrapper

    return scheduler


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
